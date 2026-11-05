"""Visitor greeting and local camera configuration checks without a real webcam."""

import base64
import json
import io
import os
import unittest
import pytest
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


def presentation_trace(frames, names=None):
    """Exercise legacy fallback too, so the baseline fails on repeated speech."""
    import camera_presence as camera,inspect
    tracker=VisitorTracker(cooldown=120)
    policy=camera.VisitorGreetingPolicy() if hasattr(camera,'VisitorGreetingPolicy') else None
    presented=[]
    for index,(features,boxes) in enumerate(frames):
        arrivals=(tracker.observe(features,index*.5,boxes=boxes)
                  if 'boxes' in inspect.signature(tracker.observe).parameters else tracker.observe(features,index*.5))
        labels=[None if names is None else names[index] for _ in arrivals]
        flags=getattr(tracker,'arrival_continuities',[False]*len(arrivals))
        selected=policy.select(labels,flags,index*.5) if policy else labels
        presented.extend(selected)
    return presented


def test_continuous_same_position_drift_does_not_repeat_unknown_introduction():
    a,b,c=[1.,0.,0.],[.6,.8,0.],[.6,0.,.8]
    box=[.1,.1,.2,.3]
    assert presentation_trace([([f],[box]) for f in (a,a,b,b,c,c)])==[None]


@pytest.mark.parametrize('reversed_order',[False,True])
def test_occluding_new_visitor_is_greeted_when_original_becomes_visible_again(reversed_order):
    a,b=[1.,0.,0.],[.6,.8,0.]
    left,right=[.1,.1,.2,.3],[.6,.1,.2,.3]
    final=([b,a],[right,left]) if reversed_order else ([a,b],[left,right])
    frames=[([a],[left]),([a],[left]),([b],[left]),final]
    assert presentation_trace(frames)==[None,None]


def test_pending_presentation_continuity_expires_when_face_moves_away():
    a,b=[1.,0.,0.],[.6,.8,0.]
    left,right=[.1,.1,.2,.3],[.6,.1,.2,.3]
    assert presentation_trace([([a],[left]),([a],[left]),([b],[left]),([b],[right])])==[None,None]


def test_pending_presentation_continuity_is_not_unique_after_new_overlap():
    a,b,c=[1.,0.,0.],[.6,.8,0.],[0.,0.,1.]
    box=[.1,.1,.2,.3]
    assert presentation_trace([([a],[box]),([a],[box]),([b],[box]),([b,c],[box,box])])==[None,None]


def test_known_name_cooldown_cannot_be_bypassed_by_feature_drift():
    a,b,c=[1.,0.,0.],[.6,.8,0.],[.6,0.,.8]
    box=[.1,.1,.2,.3]
    assert presentation_trace([([f],[box]) for f in (a,a,b,b,c,c)],names=['Alex']*6)==['Alex']


def test_geometry_does_not_hide_first_intro_new_second_or_new_known_visitor():
    a,b=[1.,0.,0.],[.6,.8,0.]
    left,right=[.1,.1,.2,.3],[.6,.1,.2,.3]
    assert presentation_trace([([f],[left]) for f in (a,b,b)])==[None]
    assert presentation_trace([([a],[left]),([a],[left]),([a,b],[left,right]),([a,b],[left,right])])==[None,None]
    assert presentation_trace([([f],[left]) for f in (a,a,b,b)],names=[None,None,'Jamie','Jamie'])==[None,'Jamie']


def test_geometry_requires_unique_recent_visible_box_and_real_absence_resets_it():
    a,b,c=[1.,0.,0.],[0.,1.,0.],[.6,0.,.8]
    box=[.1,.1,.2,.3]
    # Overlapping prior tracks leave ambiguous geometry; do not guess continuity.
    assert presentation_trace([([a,b],[box,box]),([a,b],[box,box]),([c],[box]),([c],[box])])==[None,None,None]
    frames=[([a],[box]),([a],[box])]+[([],[]) for _ in range(5)]+[([c],[box]),([c],[box])]
    assert presentation_trace(frames)==[None,None]
    # Legacy/no-box callers keep the existing feature-only behavior.
    assert presentation_trace([([f],[None]) for f in (a,a,c,c)])==[None,None]


def test_geometry_cannot_steal_stronger_embedding_assignment():
    tracker=VisitorTracker()
    a,b,c=[1.,0.,0.],[0.,1.,0.],[.6,0.,.8]
    left,right=[.1,.1,.2,.3],[.6,.1,.2,.3]
    tracker.observe([a,b],0,boxes=[left,right]);tracker.observe([a,b],.5,boxes=[left,right])
    tracker.observe([c,b],1,boxes=[left,right])
    arrivals=tracker.observe([b,c],1.5,boxes=[right,left])
    assert arrivals==[c] and tracker.arrival_continuities==[True]
    assert len(tracker.tracks)==3  # No identity or feature-track merger.


def test_known_name_cooldowns_are_independent_and_expire():
    from camera_presence import VisitorGreetingPolicy
    policy=VisitorGreetingPolicy(cooldown=120)
    assert policy.select(['Alex'],[False],0)==['Alex']
    assert policy.select(['Alex','Jamie'],[True,True],1)==['Jamie']
    assert policy.select(['Alex'],[False],119)==[]
    assert policy.select(['Alex'],[True],120)==['Alex']


def test_face_observation_boxes_stay_aligned_after_invalid_feature():
    faces=np.array([[0.,0.,20.,20.],[30.,0.,20.,20.],[60.,0.,20.,20.]])
    class Detector:
        def setInputSize(self,_size):pass
        def detect(self,_frame):return None,faces
    class Recognizer:
        def alignCrop(self,_frame,face):return face
        def feature(self,face):return np.array([0.,0.]) if face[0]==30 else np.array([3.,4.])
    model=FaceModel.__new__(FaceModel);model.detector=Detector();model.recognizer=Recognizer()
    model.cv2=SimpleNamespace(error=RuntimeError)
    image=np.zeros((100,100,3),dtype=np.uint8)
    observed=model.observations(image)
    assert [item['feature'] for item in observed]==[[.6,.8],[.6,.8]]
    assert np.allclose([item['box'] for item in observed],[[0.,0.,.2,.2],[.6,0.,.2,.2]])
    assert model.features(image)==[[.6,.8],[.6,.8]]


