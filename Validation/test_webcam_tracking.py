"""Identity teaching, exact-frame requests and bounded local object memory."""

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from camera_presence import selected_enrollment_feature
from webcam_tracking import (
    AutoEnrollment,
    JsonCache,
    ObjectMemory,
    SceneTracker,
    Tracks,
    write_json,
)
from webcam_tracking_ui import TrackingView


def test_restoring_panel_requires_existing_camera_permission(tmp_path, monkeypatch):
    config = tmp_path / "Media/webcam-tracking.json"
    config.parent.mkdir()
    write_json(config, {"enabled": True})
    camera_active = False
    view = TrackingView(None, tmp_path, lambda: 1, lambda: camera_active)
    opened = []
    monkeypatch.setattr(view, "open", lambda: opened.append(True))
    view.restore()
    assert not opened
    camera_active = True
    view.restore()
    assert opened == [True]


def test_memory_cache_reuses_json_and_forget_invalidates(tmp_path, monkeypatch):
    import webcam_tracking as tracking
    path = tmp_path/"names.json"
    write_json(path, {"Cale": [1]})
    calls = []
    original = tracking.read_json
    monkeypatch.setattr(tracking, "read_json", lambda p: (calls.append(p), original(p))[1])
    cache = JsonCache()
    for _ in range(10):
        assert cache.get(path) == {"Cale": [1]}
    assert len(calls) == 1
    write_json(path, {})
    assert cache.get(path) == {} and len(calls) == 2


def test_object_id_survives_a_brief_missed_detection_but_not_an_absence():
    tracks = Tracks()
    def item():
        return {"class": "cup", "box": [.1, .2, .2, .3]}
    first = tracks.update([item()], 1)[0]["id"]
    tracks.update([], 1.4)
    assert tracks.update([item()], 1.8)[0]["id"] == first
    tracks.update([], 2)
    assert tracks.update([item()], 4)[0]["id"] != first


def test_face_alignment_uses_native_pixels_with_scaled_landmarks():
    from face_identity import FaceModel
    model = FaceModel.__new__(FaceModel)
    face = np.array([10, 20, 30, 40, 15, 25, 25, 25, 20, 30, 16, 40, 24, 40, .97], np.float32)
    detected, aligned = [], []
    model.detector = SimpleNamespace(setInputSize=lambda value: detected.append(value), detect=lambda frame: (None, [face]))
    model.recognizer = SimpleNamespace(alignCrop=lambda frame, points: (aligned.append((frame, points.copy())), frame)[1], feature=lambda _: np.array([3., 4.]))
    model.cv2 = SimpleNamespace(error=RuntimeError)
    native = np.zeros((400, 600, 3), np.uint8)
    small = np.zeros((200, 300, 3), np.uint8)
    result = model.observations_native(native, small)
    assert detected == [(300, 200)]
    assert aligned[0][0] is native
    np.testing.assert_allclose(aligned[0][1][:14], face[:14]*2)
    assert aligned[0][1][14] == face[14]
    np.testing.assert_allclose(result[0]["box"], [10/300, 20/200, 30/300, 40/200])
    assert result[0]["feature"] == [.6, .8]


def test_teaching_another_face_angle_retains_old_examples(tmp_path, monkeypatch):
    import face_identity
    monkeypatch.setattr(face_identity, "EMBEDDINGS", tmp_path/"faces.json")
    old, new = [[1., 0.]]*5, [[.8, .6]]*5
    assert face_identity.enroll("Cale", old)
    assert face_identity.enroll("Cale", new)
    assert face_identity._known()["Cale"] == old[-3:]+new


