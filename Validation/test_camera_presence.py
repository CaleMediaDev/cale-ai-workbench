"""Visitor greeting and local camera configuration checks without a real webcam."""

import base64
import json
import io
import os
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import psutil
from PIL import Image

import avatar_video
from camera_network import _local_stream
from camera_presence import (
    VisitorState,
    VisitorTracker,
    greeting_line,
    owner_alive,
    preview_frame,
)
from face_identity import FaceModel
from visitor_memory import name_from_intro


class CameraPresenceTests(unittest.TestCase):
    def setUp(self):
        class InlineRecognition:
            def __init__(self, process): self.process = process
            def submit(self, frame): self.process(frame)
            def close(self): pass
        self.addCleanup(patch.stopall)
        patch("camera_presence.LatestFrameWorker", InlineRecognition).start()

    def test_live_preview_is_bounded_and_styled_without_saving_frames(self):
        source = Image.new("RGB", (640, 480), "#4a8fc1")
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG")
        preview = avatar_video.camera_frame(buffer.getvalue())
        self.assertEqual(preview.size, (320, 132))
        self.assertEqual(preview.getpixel((0, 0)), (11, 16, 32))

        jpeg = np.array([1, 2, 3], dtype=np.uint8)
        fake_cv2 = SimpleNamespace(
            IMWRITE_JPEG_QUALITY=1,
            resize=lambda _frame, size: np.zeros((size[1], size[0], 3), dtype=np.uint8),
            imencode=lambda *_args: (True, jpeg),
        )
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.assertEqual(base64.b64decode(preview_frame(frame, fake_cv2)), b"\x01\x02\x03")

    def test_greeting_needs_stable_face_and_empty_room(self):
        state = VisitorState(cooldown=10)
        self.assertFalse(state.observe(True, 0))
        self.assertTrue(state.observe(True, 1))
        self.assertFalse(state.observe(True, 2))
        for now in range(3, 8):
            self.assertFalse(state.observe(False, now))
        self.assertFalse(state.observe(True, 8))
        self.assertFalse(state.observe(True, 9))  # Cooldown still active.
        for now in range(10, 15):
            state.observe(False, now)
        self.assertFalse(state.observe(True, 15))
        self.assertTrue(state.observe(True, 16))

    def test_self_introduction_is_narrow(self):
        self.assertEqual(name_from_intro("I'm Alex"), "Alex")
        self.assertEqual(name_from_intro("my name is Jamie Lee"), "Jamie Lee")
        self.assertIsNone(name_from_intro("I am making a website"))

    def test_network_stream_must_stay_local(self):
        self.assertEqual(_local_stream("rtsp://192.168.1.20/live"),
                         "rtsp://192.168.1.20/live")
        with self.assertRaises(ValueError):
            _local_stream("rtsp://8.8.8.8/live")

    def test_camera_worker_tracks_exact_window_process(self):
        current = psutil.Process(os.getpid())
        self.assertTrue(owner_alive(current.pid, current.create_time()))
        self.assertFalse(owner_alive(current.pid, current.create_time() - 30))
        self.assertFalse(owner_alive(99999999, current.create_time()))

    def test_two_faces_are_greeted_independently(self):
        tracker = VisitorTracker(cooldown=30)
        alex = [1.0, 0.0, 0.0]
        jamie = [0.0, 1.0, 0.0]
        self.assertEqual(tracker.observe([alex], 0), [])
        self.assertEqual(tracker.observe([alex], 0.5), [alex])
        self.assertEqual(tracker.observe([alex, jamie], 1), [])
        self.assertEqual(tracker.observe([jamie, alex], 1.5), [jamie])
        self.assertEqual(tracker.observe([alex, jamie], 2), [])
        self.assertEqual(tracker.observe([], 3), [])
        self.assertEqual(tracker.observe([alex], 4), [])

    def test_ambiguous_detection_does_not_steal_an_exact_match(self):
        tracker = VisitorTracker(cooldown=30)
        alex = [1.0, 0.0]
        jamie = [0.6, 0.8]
        ambiguous_alex = [0.8, 0.6]
        self.assertEqual(tracker.observe([alex, jamie], 0), [])
        self.assertEqual(tracker.observe([alex, jamie], 0.5), [alex, jamie])

        # This detection resembles Jamie more than Alex, but Jamie also has
        # an exact detection. Reserve that exact match before matching Alex.
        self.assertEqual(tracker.observe([ambiguous_alex, jamie], 1), [])
        self.assertEqual(len(tracker.tracks), 2)
        self.assertEqual(tracker.observe([ambiguous_alex, jamie], 1.5), [])

    def test_tracker_requires_consecutive_detections_before_greeting(self):
        tracker = VisitorTracker(cooldown=10)
        face = [1.0, 0.0]
        self.assertEqual(tracker.observe([face], 0), [])
        self.assertEqual(tracker.observe([], 0.35), [])
        self.assertEqual(tracker.observe([face], 0.7), [])
        self.assertEqual(tracker.observe([face], 1.05), [face])
        self.assertEqual(tracker.observe([face], 1.4), [])

    def test_person_staying_in_view_is_not_greeted_again_after_cooldown(self):
        tracker = VisitorTracker(cooldown=10)
        face = [1.0, 0.0]
        self.assertEqual(tracker.observe([face], 0), [])
        self.assertEqual(tracker.observe([face], 0.5), [face])
        for now in (1, 5, 10.5, 20, 30):
            self.assertEqual(tracker.observe([face], now), [])

    def test_long_observation_gap_does_not_create_a_new_arrival(self):
        tracker = VisitorTracker(cooldown=10)
        face = [1.0, 0.0]
        self.assertEqual(tracker.observe([face], 0), [])
        self.assertEqual(tracker.observe([face], 0.5), [face])

        # No observation reported a departure. A slow or stalled camera
        # must not turn this visitor into a new arrival after track expiry.
        self.assertEqual(tracker.observe([face], 31), [])
        self.assertEqual(tracker.observe([face], 31.5), [])
        self.assertEqual(len(tracker.tracks), 1)
        self.assertEqual(tracker.tracks[0]["last_greeted"], 0.5)

    def test_return_after_departure_and_cooldown_is_greeted(self):
        tracker = VisitorTracker(cooldown=10)
        face = [1.0, 0.0]
        tracker.observe([face], 0)
        self.assertEqual(tracker.observe([face], 0.5), [face])
        for now in (1, 2, 3, 4, 5):
            self.assertEqual(tracker.observe([], now), [])
        self.assertEqual(tracker.observe([face], 11), [])
        self.assertEqual(tracker.observe([face], 11.5), [face])

    def test_early_return_does_not_greet_when_cooldown_expires_indoors(self):
        tracker = VisitorTracker(cooldown=10)
        face = [1.0, 0.0]
        tracker.observe([face], 0)
        self.assertEqual(tracker.observe([face], 0.5), [face])
        for now in (1, 2, 3, 4, 5):
            tracker.observe([], now)
        self.assertEqual(tracker.observe([face], 6), [])
        self.assertEqual(tracker.observe([face], 6.5), [])
        self.assertEqual(tracker.observe([face], 20), [])

    def test_group_greeting_invites_one_at_a_time_enrollment(self):
        line = greeting_line(["Alex", None])
        self.assertIn("Alex", line)
        self.assertIn("one at a time", line)
        self.assertIn("Who are you?", line)

    def test_malformed_enrollment_structure_does_not_stop_watcher(self):
        import camera_presence

        request = Mock()
        request.is_file.return_value = True
        request.read_text.return_value = "null"
        model = Mock()
        model.features.return_value = []
        camera = Mock()
        camera.isOpened.return_value = True
        camera.read.return_value = (True, np.zeros((8, 8, 3), dtype=np.uint8))
        cascade = Mock()
        cascade.empty.return_value = False
        cv2 = SimpleNamespace(
            data=SimpleNamespace(haarcascades="."),
            CascadeClassifier=Mock(return_value=cascade),
            VideoCapture=Mock(return_value=camera),
            CAP_DSHOW=0,
            COLOR_BGR2GRAY=1,
            cvtColor=lambda *_args: np.zeros((8, 8), dtype=np.uint8),
        )
        modules = {
            "cv2": cv2,
            "camera_network": SimpleNamespace(
                camera_source=lambda source: source,
                greeting_camera=lambda: 0,
            ),
            "face_identity": SimpleNamespace(
                ENROLL_REQUEST=request,
                FaceModel=Mock(return_value=model),
                enroll=Mock(),
            ),
        }
        with patch.dict("sys.modules", modules), patch.multiple(
            camera_presence,
            allowed=Mock(side_effect=[True, True, True, True, True, False]),
            emit=Mock(),
            preview_frame=Mock(return_value=""),
        ), patch.object(camera_presence.time, "sleep"):
            self.assertEqual(camera_presence.watch(), 0)

        request.unlink.assert_called_once_with(missing_ok=True)
        camera.release.assert_called_once_with()
        model.features.assert_called_once()

    def test_watcher_survives_enrollment_request_cleanup_failure(self):
        import camera_presence

        request = Mock()
        request.is_file.side_effect = [True, False]
        request.read_text.return_value = "null"
        request.unlink.side_effect = PermissionError("request is locked")
        model = Mock()
        model.features.return_value = []
        camera = Mock()
        camera.isOpened.return_value = True
        camera.read.return_value = (True, np.zeros((8, 8, 3), dtype=np.uint8))
        cascade = Mock()
        cascade.empty.return_value = False
        cv2 = SimpleNamespace(
            data=SimpleNamespace(haarcascades="."),
            CascadeClassifier=Mock(return_value=cascade),
            VideoCapture=Mock(return_value=camera),
            CAP_DSHOW=0,
            COLOR_BGR2GRAY=1,
            cvtColor=lambda *_args: np.zeros((8, 8), dtype=np.uint8),
        )
        modules = {
            "cv2": cv2,
            "camera_network": SimpleNamespace(
                camera_source=lambda source: source,
                greeting_camera=lambda: 0,
            ),
            "face_identity": SimpleNamespace(
                ENROLL_REQUEST=request,
                FaceModel=Mock(return_value=model),
                enroll=Mock(),
            ),
        }
        events = Mock()
        with patch.dict("sys.modules", modules), patch.multiple(
            camera_presence,
            allowed=Mock(side_effect=[True, True, True, True, True, True, True, True, True, False]),
            emit=events,
            preview_frame=Mock(return_value=""),
        ), patch.object(camera_presence.time, "sleep"):
            self.assertEqual(camera_presence.watch(), 0)

        request.unlink.assert_called_once_with(missing_ok=True)
        self.assertEqual(camera.read.call_count, 2)
        self.assertEqual(model.features.call_count, 2)
        events.assert_any_call(
            "notice", "The face enrollment request could not be removed."
        )
        events.assert_any_call("stopped", "Camera greeting is off.")
        camera.release.assert_called_once_with()

    def test_watcher_recovers_from_frame_read_exception(self):
        import camera_presence

        class ReadError(Exception):
            pass

        request = Mock()
        request.is_file.return_value = False
        model = Mock()
        model.features.return_value = []
        camera = Mock()
        camera.isOpened.return_value = True
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        camera.read.side_effect = [ReadError("temporary read failure"), (True, frame)]
        cascade = Mock()
        cascade.empty.return_value = False
        cv2 = SimpleNamespace(
            error=ReadError,
            data=SimpleNamespace(haarcascades="."),
            CascadeClassifier=Mock(return_value=cascade),
            VideoCapture=Mock(return_value=camera),
            CAP_DSHOW=0,
            COLOR_BGR2GRAY=1,
            cvtColor=lambda *_args: np.zeros((8, 8), dtype=np.uint8),
        )
        modules = {
            "cv2": cv2,
            "camera_network": SimpleNamespace(
                camera_source=lambda source: source,
                greeting_camera=lambda: 0,
            ),
            "face_identity": SimpleNamespace(
                ENROLL_REQUEST=request,
                FaceModel=Mock(return_value=model),
                enroll=Mock(),
            ),
        }
        events = Mock()
        with patch.dict("sys.modules", modules), patch.multiple(
            camera_presence,
            allowed=Mock(side_effect=[True, True, True, True, True, True, False]),
            emit=events,
            preview_frame=Mock(return_value=""),
        ), patch.object(camera_presence.time, "sleep") as sleep:
            self.assertEqual(camera_presence.watch(), 0)

        self.assertEqual(camera.read.call_count, 2)
        model.features.assert_called_once_with(frame)
        sleep.assert_any_call(0.5)
        camera.release.assert_called_once_with()
        self.assertFalse(any(call.args[0] == "error" for call in events.call_args_list))

    def test_watcher_recovers_from_successful_reads_with_empty_frames(self):
        import camera_presence

        request = Mock()
        request.is_file.return_value = False
        model = Mock()
        model.features.return_value = []
        camera = Mock()
        camera.isOpened.return_value = True
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        camera.read.side_effect = [
            (True, None),
            (True, np.empty((0, 0, 3), dtype=np.uint8)),
            (True, frame),
        ]
        cascade = Mock()
        cascade.empty.return_value = False

        def convert_frame(image, _code):
            self.assertIs(image, frame)
            return np.zeros((8, 8), dtype=np.uint8)

        convert = Mock(side_effect=convert_frame)
        cv2 = SimpleNamespace(
            data=SimpleNamespace(haarcascades="."),
            CascadeClassifier=Mock(return_value=cascade),
            VideoCapture=Mock(return_value=camera),
            CAP_DSHOW=0,
            COLOR_BGR2GRAY=1,
            cvtColor=convert,
        )
        modules = {
            "cv2": cv2,
            "camera_network": SimpleNamespace(
                camera_source=lambda source: source,
                greeting_camera=lambda: 0,
            ),
            "face_identity": SimpleNamespace(
                ENROLL_REQUEST=request,
                FaceModel=Mock(return_value=model),
                enroll=Mock(),
            ),
        }
        events = Mock()
        with patch.dict("sys.modules", modules), patch.multiple(
            camera_presence,
            allowed=Mock(side_effect=[True, True, True, True, True, True, True, False]),
            emit=events,
            preview_frame=Mock(return_value=""),
        ), patch.object(camera_presence.time, "sleep") as sleep:
            self.assertEqual(camera_presence.watch(), 0)

        self.assertEqual(camera.read.call_count, 3)
        self.assertEqual(convert.call_count, 1)
        model.features.assert_called_once_with(frame)
        self.assertEqual(sum(call.args == (0.5,) for call in sleep.call_args_list), 2)
        camera.release.assert_called_once_with()
        self.assertFalse(any(call.args[0] == "error" for call in events.call_args_list))

    def test_watcher_resizes_very_wide_frames_to_positive_height(self):
        import camera_presence

        frame = np.zeros((1, 1281, 3), dtype=np.uint8)
        gray = np.zeros((1, 1281), dtype=np.uint8)
        resized = np.zeros((1, 640), dtype=np.uint8)
        camera = Mock()
        camera.isOpened.return_value = True
        camera.read.return_value = (True, frame)
        cascade = Mock()
        cascade.empty.return_value = False
        cascade.detectMultiScale.return_value = []

        def resize(image, size):
            self.assertIs(image, gray)
            self.assertEqual(size, (640, 1))
            return resized

        resize_mock = Mock(side_effect=resize)
        cv2 = SimpleNamespace(
            data=SimpleNamespace(haarcascades="."),
            CascadeClassifier=Mock(return_value=cascade),
            VideoCapture=Mock(return_value=camera),
            CAP_DSHOW=0,
            COLOR_BGR2GRAY=1,
            cvtColor=Mock(return_value=gray),
            resize=resize_mock,
        )
        modules = {
            "cv2": cv2,
            "camera_network": SimpleNamespace(
                camera_source=lambda source: source,
                greeting_camera=lambda: 0,
            ),
            "face_identity": SimpleNamespace(
                ENROLL_REQUEST=Mock(),
                FaceModel=Mock(side_effect=RuntimeError("model unavailable")),
                enroll=Mock(),
            ),
        }
        with patch.dict("sys.modules", modules), patch.multiple(
            camera_presence,
            allowed=Mock(side_effect=[True, True, True, True, True, False]),
            emit=Mock(),
            preview_frame=Mock(return_value=""),
        ), patch.object(camera_presence.time, "sleep"):
            self.assertEqual(camera_presence.watch(), 0)

        resize_mock.assert_called_once()
        cascade.detectMultiScale.assert_called_once_with(
            resized, scaleFactor=1.1, minNeighbors=5, minSize=(45, 45)
        )
        camera.release.assert_called_once_with()

    def test_enrollment_rejects_multiple_visible_faces(self):
        model = FaceModel.__new__(FaceModel)
        model.features = lambda _frame: [[1.0, 0.0], [0.0, 1.0]]
        self.assertIsNone(model.feature(None))

    def test_face_model_returns_separate_features_for_two_faces(self):
        class Detector:
            def setInputSize(self, _size):
                pass

            def detect(self, _frame):
                return None, np.array([[3.0, 4.0], [4.0, 3.0]])

        class Recognizer:
            def alignCrop(self, _frame, face):
                return face

            def feature(self, aligned):
                return np.asarray(aligned)

        model = FaceModel.__new__(FaceModel)
        model.detector = Detector()
        model.recognizer = Recognizer()
        model.cv2 = type("CV2", (), {"error": RuntimeError})
        features = model.features(np.zeros((8, 8, 3)))
        self.assertEqual(features, [[0.6, 0.8], [0.8, 0.6]])


