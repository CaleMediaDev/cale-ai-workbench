"""Opt-in, local webcam visitor greetings. Frames are never saved or uploaded."""

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent
CONSENT = ROOT / "Media/camera-consent.json"


def allowed(now=None):
    """Share the desktop app's expiring camera permission with camera tools."""
    try:
        until = float(json.loads(CONSENT.read_text(encoding="utf-8"))["until"])
        return until > (time.time() if now is None else now)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def owner_alive(pid=None, started=None):
    """Stop watching if the desktop window that enabled the camera disappears."""
    if pid is None:
        return True  # Direct bounded tool and smoke-test invocations.
    try:
        owner = psutil.Process(int(pid))
        return owner.is_running() and abs(owner.create_time() - float(started)) < 1
    except (OSError, ValueError, TypeError, psutil.Error):
        return False


def watch_owner(pid, started):
    """Exit promptly even if a network camera's frame read is blocking."""
    while owner_alive(pid, started):
        time.sleep(0.5)
    os._exit(0)


class VisitorState:
    """Require a stable face, then an empty room before greeting again."""

    def __init__(self, cooldown=120):
        self.cooldown = cooldown
        self.present_frames = 0
        self.absent_frames = 0
        self.visitor_present = False
        self.last_greeted = float("-inf")

    def observe(self, face_seen, now):
        if face_seen:
            self.present_frames += 1
            self.absent_frames = 0
            if not self.visitor_present and self.present_frames >= 2:
                self.visitor_present = True
                if now - self.last_greeted >= self.cooldown:
                    self.last_greeted = now
                    return True
        else:
            self.absent_frames += 1
            self.present_frames = 0
            if self.absent_frames >= 5:
                self.visitor_present = False
        return False


class VisitorTracker:
    """Track individual faces and greet once per arrival, subject to cooldown."""

    def __init__(self, cooldown=120, similarity=0.68):
        self.cooldown = cooldown
        self.similarity = similarity
        self.tracks = []

    def observe(self, features, now):
        # Keep recently departed faces long enough to avoid repeated greetings
        # when someone briefly turns away from the camera.
        self.tracks = [track for track in self.tracks
                       if now - track["last_seen"] <= max(self.cooldown, 30)]
        greetings = []
        features = features[:8]
        matched = set()
        assignments = {}
        # Reserve the strongest matches before an ambiguous detection can
        # consume another visitor's track merely by appearing first.
        candidates = sorted(
            (sum(a * b for a, b in zip(feature, track["feature"])),
             feature_index, track_index)
            for feature_index, feature in enumerate(features)
            for track_index, track in enumerate(self.tracks)
            if len(feature) == len(track["feature"])
        )
        for score, feature_index, track_index in reversed(candidates):
            if (score >= self.similarity and feature_index not in assignments
                    and track_index not in matched):
                assignments[feature_index] = track_index
                matched.add(track_index)
        for feature_index, feature in enumerate(features):
            if feature_index in assignments:
                track = self.tracks[assignments[feature_index]]
                if track["misses"] >= 5:
                    # A real departure starts a new visit. Decide eligibility
                    # now, so waiting in the room cannot outlast a cooldown.
                    track["hits"] = 1
                    track["pending_greeting"] = now - track["last_greeted"] >= self.cooldown
                else:
                    track["hits"] = (
                        track["hits"] + 1
                        if track["misses"] == 0 and now - track["last_seen"] <= 1.5
                        else 1
                    )
                track["last_seen"] = now
                track["feature"] = feature
                track["misses"] = 0
            else:
                track = {"feature": feature, "last_seen": now,
                         "hits": 1, "last_greeted": float("-inf"),
                         "misses": 0, "pending_greeting": True}
                self.tracks.append(track)
                matched.add(len(self.tracks) - 1)
            if track["hits"] >= 2 and track["pending_greeting"]:
                track["pending_greeting"] = False
                track["last_greeted"] = now
                greetings.append(feature)
        for index, track in enumerate(self.tracks):
            if index not in matched:
                track["misses"] += 1
        return greetings


def greeting_line(names):
    """Speak once for a group while keeping unknown visitors invited to answer."""
    known = list(dict.fromkeys(name for name in names if name))
    unknown = len(names) - sum(bool(name) for name in names)
    if len(names) == 1:
        if known:
            return f"Hey {known[0]}! Good to see you. Say Hey Cale if you need anything."
        return ("Hey there! I'm Cale. Who are you? If you'd like me to remember "
                "your name and learn your face on this PC, say Hey Cale, then tell me your name.")
    if unknown:
        hello = ("Hey " + " and ".join(known) + ", and hello to everyone else!") if known else "Hey everyone!"
        return (hello + " I'm Cale. Who are you? If you'd like me to learn your "
                "faces, step in front of the camera one at a time and say Hey Cale, then your name.")
    return "Hey " + " and ".join(known) + "! Good to see you. Say Hey Cale if you need anything."


