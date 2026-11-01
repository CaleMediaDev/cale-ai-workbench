"""Visitor greeting and local camera configuration checks without a real webcam."""

import os
import unittest

import psutil

from camera_network import _local_stream
from camera_presence import VisitorState, owner_alive
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


if __name__ == "__main__":
    unittest.main()