def test_object_fingerprints_are_lazy_and_teach_uses_exact_old_frame(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    import webcam_tracking as tracking
    class Detector:
        def __init__(self, *_):
            pass
        def detect(self, _):
            return [{"kind": "object", "class": "book", "label": "book", "score": .9, "box": [0., 0., 1., 1.]}]
    monkeypatch.setattr(tracking, "ObjectDetector", Detector)
    engine = SceneTracker(tmp_path)
    calls, learned = [], []
    monkeypatch.setattr(engine.memory, "signature", lambda frame, box: (calls.append(frame), {"marker": int(frame[0, 0, 0])})[1])
    monkeypatch.setattr(engine.memory, "learn", lambda *args: learned.append(args))
    old = np.zeros((100, 100, 3), np.uint8)
    shown = engine.process(old, [], None)
    engine.process(np.ones_like(old), [], None)
    assert calls == []
    write_json(tmp_path/"Media/webcam-learn-request.json", {"frame_id": shown["frame_id"], "id": shown["items"][0]["id"], "name": "My book"})
    assert "Remembered" in engine.handle_request()
    assert calls[0] is old and learned == [("My book", "book", {"marker": 0})]


def test_webcam_image_surface_is_reused_across_new_frames(tmp_path):
    import base64
    import io
    import tkinter as tk

    from PIL import Image
    root = tk.Tk()
    root.withdraw()
    try:
        view = TrackingView(root, tmp_path, lambda: 1, lambda: True)
        view.dialog = tk.Toplevel(root)
        view.dialog.withdraw()
        view.canvas = tk.Canvas(view.dialog, width=320, height=180)
        view.status = tk.StringVar(root)
        def result(color, token):
            raw = io.BytesIO()
            Image.new("RGB", (320, 180), color).save(raw, format="JPEG")
            return {"image": base64.b64encode(raw.getvalue()).decode(), "frame_id": token, "items": []}
        view.result = result("red", "first")
        view.draw()
        photo, canvas_item, decoded = view.photo, view.image_item, view.decoded
        view.draw()
        assert view.decoded is decoded
        view.result = result("blue", "second")
        view.draw()
        assert view.photo is photo and view.image_item == canvas_item
        assert len(view.canvas.find_all()) == 1
        view.clear()
        assert view.photo is None and view.image_item is None
    finally:
        root.destroy()


def test_automatic_zeb_enrollment_requires_cale_and_same_new_face():
    learner = AutoEnrollment()
    request = {"name": "Zeb", "alongside": "Cale", "until": 100}
    cale = {"name": "Cale", "feature": [1., 0.], "box": [.1, .2, .2, .3]}
    zeb = {"name": None, "feature": [0., 1.], "box": [.6, .2, .2, .3]}
    for _ in range(10):
        assert learner.observe([zeb], request, {"Cale": []}, 10) is None
    for _ in range(4):
        assert learner.observe([cale, zeb], request, {"Cale": []}, 10) is None
    assert learner.observe([cale, zeb], request, {"Cale": []}, 10) == ("Zeb", [[0., 1.]]*5)
    assert learner.observe([cale, zeb], request, {"Cale": [], "Zeb": []}, 10) is None


def test_automatic_learning_resets_for_extra_faces_overlap_or_expiry():
    learner = AutoEnrollment()
    request = {"name": "Zeb", "alongside": "Cale", "until": 100}
    cale = {"name": "Cale", "feature": [1., 0.], "box": [.1, .2, .2, .3]}
    zeb = {"name": None, "feature": [0., 1.], "box": [.6, .2, .2, .3]}
    learner.observe([cale, zeb], request, {"Cale": []}, 10)
    assert len(learner.samples) == 1
    assert learner.observe([cale, zeb, zeb], request, {"Cale": []}, 10) is None
    assert not learner.samples
    assert learner.observe([cale, {**zeb, "box": cale["box"]}], request, {"Cale": []}, 10) is None
    assert not learner.samples
    assert learner.observe([cale, zeb], request, {"Cale": []}, 101) is None


def test_nanodet_three_output_levels_and_letterbox_coordinates():
    cv = pytest.importorskip("cv2")
    from webcam_tracking import ObjectDetector
    outputs = []
    for stride in (8, 16, 32):
        size = 416//stride
        probabilities = np.zeros((1, size*size, 80), np.float32)
        distances = np.zeros((1, size*size, 32), np.float32)
        if stride == 8:
            probabilities[0, (size//2)*size+size//2, 0] = .9
        outputs.extend([probabilities, distances])
    detector = ObjectDetector.__new__(ObjectDetector)
    detector.cv, detector.np = cv, np
    detector.net = SimpleNamespace(setInput=lambda _: None, getUnconnectedOutLayersNames=lambda: [], forward=lambda _: outputs)
    found = detector.detect(np.zeros((540, 960, 3), np.uint8))
    assert len(found) == 1 and found[0]["class"] == "person"
    x, y, w, h = found[0]["box"]
    assert 0 <= x < x+w <= 1 and 0 <= y < y+h <= 1
    assert abs((x+w/2)-.5) < .02


def test_selected_face_is_not_another_person_in_the_same_seat():
    cale, zeb = [1., 0., 0.], [0., 1., 0.]
    assert selected_enrollment_feature([zeb, cale], cale) == cale
    assert selected_enrollment_feature([zeb], cale) is None
    assert selected_enrollment_feature([[.72, .69, 0], [.71, .70, 0]], cale) is None
    assert selected_enrollment_feature([[float("nan"), 0, 0]], cale) is None


def test_geometry_ids_never_carry_an_old_person_name():
    tracks = Tracks()
    first = tracks.update([{"class": "person", "box": [.1, .1, .3, .7], "label": "Cale"}], 1)
    next_frame = tracks.update([{"class": "person", "box": [.11, .1, .3, .7], "label": "person"}], 1.4)
    assert next_frame[0]["id"] == first[0]["id"]
    assert next_frame[0]["label"] == "person"
    returned = tracks.update([{"class": "person", "box": [.11, .1, .3, .7]}], 4)
    assert returned[0]["id"] != first[0]["id"]


def test_face_learning_uses_selected_snapshot_not_new_arrival(tmp_path, monkeypatch):
    import face_identity
    tracker = SceneTracker.__new__(SceneTracker)
    tracker.root = tmp_path
    tracker.cache = {"shown": {"at": time.monotonic(), "items": {"face-0": {"class": "face", "feature": [1., 0.]}}}}
    calls = []
    monkeypatch.setattr(face_identity, "request_enrollment", lambda name, reference: calls.append((name, reference)))
    write_json(tmp_path/"Media/webcam-learn-request.json", {"frame_id": "shown", "id": "face-0", "name": "Cale"})
    assert "Learning Cale" in tracker.handle_request()
    assert calls == [("Cale", [1., 0.])]
    assert not (tmp_path/"Media/webcam-learn-request.json").exists()


def test_expired_snapshot_never_enrolls_a_new_person(tmp_path):
    tracker = SceneTracker.__new__(SceneTracker)
    tracker.root, tracker.cache = tmp_path, {"old": {"at": time.monotonic()-21, "items": {}}}
    write_json(tmp_path/"Media/webcam-learn-request.json", {"frame_id": "old", "id": "face-0", "name": "Zeb"})
    assert "expired" in tracker.handle_request()


def test_multiple_visible_faces_require_selection():
    notices, submissions = [], []
    view = SimpleNamespace(result={"items": [{"id": "a", "kind": "face"}, {"id": "b", "kind": "face"}]},
                           selected=None, notice=notices.append, submit=lambda *args: submissions.append(args))
    TrackingView.learn_person(view, "Zeb")
    assert not submissions and "right face" in notices[0]
    view.selected = "b"
    TrackingView.learn_person(view, "Zeb")
    assert submissions[0][0]["id"] == "b"


def test_quick_learn_zeb_cannot_relabel_known_cale():
    notices, submissions = [], []
    view = SimpleNamespace(result={"items": [{"id": "a", "kind": "face", "remembered": True, "label": "Cale"}]},
                           selected=None, notice=notices.append, submit=lambda *args: submissions.append(args))
    TrackingView.learn_person(view, "Zeb")
    assert not submissions and "already learned as Cale" in notices[0]


def test_distinctive_object_memory_survives_restart_without_images(tmp_path):
    pytest.importorskip("cv2")
    rng = np.random.default_rng(42)
    frame = rng.integers(0, 255, (320, 320, 3), dtype=np.uint8)
    memory = ObjectMemory(tmp_path)
    signature = memory.signature(frame, [0, 0, 1, 1])
    memory.learn("Cale's notebook", "book", signature)
    reopened = ObjectMemory(tmp_path)
    assert reopened.identify("book", signature) == "Cale's notebook"
    other = memory.signature(rng.integers(0, 255, frame.shape, dtype=np.uint8), [0, 0, 1, 1])
    assert reopened.identify("book", other) is None
    assert reopened.identify("cup", signature) is None
    data = json.loads(memory.path.read_text())
    assert set(data["Cale's notebook"][0]) == {"class", "points", "descriptors"}
    assert list(tmp_path.rglob("*jpg")) == []


def test_people_cannot_be_taught_as_objects_and_plain_objects_stay_unknown(tmp_path):
    pytest.importorskip("cv2")
    memory = ObjectMemory(tmp_path)
    assert memory.signature(np.zeros((320, 320, 3), dtype=np.uint8), [0, 0, 1, 1]) is None
    with pytest.raises(ValueError):
        memory.learn("Zeb", "person", {"points": [], "descriptors": []})
    with pytest.raises(ValueError):
        memory.learn("cup", "cup", None)


def test_ambiguous_object_names_do_not_claim_identity(tmp_path):
    pytest.importorskip("cv2")
    memory = ObjectMemory(tmp_path)
    frame = np.random.default_rng(4).integers(0, 255, (320, 320, 3), dtype=np.uint8)
    signature = memory.signature(frame, [0, 0, 1, 1])
    memory.learn("First", "book", signature)
    memory.learn("Second", "book", signature)
    assert memory.identify("book", signature) is None