def test_watch_uses_ephemeral_geometry_without_repeated_unknown_speech(tmp_path,monkeypatch):
    import camera_presence as camera,sys
    permission=[True];index=[0];clock=[0.];events=[]
    vectors=([1.,0.,0.],[1.,0.,0.],[.6,.8,0.],[.6,.8,0.],[.6,0.,.8],[.6,0.,.8])
    class Capture:
        def isOpened(self):return True
        def read(self):
            number=index[0];index[0]+=1;clock[0]=number*.5
            if number>=len(vectors):permission[0]=False
            return True,np.full((8,8,3),number,dtype=np.uint8)
        release=Mock()
    capture=Capture()
    class Model:
        def observations(self,frame):
            return [{'feature':vectors[int(frame[0,0,0])],'box':[.1,.1,.2,.3]}]
        def identify(self,_feature):return None
    class ImmediateWorker:
        def __init__(self,process):self.process=process
        def submit(self,frame):self.process(frame)
        def close(self):pass
    cascade=SimpleNamespace(empty=lambda:False)
    cv2=SimpleNamespace(VideoCapture=lambda *_:capture,CAP_DSHOW=0,
        data=SimpleNamespace(haarcascades='.'),CascadeClassifier=lambda *_:cascade,
        COLOR_BGR2GRAY=1,cvtColor=lambda frame,*_:frame[:,:,0],error=RuntimeError)
    enroll=Mock()
    monkeypatch.setitem(sys.modules,'cv2',cv2)
    monkeypatch.setitem(sys.modules,'camera_network',SimpleNamespace(camera_source=lambda value:value,greeting_camera=lambda:0))
    monkeypatch.setitem(sys.modules,'face_identity',SimpleNamespace(FaceModel=Model,ENROLL_REQUEST=tmp_path/'absent',enroll=enroll))
    monkeypatch.setattr(camera,'allowed',lambda:permission[0]);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    monkeypatch.setattr(camera,'LatestFrameWorker',ImmediateWorker)
    monkeypatch.setattr(camera,'negotiate_capture',lambda *_:{'width':8,'height':8,'fps':30})
    monkeypatch.setattr(camera,'preview_frame',lambda *_:'')
    monkeypatch.setattr(camera,'time',SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda *_:None))
    monkeypatch.setattr(camera,'emit',lambda kind,value:events.append((kind,value)))
    assert camera.watch()==0
    greetings=[message for kind,message in events if kind=='visitor']
    assert len(greetings)==1 and 'Who are you?' in greetings[0]
    assert all(isinstance(message,str) for _,message in events)  # No geometry payload.
    enroll.assert_not_called();capture.release.assert_called_once()
from visitor_memory import name_from_intro


def static_avatar_cpu_fixture():
    """Exercise the real render/composition path with no model/device calls."""
    import time
    products=[]
    class StaticPortrait(np.ndarray):
        def __array_ufunc__(self,ufunc,method,*inputs,**kwargs):
            if ufunc is np.multiply:products.append(True)
            values=[np.asarray(value) if isinstance(value,StaticPortrait) else value for value in inputs]
            return getattr(ufunc,method)(*values,**kwargs)
    class MotionTensor(np.ndarray):
        def transpose(self,*axes):
            return np.swapaxes(np.asarray(self),*axes) if len(axes)==2 else super().transpose(*axes)
    random=np.random.default_rng(47)
    engine=avatar_video.CaleAvatar.__new__(avatar_video.CaleAvatar)
    engine.closed=False;engine.np=np
    engine.portrait=random.integers(0,256,(16,40,3),dtype=np.uint8).view(StaticPortrait)
    engine.crop_box=(10,0,26,16)
    engine.mask=np.arange(256,dtype=np.float32).reshape(16,16,1)/255
    pixels=random.integers(0,256,(16,16,3),dtype=np.uint8)
    engine.cv2=SimpleNamespace(BORDER_REPLICATE=1,COLOR_BGR2RGB=2,INTER_LANCZOS4=4,
        warpAffine=lambda image,*args,**kwargs:image,cvtColor=lambda image,*args:image,
        resize=Mock(side_effect=lambda image,size,**kwargs:image.copy()))
    engine.last_detection=engine.last_seen=time.monotonic();engine.last_box=(0,0,8)
    info={'pitch':0,'yaw':0,'roll':0,'exp':np.zeros((1,1,3),dtype=np.float32)}
    engine.model=SimpleNamespace(prepare_source=lambda image:image,get_kp_info=lambda _:info,
                                stitching=lambda _source,target:target)
    engine.rotation=lambda *_args:np.eye(3,dtype=np.float32)[None].view(MotionTensor)
    engine.source_rotation=engine.rotation();engine.initial=engine.initial_rotation=None
    engine.source_info={'exp':info['exp'],'scale':np.ones((1,1),dtype=np.float32),
                        'kp':np.zeros((1,1,3),dtype=np.float32),'t':np.zeros((1,3),dtype=np.float32)}
    engine.source_keys=SimpleNamespace(float=lambda:np.zeros((1,1,3),dtype=np.float32))
    engine._decode=lambda _keys:pixels.copy()
    engine.torch=SimpleNamespace(cuda=SimpleNamespace(empty_cache=Mock()))
    return engine,pixels,products


def test_avatar_static_composition_does_not_repeat_background_products():
    engine,pixels,products=static_avatar_cpu_fixture()
    portrait=np.asarray(engine.portrait).copy();mask=engine.mask.copy()
    expected=portrait.copy()
    expected[:,10:26]=np.clip(pixels*mask+portrait[:,10:26]*(1-mask),0,255).astype(np.uint8)
    if hasattr(engine,'_cache_static_composite'):engine._cache_static_composite()
    before=len(products)
    first=engine.render(np.zeros((8,8,3),dtype=np.uint8))
    second=engine.render(np.zeros((8,8,3),dtype=np.uint8))
    assert np.array_equal(first,expected) and np.array_equal(second,expected)
    assert len(products)==before,'static portrait weighting must not repeat per render'
    assert not np.shares_memory(first,second) and not np.shares_memory(first,engine.portrait)
    assert np.array_equal(engine.portrait,portrait) and np.array_equal(engine.mask,mask)
    for call in engine.cv2.resize.call_args_list:
        assert call.kwargs=={'interpolation':engine.cv2.INTER_LANCZOS4}


def test_avatar_cached_blend_is_byte_equal_for_every_uint8_mask_pixel_background():
    engine=avatar_video.CaleAvatar.__new__(avatar_video.CaleAvatar)
    engine.np=np;engine.crop_box=(0,0,256,256)
    # Channels are independent. Exhaust all256^3 combinations, including fully
    # transparent/opaque mask boundaries, without any GPU or camera machinery.
    pixels=np.broadcast_to(np.arange(256,dtype=np.uint8)[:,None,None],(256,256,1))
    engine.portrait=np.broadcast_to(np.arange(256,dtype=np.uint8)[None,:,None],(256,256,1)).copy()
    engine.cv2=SimpleNamespace(INTER_LANCZOS4=4,resize=lambda image,_size,**_kwargs:image)
    for value in range(256):
        engine.mask=np.full((256,256,1),value,dtype=np.float32)/255
        expected=np.clip(pixels*engine.mask+engine.portrait*(1-engine.mask),0,255).astype(np.uint8)
        engine._cache_static_composite()
        actual=engine._composite(pixels)
        assert np.array_equal(actual,expected),value
        assert engine.mask_inverse.dtype==engine.masked_background.dtype==np.float32