if __name__ == "__main__":
    unittest.main()


def test_slow_recognition_does_not_stall_capture_or_preview(monkeypatch):
    import camera_presence as cam
    import threading
    import time
    enabled = threading.Event(); enabled.set()
    entered = threading.Event(); release = threading.Event()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    reads = []
    def read():
        time.sleep(0.02)
        reads.append(1)
        if len(reads) >= 20: enabled.clear()
        return True, frame
    camera = Mock(); camera.isOpened.return_value = True; camera.read.side_effect = read
    model = Mock()
    def recognize(_frame):
        entered.set(); release.wait(2); return []
    model.features.side_effect = recognize
    cascade = Mock(); cascade.empty.return_value = False
    cv2 = SimpleNamespace(error=RuntimeError, data=SimpleNamespace(haarcascades="."),
        CascadeClassifier=lambda *_: cascade, VideoCapture=lambda *_: camera,
        CAP_DSHOW=0, COLOR_BGR2GRAY=1, cvtColor=lambda *_: frame[:, :, 0])
    events = []
    monkeypatch.setitem(__import__('sys').modules, 'cv2', cv2)
    monkeypatch.setitem(__import__('sys').modules, 'camera_network', SimpleNamespace(camera_source=lambda _:0, greeting_camera=lambda:0))
    monkeypatch.setitem(__import__('sys').modules, 'face_identity', SimpleNamespace(ENROLL_REQUEST=Mock(),FaceModel=lambda:model,enroll=Mock()))
    monkeypatch.setattr(cam, 'allowed', enabled.is_set)
    monkeypatch.setattr(cam, 'preview_frame', lambda *_:'bounded-preview')
    monkeypatch.setattr(cam, 'emit', lambda *args:events.append(args))
    worker = threading.Thread(target=cam.watch)
    worker.start()
    try:
        assert entered.wait(0.5)
        deadline = time.monotonic()+0.8
        while len(reads)<20 and time.monotonic()<deadline: time.sleep(0.01)
        assert len(reads)>=20, 'capture waited for recognition'
        assert sum(kind=='frame' for kind,*_ in events)>=4, 'preview is still paced at one fps'
        assert not release.is_set()
    finally:
        enabled.clear(); release.set(); worker.join(3)
    assert not worker.is_alive()
    camera.release.assert_called_once()
    assert not any(kind in {'visitor','enrolled'} for kind,*_ in events)


