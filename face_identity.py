"""Offline opt-in face enrollment using OpenCV Zoo YuNet and SFace embeddings."""

import hashlib
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "Media/models/face"
EMBEDDINGS = ROOT / "Media/face-embeddings.json"
ENROLL_REQUEST = ROOT / "Media/face-enroll-request.json"
FILES = {
    "yunet.onnx": (
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "sface.onnx": (
        "face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


def ensure_models():
    """Download fixed, hash-checked model files only when recognition is enabled."""
    MODELS.mkdir(parents=True, exist_ok=True)
    for name, (remote, expected) in FILES.items():
        target = MODELS / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        temp = target.with_suffix(".download")
        try:
            urllib.request.urlretrieve(
                "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/" + remote,
                temp,
            )
            if hashlib.sha256(temp.read_bytes()).hexdigest() != expected:
                raise ValueError("Downloaded face model failed verification.")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)


def _known():
    try:
        value = json.loads(EMBEDDINGS.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class FaceModel:
    """Detect, align, and compare faces without storing camera frames."""

    def __init__(self):
        import cv2

        ensure_models()
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(str(MODELS / "yunet.onnx"), "", (320, 320), 0.85)
        self.recognizer = cv2.FaceRecognizerSF.create(str(MODELS / "sface.onnx"), "")

    def features(self, frame):
        """Return one normalized embedding per visible face, up to eight."""
        return [item['feature'] for item in self.observations(frame)]

    def observations(self, frame):
        """Keep valid embeddings aligned with optional, RAM-only normalized boxes.

        Geometry is presentation-continuity evidence, never a name or an
        enrollment label. Legacy features() callers still receive vectors only.
        """
        import numpy as np

        height, width = frame.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(frame)
        if faces is None:
            return []
        results = []
        for face in faces[:8]:
            try:
                aligned = self.recognizer.alignCrop(frame, face)
                vector = self.recognizer.feature(aligned).flatten().astype(float)
                norm = np.linalg.norm(vector)
            except (self.cv2.error, ValueError):
                continue  # One blurred face must not disable the whole camera.
            if np.isfinite(norm) and norm > 0:
                box=None
                if len(face)>=4 and width>0 and height>0 and np.isfinite(face[:4]).all():
                    x,y,w,h=map(float,face[:4])
                    left,top=max(0.,x/width),max(0.,y/height)
                    right,bottom=min(1.,(x+w)/width),min(1.,(y+h)/height)
                    if right>left and bottom>top:
                        box=[left,top,right-left,bottom-top]
                results.append({'feature':(vector / norm).tolist(),'box':box})
        return results

    def feature(self, frame):
        """Enrollment requires exactly one visible face to avoid a wrong label."""
        found = self.features(frame)
        return found[0] if len(found) == 1 else None

    def identify(self, feature):
        """Require a conservative cosine similarity and a gap to the runner-up."""
        import numpy as np

        if feature is None:
            return None
        candidate = np.asarray(feature)
        scores = []
        for name, examples in _known().items():
            if not isinstance(examples, list):
                continue
            for sample in examples[:8]:
                if len(sample) == len(candidate):
                    scores.append((float(np.dot(candidate, np.asarray(sample))), name))
        if not scores:
            return None
        by_name = {}
        for score, name in scores:
            by_name[name] = max(score, by_name.get(name, -1))
        ranked = sorted(by_name.items(), key=lambda pair: pair[1], reverse=True)
        if ranked[0][1] < 0.55 or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.08):
            return None
        return ranked[0][0]


def enroll(name, features):
    """Keep at most eight examples for one voluntarily named person."""
    examples = [item for item in features if isinstance(item, list)]
    if len(examples) < 3:
        return False
    known = _known()
    known[str(name)] = examples[-8:]
    EMBEDDINGS.parent.mkdir(exist_ok=True)
    temp = EMBEDDINGS.with_suffix(".tmp")
    temp.write_text(json.dumps(known), encoding="utf-8")
    os.replace(temp, EMBEDDINGS)
    return True


def request_enrollment(name, reference=None):
    ENROLL_REQUEST.parent.mkdir(exist_ok=True)
    value = {"name": str(name).strip()[:60]}
    if reference is not None:
        value["reference"] = reference
    ENROLL_REQUEST.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