def test_avatar_static_blend_buffers_are_released_on_owned_close():
    engine,_pixels,_products=static_avatar_cpu_fixture()
    engine._cache_static_composite()
    engine.close()
    assert engine.closed and engine.mask is engine.mask_inverse is engine.masked_background is None
    engine.torch.cuda.empty_cache.assert_called_once()


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
               'show_camera_preview':Mock(),'window':Mock(),'camera_generation':[0]}
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
        'greeting_until':[0],'time':__import__('time'),'current':lambda:True,'generation':0,'animated':False,'sharing':False,'camera_start_lock':__import__('threading').Lock(),'stop_owned_camera_process':Mock(),'camera_worker_command':Mock(return_value=['fake-camera-command'])})
    exec(compile(ast.Module(body=[camera_monitor],type_ignores=[]),str(source),'exec'),namespace)
    with patch.object(Path,'is_file',return_value=True):namespace['monitor']()
    assert namespace['camera_frames'].get_nowait()==(0,'99')
    assert namespace['voice_events'].get_nowait()==('camera_session',(0,'camera_notice','native resolution'))
    assert namespace['voice_events'].empty()


def test_media_camera_reuses_native_negotiation_and_returns_actual_dimensions(monkeypatch,tmp_path):
    import camera_presence as cam
    import media_worker
    # Exercise the real raw-mode guard with owned test settings. An installed
    # user's animated-camera choice must not decide this mocked capture test.
    media = tmp_path / 'Media'
    media.mkdir()
    (media / 'camera-avatar.json').write_text(json.dumps({'enabled': False}), encoding='utf-8')
    monkeypatch.setattr(media_worker, 'MEDIA', media)
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

def test_avatar_profile_checks_exact_sources_models_and_extra_executable_files(tmp_path, monkeypatch):
    import hashlib
    import pytest

    source = tmp_path / "source/src"
    source.mkdir(parents=True)
    body = b"raise RuntimeError('verifier must not execute source')\n"
    (source / "safe.py").write_bytes(body)
    model = tmp_path / "models/tensor.bin"
    model.parent.mkdir()
    model.write_bytes(b"verified model fixture")
    monkeypatch.setattr(avatar_video, "AVATAR_SOURCE_FILES", {"src/safe.py": {
        "size": len(body), "blob": hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()}})
    monkeypatch.setattr(avatar_video, "AVATAR_MODEL_FILES", {"tensor.bin": {
        "size": model.stat().st_size, "sha256": hashlib.sha256(model.read_bytes()).hexdigest()}})
    assert avatar_video.verify_avatar_profile(tmp_path) == tmp_path.resolve()
    extra = source / "__init__.py"
    extra.write_text("raise RuntimeError('unverified initializer')", encoding="utf-8")
    with pytest.raises(ValueError, match="unverified"):
        avatar_video.verify_avatar_profile(tmp_path)
    extra.unlink()
    model.write_bytes(b"corrupted model bytes!")
    with pytest.raises(ValueError):
        avatar_video.verify_avatar_profile(tmp_path)


def test_avatar_face_loss_returns_only_neutral_illustration_and_closed_returns_none():
    import pytest

    engine = avatar_video.CaleAvatar.__new__(avatar_video.CaleAvatar)
    camera_ops = SimpleNamespace(COLOR_BGR2GRAY=1,
        resize=lambda image, size: np.zeros((size[1], size[0], 3), dtype=np.uint8),
        cvtColor=lambda image, _format: image[..., 0])
    engine.closed, engine.np, engine.cv2 = False, np, camera_ops
    engine.portrait = np.full((360, 880, 3), [10, 50, 90], dtype=np.uint8)
    engine.detector = SimpleNamespace(detectMultiScale=lambda *_args, **_kwargs: [])
    engine.last_detection = float("-inf")
    engine.last_seen = float("-inf")
    engine.last_box = None
    camera = np.full((480, 640, 3), [255, 0, 0], dtype=np.uint8)
    output = engine.render(camera, sequence=12)
    assert np.array_equal(output, engine.portrait)
    assert not np.shares_memory(output, camera)
    with pytest.raises(ValueError):
        engine.render(camera.astype(np.float32))
    engine.closed = True
    assert engine.render(camera) is None


def test_neutral_avatar_is_the_illustrated_asset_not_a_capture():
    pixels = avatar_video.neutral_camera_avatar()
    assert pixels.dtype == np.uint8 and pixels.shape == (360, 880, 3)

def test_avatar_worker_keeps_latest_frame_and_never_raw_fallback(monkeypatch,tmp_path):
    import threading,time
    import camera_presence as camera
    first=threading.Event();release=threading.Event();last=threading.Event();seen=[];published=[];closed=[]
    neutral=np.zeros((8,8,3),dtype=np.uint8)
    class Engine:
        def __init__(self,*_args):pass
        def render(self,frame,**_kwargs):
            seen.append(int(frame[0,0,0]))
            if len(seen)==1:first.set();assert release.wait(2);raise ValueError('synthetic render failure')
            last.set();return None
        def probe(self):return {'ok':True,'gpu_allocation_bytes':123}
        def close(self):closed.append(True)
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:False)
    monkeypatch.setattr(camera,'allowed',lambda:True);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    # These queue/lifecycle tests replace the neural engine, including its runtime imports.
    monkeypatch.setattr(__import__('video_generation'),'_avatar_runtime_facts',lambda *_:{'ort':'fixture','torch':'fixture'})
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine,raising=False)
    monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:neutral,raising=False)
    monkeypatch.setattr(camera,'emit',Mock())
    monkeypatch.setattr(camera.AvatarFrameWorker,'_publish',lambda self,rgb:published.append(rgb.copy()))
    worker=camera.AvatarFrameWorker(tmp_path)
    try:
        worker.submit(np.full((8,8,3),1,dtype=np.uint8));assert first.wait(2)
        for n in range(2,21):worker.submit(np.full((8,8,3),n,dtype=np.uint8))
        assert worker.pending.qsize()==1
        release.set();assert last.wait(2)
    finally:release.set();worker.close()
    assert seen==[1,20] and closed==[True]
    assert published and all(np.array_equal(image,neutral) for image in published)