def test_latest_recognition_slot_drops_stale_frames_and_closes():
    import camera_presence as cam
    import threading
    entered=threading.Event(); release=threading.Event(); seen=[]
    def process(frame):
        seen.append(frame)
        if frame==0: entered.set(); release.wait(1)
    worker=cam.LatestFrameWorker(process)
    try:
        worker.submit(0); assert entered.wait(1)
        for n in range(1,101): worker.submit(n)
        assert worker.pending.qsize()==1
        release.set()
        deadline=__import__('time').monotonic()+1
        while len(seen)<2 and __import__('time').monotonic()<deadline: __import__('time').sleep(0.01)
        assert seen==[0,100]
    finally: release.set(); worker.close()
    assert not worker.thread.is_alive() and worker.pending.empty()
    worker.submit(101); assert worker.pending.empty()


def test_camera_requests_native_uhd_but_reports_negotiated_fallback():
    import camera_presence as cam
    props={'CAP_PROP_FOURCC':1,'CAP_PROP_FRAME_WIDTH':2,'CAP_PROP_FRAME_HEIGHT':3,'CAP_PROP_FPS':4,'CAP_PROP_BUFFERSIZE':5,'CAP_PROP_READ_TIMEOUT_MSEC':6}
    cv2=SimpleNamespace(**props,VideoWriter_fourcc=lambda *args:123)
    camera=Mock(); camera.get.side_effect=lambda key:{2:1920,3:1080,4:15}[key]
    result=cam.negotiate_capture(camera,cv2)
    assert result=={'width':1920,'height':1080,'fps':15}
    assert camera.set.call_args_list==[__import__('unittest').mock.call(k,v) for k,v in [(1,123),(2,3840),(3,2160),(4,30),(5,1),(6,2000)]]
    camera.reset_mock(); cam.negotiate_capture(camera,cv2,local=False)
    camera.set.assert_not_called()


