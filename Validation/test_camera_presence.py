"""Visitor greeting and local camera configuration checks without a real webcam."""

import os
import unittest

import numpy as np
import psutil

from camera_network import _local_stream
from camera_presence import VisitorState, VisitorTracker, greeting_line, owner_alive
from face_identity import FaceModel
from visitor_memory import name_from_intro


class CameraPresenceTests(unittest.TestCase):
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