def test_avatar_worker_drops_inference_result_after_consent_ends(monkeypatch,tmp_path):
    import threading
    import camera_presence as camera
    entered=threading.Event();release=threading.Event();finished=threading.Event();permission=[True];published=[]
    class Engine:
        def __init__(self,*_args):pass
        def render(self,*_args,**_kwargs):entered.set();assert release.wait(2);return np.ones((8,8,3),dtype=np.uint8)
        def probe(self):return {'ok':True,'gpu_allocation_bytes':123}
        def close(self):finished.set()
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:False)
    monkeypatch.setattr(camera,'allowed',lambda:permission[0]);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    # These queue/lifecycle tests replace the neural engine, including its runtime imports.
    monkeypatch.setattr(__import__('video_generation'),'_avatar_runtime_facts',lambda *_:{'ort':'fixture','torch':'fixture'})
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine,raising=False)
    monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:np.zeros((8,8,3),dtype=np.uint8),raising=False)
    monkeypatch.setattr(camera.AvatarFrameWorker,'_publish',lambda self,rgb:published.append(int(rgb[0,0,0])))
    monkeypatch.setattr(camera,'emit',Mock())
    worker=camera.AvatarFrameWorker(tmp_path)
    worker.submit(np.full((8,8,3),99,dtype=np.uint8));assert entered.wait(2)
    permission[0]=False;release.set();assert finished.wait(2);worker.close()
    assert published==[0]  # Only the neutral avatar, never late render/raw input.


def test_avatar_worker_skips_old_pending_input_after_engine_load(monkeypatch,tmp_path):
    import camera_presence as camera,threading,time
    loading=threading.Event();release=threading.Event();rendered=threading.Event();seen=[]
    class Engine:
        def __init__(self,*_args):pass
        def probe(self):loading.set();assert release.wait(2);return {'ok':True,'gpu_allocation_bytes':1}
        def render(self,frame,**_kwargs):
            seen.append(int(frame[0,0,0]))
            if seen[-1]==2:rendered.set()
            return None
        def close(self):pass
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    monkeypatch.setattr(camera,'allowed',lambda:True);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:False)
    # These queue/lifecycle tests replace the neural engine, including its runtime imports.
    monkeypatch.setattr(__import__('video_generation'),'_avatar_runtime_facts',lambda *_:{'ort':'fixture','torch':'fixture'})
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine)
    monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:np.zeros((8,8,3),dtype=np.uint8))
    monkeypatch.setattr(camera.AvatarFrameWorker,'_publish',lambda *_:None);monkeypatch.setattr(camera,'emit',Mock())
    worker=camera.AvatarFrameWorker(tmp_path)
    try:
        assert loading.wait(2)
        worker.pending.put_nowait((np.full((8,8,3),1,dtype=np.uint8),time.monotonic()-10))
        release.set()
        deadline=time.monotonic()+1
        while not worker.pending.empty() and time.monotonic()<deadline:time.sleep(.005)
        worker.submit(np.full((8,8,3),2,dtype=np.uint8));assert rendered.wait(2)
    finally:release.set();worker.close()
    assert seen==[2]


def avatar_stale_output_case(monkeypatch,tmp_path,late_inference,age=10):
    import camera_presence as camera,threading,time
    entered=threading.Event();release=threading.Event();live=threading.Event();neutral=threading.Event();published=[]
    class Clock:
        offset=0
        def monotonic(self):return time.monotonic()+self.offset
        def __getattr__(self,name):return getattr(time,name)
    clock=Clock()
    class Engine:
        def __init__(self,*_args):pass
        def probe(self):return {'ok':True,'gpu_allocation_bytes':1}
        def render(self,frame,**_kwargs):
            entered.set()
            if late_inference:assert release.wait(2)
            return np.ones((8,8,3),dtype=np.uint8)
        def close(self):pass
    def publish(_self,rgb):
        value=int(rgb[0,0,0]);published.append(value)
        if value:live.set()
        elif len(published)>1:neutral.set()
    monkeypatch.setattr(camera,'ROOT',tmp_path);monkeypatch.setattr(camera,'time',clock)
    monkeypatch.setattr(camera,'allowed',lambda:True);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:False)
    # These queue/lifecycle tests replace the neural engine, including its runtime imports.
    monkeypatch.setattr(__import__('video_generation'),'_avatar_runtime_facts',lambda *_:{'ort':'fixture','torch':'fixture'})
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine)
    monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:np.zeros((8,8,3),dtype=np.uint8))
    monkeypatch.setattr(camera.AvatarFrameWorker,'_publish',publish);monkeypatch.setattr(camera,'emit',Mock())
    worker=camera.AvatarFrameWorker(tmp_path)
    try:
        worker.submit(np.full((8,8,3),99,dtype=np.uint8));assert entered.wait(2)
        if not late_inference:assert live.wait(2)
        clock.offset=age;release.set()
        if age<=camera.AVATAR_MAX_FRAME_AGE:
            assert live.wait(1),published
        else:assert neutral.wait(1),published
    finally:release.set();worker.close()
    assert published==([0,1] if age<=camera.AVATAR_MAX_FRAME_AGE else [0,0] if late_inference else [0,1,0])


def test_avatar_worker_neutralizes_stalled_capture(monkeypatch,tmp_path):
    avatar_stale_output_case(monkeypatch,tmp_path,False)


def test_avatar_worker_neutralizes_late_render(monkeypatch,tmp_path):
    avatar_stale_output_case(monkeypatch,tmp_path,True)


def test_avatar_worker_keeps_delayed_but_still_fresh_good_render(monkeypatch,tmp_path):
    avatar_stale_output_case(monkeypatch,tmp_path,True,age=.5)


def test_avatar_frame_freshness_rejects_invalid_future_and_expired_times(monkeypatch):
    import camera_presence as camera
    monkeypatch.setattr(camera,'time',SimpleNamespace(monotonic=lambda:10.0))
    assert camera.avatar_frame_fresh(10.0)
    assert camera.avatar_frame_fresh(9.5)
    for captured_at in (None,True,'10',float('nan'),float('inf'),0,10.1,9.0):
        assert not camera.avatar_frame_fresh(captured_at)


def test_camera_launcher_reaps_before_replacement_and_never_shares_raw():
    import subprocess,long_tasks
    process=Mock();process.poll.return_value=None
    process.wait.side_effect=[subprocess.TimeoutExpired('owned-camera',3),0]
    long_tasks.stop_owned_camera_process(process)
    assert process.method_calls==[unittest.mock.call.poll(),unittest.mock.call.terminate(),unittest.mock.call.wait(timeout=3),unittest.mock.call.kill(),unittest.mock.call.wait(timeout=3)]
    raw=long_tasks.camera_worker_command(Path('app'),Path('python'),10,20,False,True)
    animated=long_tasks.camera_worker_command(Path('app'),Path('python'),10,20,True,True)
    assert '--avatar' not in raw and '--virtual-output' not in raw
    assert '--avatar' in animated and '--virtual-output' in animated