def emit(kind, message=""):
    print(json.dumps({"event": kind, "message": message}), flush=True)


def preview_frame(frame, cv2):
    """Encode a small in-memory frame for the desktop's local beach scene."""
    try:
        width = 200
        height = max(1, min(160, round(frame.shape[0] * width / frame.shape[1])))
        small = cv2.resize(frame, (width, height))
        ok, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 62])
        return base64.b64encode(encoded.tobytes()).decode("ascii") if ok else ""
    except Exception:
        return ""  # Preview failure must not stop local visitor detection.


def watch(device=0, parent_pid=None, parent_started=None):
    """Detect faces on-device; release the webcam immediately when permission ends."""
    if not allowed() or not owner_alive(parent_pid, parent_started):
        emit("error", "Camera greeting is off. Turn it on in the app first.")
        return 1
    if parent_pid is not None:
        threading.Thread(target=watch_owner,
                         args=(parent_pid, parent_started), daemon=True).start()
    import cv2

    from camera_network import camera_source, greeting_camera
    from face_identity import ENROLL_REQUEST, FaceModel, enroll

    cascade = cv2.CascadeClassifier(
        str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    )
    if cascade.empty():
        emit("error", "The local face detector is unavailable.")
        return 1
    emit("setup", "Preparing local face recognition.")
    try:
        face_model = FaceModel()
    except Exception:
        face_model = None  # Face detection and generic greetings still work offline.
    source = camera_source(greeting_camera())
    camera = (cv2.VideoCapture(source, cv2.CAP_DSHOW)
              if isinstance(source, int) else cv2.VideoCapture(source))
    if not camera.isOpened():
        camera.release()
        emit("error", "Webcam unavailable. Check Windows camera access or close another camera app.")
        return 1
    state = VisitorState()
    tracker = VisitorTracker()
    enrolling_name = None
    enrolling_features = []
    enrolling_started = 0.0
    if not owner_alive(parent_pid, parent_started):
        camera.release()
        emit("stopped", "Camera greeting stopped because the app closed.")
        return 0
    emit("ready", "Camera greeting is on. Faces are checked locally; no video is saved.")
    try:
        failed_reads = 0
        last_preview = 0.0
        while allowed() and owner_alive(parent_pid, parent_started):
            ok, frame = camera.read()
            if not ok:
                failed_reads += 1
                if failed_reads >= 5:
                    emit("error", "The webcam stopped sending frames.")
                    return 1
                time.sleep(0.5)
                continue
            failed_reads = 0
            preview_now = time.monotonic()
            if preview_now - last_preview >= 1:
                payload = preview_frame(frame, cv2)
                if payload:
                    emit("frame", payload)
                last_preview = preview_now
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            height, width = gray.shape[:2]
            if width > 640:
                gray = cv2.resize(gray, (640, round(height * 640 / width)))
            try:
                features = face_model.features(frame) if face_model else []
            except Exception:
                face_model = None
                features = []
                emit("notice", "Face recognition is unavailable; generic greetings still work.")
            if face_model:
                seen = bool(features)
            else:
                faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                                 minSize=(45, 45))
                seen = len(faces) > 0
            feature = features[0] if len(features) == 1 else None
            if face_model and ENROLL_REQUEST.is_file():
                try:
                    enrolling_name = str(json.loads(ENROLL_REQUEST.read_text(encoding="utf-8"))["name"])
                    enrolling_features = []
                    enrolling_started = time.monotonic()
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                ENROLL_REQUEST.unlink(missing_ok=True)
            if enrolling_name and time.monotonic() - enrolling_started > 12:
                emit("notice", "I couldn't learn that face. Try introducing yourself while looking at the camera.")
                enrolling_name = None
            if enrolling_name and feature:
                enrolling_features.append(feature)
                if len(enrolling_features) >= 5:
                    if enroll(enrolling_name, enrolling_features):
                        emit("enrolled", f"I learned {enrolling_name}'s face locally. I might still make mistakes, so please correct me if I do.")
                    enrolling_name = None
            now = time.monotonic()
            if face_model:
                arrivals = tracker.observe(features, now)
                if arrivals:
                    emit("visitor", greeting_line([face_model.identify(item) for item in arrivals]))
            elif state.observe(seen, now):
                emit("visitor", greeting_line([None]))
            time.sleep(0.35)
    finally:
        camera.release()
    emit("stopped", "Camera greeting is off.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--parent-started", type=float)
    options = parser.parse_args()
    sys.exit(watch(parent_pid=options.parent_pid,
                   parent_started=options.parent_started))
