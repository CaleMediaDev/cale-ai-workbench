"""Identity teaching, exact-frame requests and bounded local object memory."""

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from camera_presence import selected_enrollment_feature
from webcam_tracking import (
    AutoEnrollment,
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