def test_avatar_mode_blocks_raw_camera_tools_before_capture_or_files(tmp_path,monkeypatch):
    import media_worker
    mode=tmp_path/'camera-avatar.json';mode.write_text(json.dumps({'enabled':True}),encoding='utf-8')
    monkeypatch.setattr(media_worker,'MEDIA',tmp_path)
    monkeypatch.setattr(media_worker,'camera_permission',lambda:True)
    opener=Mock(side_effect=AssertionError('Physical capture must not open'))
    monkeypatch.setattr(media_worker,'open_camera',opener)
    for call in (lambda:media_worker.capture_camera(str(tmp_path/'raw.png')),lambda:media_worker.watch_camera()):
        with unittest.TestCase().assertRaisesRegex(ValueError,'Animated Cale'):call()
    opener.assert_not_called();assert not (tmp_path/'raw.png').exists()
    mode.write_text(json.dumps({'enabled':False}),encoding='utf-8')
    media_worker.require_raw_camera_mode()
    mode.write_text('corrupt',encoding='utf-8')
    with unittest.TestCase().assertRaisesRegex(ValueError,'cannot be checked'):media_worker.require_raw_camera_mode()


def test_avatar_watch_has_one_capture_owner_and_no_raw_preview(monkeypatch,tmp_path):
    import camera_presence as cam,sys
    enabled=[True];frame=np.full((8,8,3),99,dtype=np.uint8);reads=[];received=[];closed=[]
    def read():
        reads.append(True)
        if len(reads)>=3:enabled[0]=False
        return True,frame
    capture=Mock();capture.isOpened.return_value=True;capture.read.side_effect=read
    cascade=Mock();cascade.empty.return_value=False
    opener=Mock(return_value=capture)
    cv2=SimpleNamespace(error=RuntimeError,data=SimpleNamespace(haarcascades='.'),CascadeClassifier=lambda *_:cascade,VideoCapture=opener,CAP_DSHOW=0)
    class Consumer:
        def __init__(self,*_args):pass
        def submit(self,value):received.append(value)
        def close(self):closed.append(True)
    monkeypatch.setitem(sys.modules,'cv2',cv2)
    monkeypatch.setitem(sys.modules,'camera_network',SimpleNamespace(camera_source=lambda _:0,greeting_camera=lambda:0))
    monkeypatch.setattr(cam,'ROOT',tmp_path);monkeypatch.setattr(cam,'allowed',lambda:enabled[0])
    monkeypatch.setattr(cam,'LatestFrameWorker',lambda *_:Mock())
    monkeypatch.setattr(cam,'AvatarFrameWorker',Consumer)
    monkeypatch.setattr(cam,'preview_frame',Mock(side_effect=AssertionError('Raw camera encoded')))
    monkeypatch.setattr(cam,'emit',Mock())
    assert cam.watch(avatar=True)==0
    opener.assert_called_once();capture.release.assert_called_once();assert len(received)==2 and closed==[True]
    cam.preview_frame.assert_not_called()


def test_camera_coalescer_discards_stale_raw_session_after_avatar_switch():
    import ast,queue
    source=Path(__file__).parents[1]/'long_tasks.py';tree=ast.parse(source.read_text(encoding='utf-8'))
    functions={node.name:node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef)}
    ns={'queue':queue,'camera_frames':queue.Queue(maxsize=1),'camera_generation':[4],'show_camera_preview':Mock(),'window':Mock()}
    for name in ('offer_camera_frame','camera_preview_poll'):exec(compile(ast.Module(body=[functions[name]],type_ignores=[]),str(source),'exec'),ns)
    ns['offer_camera_frame']('old raw',4);ns['camera_generation'][0]=5
    ns['camera_preview_poll']();ns['offer_camera_frame']('late raw',4)
    assert ns['camera_frames'].empty();ns['show_camera_preview'].assert_not_called()
    ns['offer_camera_frame']('current avatar',5);ns['camera_preview_poll']()
    ns['show_camera_preview'].assert_called_once_with('current avatar')


def test_avatar_launcher_reaps_old_owner_before_setup_and_new_capture(tmp_path,monkeypatch):
    import ast,queue,threading,time,subprocess,video_generation
    source=Path(__file__).parents[1]/'long_tasks.py';tree=ast.parse(source.read_text(encoding='utf-8'))
    start=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='start_camera_greeting')
    events=[];old=Mock();old.poll.return_value=None;old.terminate.side_effect=lambda:events.append('old terminate');old.wait.side_effect=lambda **_:events.append('old reaped')
    child=Mock();child.stdout=[];child.returncode=0;child.poll.return_value=0
    python=tmp_path/'profile/python.exe';python.parent.mkdir();python.write_bytes(b'fixture')
    def setup(*_args,**_kwargs):
        assert 'old reaped' in events
        assert _kwargs.get('defer_probe') is True
        events.append('setup');return python
    monkeypatch.setattr(video_generation,'ensure_avatar_profile',setup)
    monkeypatch.setitem(__import__('sys').modules,'psutil',SimpleNamespace(Process=lambda _:SimpleNamespace(create_time=lambda:123)))
    def spawned(*args,**kwargs):assert events[-1]=='setup';events.append('new capture');return child
    scheduled=[]
    fake_threading=SimpleNamespace(Thread=lambda **kwargs:SimpleNamespace(start=lambda:scheduled.append(kwargs['target'])))
    import long_tasks
    ns={'greeting_until':[time.time()+60],'greeting_process':[old],'camera_generation':[0],'avatar_selected':SimpleNamespace(get=lambda:True),'virtual_selected':SimpleNamespace(get=lambda:False),'ROOT':tmp_path,'time':time,'threading':fake_threading,'camera_start_lock':threading.Lock(),'clear_camera_preview':Mock(),'update_camera_layout':Mock(),'camera_preview':Mock(),'voice_events':queue.Queue(),'os':os,'stop_owned_camera_process':long_tasks.stop_owned_camera_process,'camera_worker_command':long_tasks.camera_worker_command,'avatar_video':SimpleNamespace(neutral_camera_avatar=lambda:np.zeros((8,8,3),dtype=np.uint8)),'Image':Image,'ImageTk':SimpleNamespace(PhotoImage=lambda _:object()),'offer_camera_frame':Mock(),'subprocess':SimpleNamespace(Popen=spawned,PIPE=-1,DEVNULL=-3,TimeoutExpired=subprocess.TimeoutExpired)}
    exec(compile(ast.Module(body=[start],type_ignores=[]),str(source),'exec'),ns)
    ns['start_camera_greeting'](restart=True)
    assert len(scheduled)==1 and events==[]  # Setup/camera work stays off Tk.
    scheduled[0]()
    assert events==['old terminate','old reaped','setup','new capture']
    assert json.loads((tmp_path/'Media/camera-avatar.json').read_text())=={'enabled':True}
    ns['camera_preview'].configure.assert_called()  # Neutral shown before setup.