def test_camera_preview_uses_latest_slot_independent_of_voice_fifo():
    import ast, queue
    source=Path(__file__).parents[1]/'long_tasks.py'
    tree=ast.parse(source.read_text(encoding='utf-8'))
    functions={node.name:node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef)}
    namespace={'queue':queue,'camera_frames':queue.Queue(maxsize=1),'voice_events':queue.Queue(),
               'show_camera_preview':Mock(),'window':Mock()}
    for name in ['offer_camera_frame','camera_preview_poll']:
        exec(compile(ast.Module(body=[functions[name]],type_ignores=[]),str(source),'exec'),namespace)
    for n in range(100): namespace['offer_camera_frame'](str(n))
    assert namespace['camera_frames'].qsize()==1 and namespace['voice_events'].empty()
    namespace['camera_preview_poll']()
    namespace['show_camera_preview'].assert_called_once_with('99')
    namespace['window'].after.assert_called_once_with(33,namespace['camera_preview_poll'])
    camera_monitor=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='monitor' and 'offer_camera_frame' in ast.unparse(n))
    process=Mock(); process.stdout=io.StringIO(''.join(json.dumps({'event':'frame','message':str(n)})+'\n' for n in range(100))+json.dumps({'event':'notice','message':'native resolution'})+'\n')
    process.returncode=0
    namespace.update({'json':json,'os':os,'subprocess':SimpleNamespace(Popen=Mock(return_value=process),PIPE=-1,DEVNULL=-3),
        'ROOT':source.parent,'media_python':Path('test-only-python'),'greeting_process':[None],
        'greeting_until':[0],'time':__import__('time')})
    exec(compile(ast.Module(body=[camera_monitor],type_ignores=[]),str(source),'exec'),namespace)
    namespace['monitor']()
    assert namespace['camera_frames'].get_nowait()=='99'
    assert namespace['voice_events'].get_nowait()==('camera_notice','native resolution')
    assert namespace['voice_events'].empty()


