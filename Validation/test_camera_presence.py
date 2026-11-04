"""Visitor greeting and local camera configuration checks without a real webcam."""

import base64
import io
import os
import unittest
from types import SimpleNamespace
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
    def test_live_preview_is_bounded_and_styled_without_saving_frames(self):
        source = Image.new("RGB", (640, 480), "#4a8fc1")
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG")
        preview = avatar_video.camera_frame(buffer.getvalue())
        self.assertEqual(preview.size, (200, 132))
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
            allowed=Mock(side_effect=[True, True, False]),
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
            allowed=Mock(side_effect=[True, True, True, False]),
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
            allowed=Mock(side_effect=[True, True, True, False]),
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
            allowed=Mock(side_effect=[True, True, True, True, False]),
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
            allowed=Mock(side_effect=[True, True, False]),
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