def test_avatar_lease_tracks_measured_bytes_and_removes_only_its_owner(tmp_path,monkeypatch):
    import camera_presence as camera,time
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    lease=camera.AvatarGpuLease()
    initial=json.loads(lease.path.read_text())
    assert initial['phase']=='starting' and initial['reserved_bytes']==2*1024**3
    lease.measured({'ok':True,'gpu_allocation_bytes':1455423488})
    active=json.loads(lease.path.read_text())
    assert active['phase']=='active' and active['reserved_bytes']==1455423488+512*1024**2
    assert active['pid']==os.getpid() and active['started']==psutil.Process(os.getpid()).create_time()
    assert active['updated']>=initial['updated']
    lease.close();assert not lease.path.exists()
    other=camera.AvatarGpuLease();other.stopped.set();other.thread.join(2)
    foreign={**json.loads(other.path.read_text()),'started':0};other.path.write_text(json.dumps(foreign))
    other.close();assert other.path.exists()


def test_avatar_heavy_gpu_work_pauses_and_releases_before_resuming(monkeypatch,tmp_path):
    import camera_presence as camera,threading,time
    heavy=[False];permission=[True];first=threading.Event();paused=threading.Event();second=threading.Event();opened=[];closed=[]
    class Engine:
        def __init__(self,*_args):opened.append(True)
        def probe(self):return {'ok':True,'gpu_allocation_bytes':123}
        def render(self,*_args,**_kwargs):
            if len(opened)==1:heavy[0]=True;first.set()
            else:permission[0]=False;second.set()
            return np.ones((8,8,3),dtype=np.uint8)
        def close(self):closed.append(True);paused.set()
    monkeypatch.setattr(camera,'ROOT',tmp_path);monkeypatch.setattr(camera,'allowed',lambda:permission[0]);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:heavy[0])
    # These queue/lifecycle tests replace the neural engine, including its runtime imports.
    monkeypatch.setattr(__import__('video_generation'),'_avatar_runtime_facts',lambda *_:{'ort':'fixture','torch':'fixture'})
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine,raising=False);monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:np.zeros((8,8,3),dtype=np.uint8),raising=False)
    monkeypatch.setattr(camera.AvatarFrameWorker,'_publish',lambda *_:None);monkeypatch.setattr(camera,'emit',Mock())
    worker=camera.AvatarFrameWorker(tmp_path)
    try:
        worker.submit(np.full((8,8,3),99,dtype=np.uint8));assert first.wait(2);assert paused.wait(2)
        deadline=time.monotonic()+2
        while list((tmp_path/'LongTasks').glob('avatar-gpu-active-*.json')) and time.monotonic()<deadline:time.sleep(.01)
        assert not list((tmp_path/'LongTasks').glob('avatar-gpu-active-*.json'))
        heavy[0]=False;worker.submit(np.full((8,8,3),99,dtype=np.uint8));assert second.wait(2)
    finally:permission[0]=False;worker.close()
    assert len(opened)==2 and len(closed)==2
    assert not list((tmp_path/'LongTasks').glob('avatar-gpu-active-*.json'))


def test_avatar_publish_preserves_native_scene_for_ui_and_virtual_bridge(monkeypatch):
    import camera_presence as camera,sys
    scene=np.zeros((360,880,3),dtype=np.uint8);scene[:,:,0]=120;scene[:,440:,1]=240
    def resize(image,size):return np.array(Image.fromarray(image).resize(size))
    def encode(_suffix,bgr,_params):
        stream=io.BytesIO();Image.fromarray(bgr[:,:,::-1]).save(stream,format='JPEG',quality=92)
        return True,SimpleNamespace(tobytes=lambda:stream.getvalue())
    monkeypatch.setitem(sys.modules,'cv2',SimpleNamespace(resize=resize,imencode=encode,IMWRITE_JPEG_QUALITY=1))
    frames=[];monkeypatch.setattr(camera,'emit',lambda kind,payload:frames.append((kind,payload)))
    worker=camera.AvatarFrameWorker.__new__(camera.AvatarFrameWorker)
    worker.authorized=lambda:True;worker.sender_worker=Mock()
    worker._publish(scene)
    assert len(frames)==1 and frames[0][0]=='frame'
    encoded=base64.b64decode(frames[0][1],validate=True)
    with Image.open(io.BytesIO(encoded)) as image:assert image.size==(880,360)
    worker.sender_worker.submit.assert_called_once_with(encoded)


def test_actual_tk_camera_off_stays_visible_outside_hidden_static_portrait(tmp_path):
    """Execute real GUI declarations/callback, not an assumed widget ancestry."""
    import ast,time,tkinter as tk
    from tkinter import ttk
    source=Path(__file__).parents[1]/'long_tasks.py';tree=ast.parse(source.read_text(encoding='utf-8'))
    gui=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='gui')
    assignments={target.id:node for node in ast.walk(gui) if isinstance(node,ast.Assign) for target in node.targets if isinstance(target,ast.Name)}
    functions={node.name:node for node in ast.walk(gui) if isinstance(node,ast.FunctionDef)}
    try:window=tk.Tk()
    except tk.TclError:raise unittest.SkipTest('Tk display unavailable')
    try:
        title_copy=ttk.Frame(window);title_copy.pack(anchor='nw',padx=20,pady=20)
        namespace={'tk':tk,'ttk':ttk,'title_copy':title_copy,'time':time,'json':json,'bg':'#0b1017',
            'greeting_until':[time.time()+3600],'avatar_selected':tk.BooleanVar(window,value=True),'camera_generation':[1],
            'greeting_process':[None],'greeting_config':tmp_path/'camera-greeting.json','camera_consent':tmp_path/'camera-consent.json',
            'status':tk.StringVar(window),'start_camera_greeting':Mock(),'clear_camera_preview':Mock()}
        def execute(node):exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
        for name in ('beach_scene','cale_scene','companion_caption','camera_scene','camera_options'):execute(assignments[name])
        ns=namespace
        ns['beach_scene'].pack(anchor='w');ns['cale_scene'].pack(anchor='w')
        ttk.Label(ns['cale_scene'],text='Static assistant portrait',width=42).pack()
        ns['companion_caption'].pack(fill='x');ns['camera_options'].pack(fill='x')
        execute(assignments['camera_scene_caption']);ns['camera_scene_caption'].pack()
        execute(functions['update_camera_layout']);execute(functions['toggle_camera_greeting'])
        execute(assignments['camera_button']);ns['camera_button'].pack(side='right')
        for width,height in ((1050,740),(1440,1024)):
            window.geometry(f'{width}x{height}+10+10')
            ns['greeting_until'][0]=time.time()+3600
            ns['camera_button'].configure(text='Camera On')
            ns['camera_scene'].pack(anchor='w',before=ns['camera_options'])
            ns['update_camera_layout']();window.update_idletasks();window.update()
            assert not ns['cale_scene'].winfo_ismapped()
            ancestors=[];parent=ns['camera_button'].master
            while parent is not None:ancestors.append(parent);parent=getattr(parent,'master',None)
            assert ns['cale_scene'] not in ancestors
            assert ns['camera_button'].winfo_ismapped()
            assert ns['camera_button'].winfo_rooty()+ns['camera_button'].winfo_height()<=window.winfo_rooty()+height
            ns['camera_button'].invoke();window.update_idletasks();window.update()
            assert ns['greeting_until'][0]==0 and ns['cale_scene'].winfo_ismapped()
            assert ns['camera_button'].winfo_ismapped() and 'Off' in ns['camera_button'].cget('text')
            assert json.loads((tmp_path/'camera-consent.json').read_text())=={'until':0}
    finally:window.destroy()