def test_media_camera_reuses_native_negotiation_and_returns_actual_dimensions(monkeypatch,tmp_path):
    import camera_presence as cam
    import media_worker
    frame=np.zeros((1080,1920,3),dtype=np.uint8)
    camera=Mock(); camera.isOpened.return_value=True; camera.read.return_value=(True,frame)
    cv2=SimpleNamespace(VideoCapture=Mock(return_value=camera),CAP_DSHOW=0,imwrite=Mock(return_value=True))
    monkeypatch.setitem(__import__('sys').modules,'cv2',cv2)
    monkeypatch.setattr(__import__('camera_network'),'camera_source',lambda _:0)
    negotiated=Mock(return_value={'width':1920,'height':1080,'fps':15})
    monkeypatch.setattr(cam,'negotiate_capture',negotiated)
    monkeypatch.setattr(media_worker,'camera_permission',lambda:True)
    monkeypatch.setattr(media_worker,'output_file',lambda *_:tmp_path/'mock-only.png')
    result=media_worker.capture_camera(str(tmp_path/'mock-only.png'))
    negotiated.assert_called_once_with(camera,cv2,True)
    assert result['width']==1920 and result['height']==1080
    camera.release.assert_called_once()
    assert not (tmp_path/'mock-only.png').exists()


def test_permission_revoked_during_capture_never_previews_or_recognizes(monkeypatch):
    import camera_presence as cam
    import threading
    enabled=threading.Event();enabled.set()
    camera=Mock();camera.isOpened.return_value=True
    def read():
        enabled.clear();return True,np.zeros((8,8,3),dtype=np.uint8)
    camera.read.side_effect=read
    cascade=Mock();cascade.empty.return_value=False
    model=Mock()
    cv2=SimpleNamespace(error=RuntimeError,data=SimpleNamespace(haarcascades='.'),CascadeClassifier=lambda *_:cascade,
        CAP_DSHOW=0,VideoCapture=lambda *_:camera)
    monkeypatch.setitem(__import__('sys').modules,'cv2',cv2)
    monkeypatch.setitem(__import__('sys').modules,'camera_network',SimpleNamespace(camera_source=lambda _:0,greeting_camera=lambda:0))
    monkeypatch.setitem(__import__('sys').modules,'face_identity',SimpleNamespace(ENROLL_REQUEST=Mock(),FaceModel=model,enroll=Mock()))
    monkeypatch.setattr(cam,'allowed',enabled.is_set)
    preview=Mock();monkeypatch.setattr(cam,'preview_frame',preview)
    events=Mock();monkeypatch.setattr(cam,'emit',events)
    assert cam.watch()==0
    preview.assert_not_called();model.assert_not_called();camera.release.assert_called_once()


def test_native_uhd_frames_only_reduce_for_recognition_and_compact_preview():
    import camera_presence as cam
    native=np.zeros((2160,3840,3),dtype=np.uint8)
    sizes=[]
    def resize(frame,size):
        sizes.append(size);return np.zeros((size[1],size[0],3),dtype=np.uint8)
    cv2=SimpleNamespace(resize=resize,IMWRITE_JPEG_QUALITY=1,imencode=Mock(return_value=(True,np.array([1],dtype=np.uint8))))
    recognized=cam.recognition_frame(native,cv2)
    assert recognized.shape==(540,960,3)
    assert native.shape==(2160,3840,3)
    cam.preview_frame(native,cv2)
    assert sizes==[(960,540),(320,160)]
    assert cv2.imencode.call_args.args[2]==[1,80]
    tiny=np.zeros((1,1281,3),dtype=np.uint8)
    assert cam.recognition_frame(tiny,cv2).shape==(1,960,3)