def test_raw_camera_permission_revoked_during_read_cannot_save(tmp_path,monkeypatch):
    import media_worker,sys
    permission=[True];frame=np.zeros((8,8,3),dtype=np.uint8)
    camera=Mock();camera.isOpened.return_value=True
    def read():permission[0]=False;return True,frame
    camera.read.side_effect=read
    cv2=SimpleNamespace(imwrite=Mock(return_value=True))
    monkeypatch.setitem(sys.modules,'cv2',cv2)
    monkeypatch.setattr(media_worker,'camera_permission',lambda:permission[0])
    monkeypatch.setattr(media_worker,'require_raw_camera_mode',lambda:None)
    monkeypatch.setattr(media_worker,'open_camera',lambda _:camera)
    monkeypatch.setattr(media_worker,'output_file',lambda *_:tmp_path/'raw.png')
    with unittest.TestCase().assertRaisesRegex(ValueError,'turned off'):media_worker.capture_camera(str(tmp_path/'raw.png'))
    cv2.imwrite.assert_not_called();camera.release.assert_called_once();assert not (tmp_path/'raw.png').exists()


def test_raw_watch_checks_permission_again_before_analysis(tmp_path,monkeypatch):
    import media_worker,sys
    permission=[True];frame=np.zeros((8,8,3),dtype=np.uint8)
    camera=Mock();camera.isOpened.return_value=True;camera.read.return_value=(True,frame)
    def write(path,_frame):Path(path).write_bytes(b'owned synthetic frame');permission[0]=False;return True
    monkeypatch.setitem(sys.modules,'cv2',SimpleNamespace(imwrite=write))
    monkeypatch.setattr(media_worker,'ROOT',tmp_path)
    monkeypatch.setattr(media_worker,'camera_permission',lambda:permission[0])
    monkeypatch.setattr(media_worker,'require_raw_camera_mode',lambda:None)
    monkeypatch.setattr(media_worker,'open_camera',lambda _:camera)
    analyze=Mock(side_effect=AssertionError('Revoked camera must not be analyzed'))
    monkeypatch.setattr(media_worker,'recognize_image',analyze)
    with unittest.TestCase().assertRaisesRegex(ValueError,'turned off'):media_worker.watch_camera(seconds=1,max_frames=1)
    analyze.assert_not_called();camera.release.assert_called_once()
    assert not list((tmp_path/'Temp').rglob('camera-*.jpg'))


def test_camera_transport_accepts_hq_avatar_with_strict_upper_bound():
    import ast,queue
    source=Path(__file__).parents[1]/'long_tasks.py';tree=ast.parse(source.read_text(encoding='utf-8'))
    function=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='offer_camera_frame')
    ns={'queue':queue,'camera_frames':queue.Queue(maxsize=1),'camera_generation':[1]}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),ns)
    ns['offer_camera_frame']('H'*300000,1)
    assert ns['camera_frames'].get_nowait()==(1,'H'*300000)
    ns['offer_camera_frame']('X'*2800001,1)
    assert ns['camera_frames'].empty()


def test_avatar_canonical_lease_is_atomic_measured_and_owner_scoped(tmp_path,monkeypatch):
    import camera_presence as camera
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    lease=camera.AvatarGpuLease()
    try:
        assert json.loads(lease.canonical.read_text())==json.loads(lease.path.read_text())
        lease.measured({'ok':True,'gpu_allocation_bytes':456})
        record=json.loads(lease.canonical.read_text())
        assert record['phase']=='active' and record['reserved_bytes']==456+512*1024**2
        newer={**record,'pid':987654,'started':record['started']+10}
        lease.canonical.write_text(json.dumps(newer))
        camera.remove_avatar_lease_records(tmp_path,record['pid'],record['started'])
        assert json.loads(lease.canonical.read_text())==newer
    finally:lease.close()
    assert json.loads(lease.canonical.read_text())['pid']==987654


def test_old_avatar_owner_cannot_displace_or_remove_new_cpu_process_lease(tmp_path,monkeypatch):
    """Real processes/file locks; no camera, Torch, inference or GPU APIs."""
    import camera_presence as camera,subprocess,sys,time
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    old=camera.AvatarGpuLease();child=None
    source=Path(camera.__file__).parent
    script=("import sys;from pathlib import Path;import camera_presence as c;"
            "c.ROOT=Path(sys.argv[1]);lease=c.AvatarGpuLease();"
            "(c.ROOT/'child-ready').write_text(str(__import__('os').getpid()));"
            "sys.stdin.buffer.read(1);lease.close()")
    try:
        child=subprocess.Popen([sys.executable,'-B','-c',script,str(tmp_path)],cwd=source,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        deadline=time.monotonic()+5
        while not (tmp_path/'child-ready').exists() and time.monotonic()<deadline and child.poll() is None:time.sleep(.02)
        assert (tmp_path/'child-ready').exists()
        actual_pid=int((tmp_path/'child-ready').read_text());record=json.loads(old.canonical.read_text());assert record['pid']==actual_pid
        old._write()  # A delayed heartbeat must not replace the newer live owner.
        assert json.loads(old.canonical.read_text())['pid']==actual_pid
        old.close()
        assert json.loads(old.canonical.read_text())['pid']==actual_pid
        child.communicate(b'x',timeout=5);assert child.returncode==0
        assert not old.canonical.exists()
    finally:
        old.close()
        if child is not None and child.poll() is None:child.kill();child.communicate(timeout=5)


def test_owned_venv_launcher_stop_reaps_actual_cpu_child_and_canonical_lease(tmp_path,monkeypatch):
    import camera_presence as camera,subprocess,sys,time
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    script=("import sys;from pathlib import Path;import camera_presence as c;"
            "c.ROOT=Path(sys.argv[1]);lease=c.AvatarGpuLease();"
            "(c.ROOT/'ready').write_text(str(__import__('os').getpid()));sys.stdin.buffer.read(1)")
    child=subprocess.Popen([sys.executable,'-B','-c',script,str(tmp_path)],cwd=Path(camera.__file__).parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline and child.poll() is None:time.sleep(.02)
        assert (tmp_path/'ready').exists()
        actual_pid=int((tmp_path/'ready').read_text());started=psutil.Process(actual_pid).create_time()
        identities=camera.stop_owned_avatar_process_tree(child,root=tmp_path)
        assert (actual_pid,started) in identities
        assert child.poll() is not None and not psutil.pid_exists(actual_pid)
        assert not (tmp_path/'LongTasks/avatar-gpu-current.json').exists()
        assert not list((tmp_path/'LongTasks').glob('avatar-gpu-active-*.json'))
    finally:
        if child.poll() is None:camera.stop_owned_avatar_process_tree(child,root=tmp_path)


def test_avatar_cli_refuses_a_missing_launcher_before_capture(monkeypatch):
    import camera_presence as camera
    monkeypatch.setattr(camera.psutil,'Process',lambda *_:SimpleNamespace(parent=lambda:None))
    with unittest.TestCase().assertRaisesRegex(RuntimeError,'launcher already closed'):camera.start_launcher_guard()


def test_unreaped_avatar_child_blocks_replacement_and_keeps_accounting(tmp_path,monkeypatch):
    import camera_presence as camera
    process=Mock();process.pid=123;process.poll.return_value=None
    child=Mock();child.pid=456;child.create_time.return_value=2
    owner=SimpleNamespace(create_time=lambda:1,children=lambda **_: [child])
    monkeypatch.setattr(camera.psutil,'Process',lambda _:owner)
    monkeypatch.setattr(camera.psutil,'wait_procs',lambda *_args,**_kwargs:([],[child]))
    cleanup=Mock();monkeypatch.setattr(camera,'remove_avatar_lease_records',cleanup)
    with unittest.TestCase().assertRaisesRegex(RuntimeError,'did not exit'):camera.stop_owned_avatar_process_tree(process,root=tmp_path)
    child.terminate.assert_called_once();child.kill.assert_called_once();cleanup.assert_not_called()


def test_camera_switch_wrapper_reaps_real_venv_child_not_only_launcher(tmp_path,monkeypatch):
    import camera_presence as camera,long_tasks,subprocess,sys,time
    monkeypatch.setattr(camera,'ROOT',tmp_path);monkeypatch.setattr(long_tasks,'ROOT',tmp_path)
    script=("import sys;from pathlib import Path;import camera_presence as c;"
            "c.ROOT=Path(sys.argv[1]);lease=c.AvatarGpuLease();"
            "(c.ROOT/'ready').write_text(str(__import__('os').getpid()));sys.stdin.buffer.read(1)")
    process=subprocess.Popen([sys.executable,'-B','-c',script,str(tmp_path)],cwd=Path(camera.__file__).parent,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    actual_pid=None
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(.02)
        assert (tmp_path/'ready').exists();actual_pid=int((tmp_path/'ready').read_text())
        long_tasks.stop_owned_camera_process(process)
        assert not psutil.pid_exists(actual_pid), 'Stopping only the venv launcher left the real camera child running'
        assert not (tmp_path/'LongTasks/avatar-gpu-current.json').exists()
    finally:
        if process.poll() is None:camera.stop_owned_avatar_process_tree(process,root=tmp_path)
        if actual_pid is not None and psutil.pid_exists(actual_pid):
            owned=psutil.Process(actual_pid);owned.terminate();owned.wait(timeout=3)
        if process.stdin is not None:process.stdin.close()


import pytest


@pytest.mark.parametrize("outcome", ["passed", "probe_failed", "consent_ended", "runtime_failed", "runtime_consent_ended"])
def test_live_worker_qualifies_prepared_profile_once_and_never_marks_cancelled_ready(tmp_path,monkeypatch,outcome):
    import threading,time,video_generation
    import camera_presence as camera
    root=tmp_path;profile=video_generation.avatar_profile_root(root)
    video_generation._avatar_write_status(profile,phase='prepared',ready=False,detail='Files checked')
    permission=[True];events=[];closed=threading.Event();published=[]
    facts={'ort':'1.26.0','torch':'2.9.1+cu128','cuda':'12.8','cudnn':123}
    def runtime(*_):
        events.append('runtime')
        if outcome=='runtime_failed':raise RuntimeError('runtime fixture failed')
        if outcome=='runtime_consent_ended':permission[0]=False
        return facts
    class Engine:
        def __init__(self,*_):events.append('engine')
        def probe(self):
            events.append('probe')
            assert video_generation.avatar_profile_status(root)['ready'] is False
            if outcome=='probe_failed':raise RuntimeError('inference fixture failed')
            if outcome=='consent_ended':permission[0]=False
            return {'ok':True,'gpu_allocation_bytes':123,'frames':4}
        def render(self,*_,**__):
            events.append('render');permission[0]=False
            return np.ones((8,8,3),dtype=np.uint8)
        def close(self):events.append('close');closed.set()
    class Lease:
        def measured(self,_):pass
        def close(self):closed.set()
    monkeypatch.setattr(camera,'ROOT',root)
    monkeypatch.setattr(camera,'allowed',lambda:permission[0]);monkeypatch.setattr(camera,'owner_alive',lambda *_:True)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:False)
    monkeypatch.setattr(camera,'AvatarGpuLease',Lease)
    monkeypatch.setattr(video_generation,'_avatar_runtime_facts',runtime)
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine)
    monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:np.zeros((8,8,3),dtype=np.uint8))
    monkeypatch.setattr(camera.AvatarFrameWorker,'_publish',lambda _,image:published.append(image.copy()))
    monkeypatch.setattr(camera,'emit',Mock())
    worker=camera.AvatarFrameWorker(profile)
    try:
        worker.submit(np.ones((8,8,3),dtype=np.uint8))
        assert closed.wait(2)
    finally:worker.close()
    status=video_generation.avatar_profile_status(root)
    assert events.count('probe')==(0 if outcome in {'runtime_failed','runtime_consent_ended'} else 1)
    if outcome=='runtime_consent_ended':assert 'engine' not in events
    if outcome=='passed':
        assert status['ready'] is True and status['phase']=='ready'
        assert status['probe']['torch']==facts['torch'] and status['probe']['probe']['frames']==4
        assert events[:3]==['runtime','engine','probe'] and 'render' in events
        # Closing the camera does not erase already-qualified runtime readiness.
        assert video_generation.avatar_profile_status(root)['ready'] is True
    else:
        assert status['ready'] is False
        assert 'render' not in events
        if outcome not in {'consent_ended','runtime_consent_ended'}:assert status['phase']=='failed'
    assert published and all(not image.any() for image in published)
