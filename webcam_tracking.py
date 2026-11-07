"""Local webcam detections and explicitly taught names; no frames stored on disk.

NanoDet decoding follows OpenCV Zoo's Apache-2.0 reference implementation:
https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_nanodet
See vendor/opencv-nanodet/LICENSE. Object memories store ORB descriptors only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_HASH = "4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186"
MODEL_URL = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_nanodet/object_detection_nanodet_2022nov.onnx"
CLASSES = ("person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,"
           "fire hydrant,stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,"
           "elephant,bear,zebra,giraffe,backpack,umbrella,handbag,tie,suitcase,frisbee,"
           "skis,snowboard,sports ball,kite,baseball bat,baseball glove,skateboard,"
           "surfboard,tennis racket,bottle,wine glass,cup,fork,knife,spoon,bowl,banana,"
           "apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,cake,chair,couch,"
           "potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,"
           "cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,"
           "scissors,teddy bear,hair drier,toothbrush").split(",")


def read_json(path, fallback=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else ({} if fallback is None else fallback)
    except (OSError, ValueError):
        return {} if fallback is None else fallback


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class JsonCache:
    """Reload named memories immediately after an atomic save or forget."""
    def __init__(self):
        self.entries = {}

    def get(self, path):
        path = Path(path)
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            stamp = None
        previous = self.entries.get(path)
        if previous is None or previous[0] != stamp:
            self.entries[path] = (stamp, read_json(path))
        return self.entries[path][1]


def clean_name(value):
    name = " ".join(str(value).split())[:60]
    if not name or any(ord(char) < 32 for char in name):
        raise ValueError("Enter a short name.")
    return name


def tracking_enabled(root=ROOT):
    return read_json(Path(root)/"Media/webcam-tracking.json").get("enabled") is True


def valid_region(box):
    return (isinstance(box, list) and len(box) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in box)
            and 0 <= box[0] < 1 and 0 <= box[1] < 1
            and box[2] >= .01 and box[3] >= .01
            and box[0]+box[2] <= 1.000001 and box[1]+box[3] <= 1.000001)


class ObjectDetector:
    def __init__(self, root=ROOT):
        import cv2
        import numpy as np

        self.cv, self.np = cv2, np
        path = Path(root)/"Media/models/objects/nanodet.onnx"
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_HASH:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".download")
            try:
                with urllib.request.urlopen(MODEL_URL, timeout=30) as source:
                    raw = source.read(4_000_001)
                if hashlib.sha256(raw).hexdigest() != MODEL_HASH:
                    raise ValueError("Object model verification failed")
                temporary.write_bytes(raw)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        self.net = cv2.dnn.readNet(str(path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

    def detect(self, frame):
        found = self.detect_single(frame)
        people = [item for item in found if item["class"] == "person"
                  and .08 <= item["box"][2]*item["box"][3] <= .75]
        if people:
            # One additional crop, bounded independently of the number of people.
            person = max(people, key=lambda item: item["box"][2]*item["box"][3])
            x, y, w, h = person["box"]
            left, top = max(0, x-.04), max(0, y-.04)
            right_crop = min(1.0, x+w+.04)
            bottom_crop = min(1.0, y+h+.04)
            box = [left, top, right_crop-left, bottom_crop-top]
            held = {"spoon", "fork", "knife", "cup", "bowl", "bottle", "cell phone", "remote", "book", "scissors"}
            found += [item for item in self.detect_region(frame, box) if item["class"] in held]
        return self.merge(found)

    @staticmethod
    def merge(items):
        selected = []
        for item in sorted(items, key=lambda value: -value["score"]):
            if not any(old["class"] == item["class"] and overlap(old["box"], item["box"]) > .4 for old in selected):
                selected.append(item)
        return selected[:20]

    def detect_region(self, frame, box):
        if not valid_region(box):
            return []
        h, w = frame.shape[:2]
        left, top = max(0, round(box[0]*w)), max(0, round(box[1]*h))
        right, bottom = min(w, round((box[0]+box[2])*w)), min(h, round((box[1]+box[3])*h))
        if right-left < 8 or bottom-top < 8:
            return []
        found = self.detect_single(frame[top:bottom, left:right])
        for item in found:
            x, y, bw, bh = item["box"]
            item["box"] = [(left+x*(right-left))/w, (top+y*(bottom-top))/h,
                           bw*(right-left)/w, bh*(bottom-top)/h]
        return found

    def detect_single(self, frame):
        cv, np = self.cv, self.np
        h, w = frame.shape[:2]
        scale = 416/max(h, w)
        nw, nh = max(1, round(w*scale)), max(1, round(h*scale))
        left, top = (416-nw)//2, (416-nh)//2
        image = np.zeros((416, 416, 3), dtype=np.uint8)
        image[top:top+nh, left:left+nw] = cv.resize(cv.cvtColor(frame, cv.COLOR_BGR2RGB), (nw, nh))
        image = image.astype(np.float32)
        image -= np.asarray([103.53, 116.28, 123.675], dtype=np.float32)
        image /= np.asarray([57.375, 57.12, 58.395], dtype=np.float32)
        self.net.setInput(cv.dnn.blobFromImage(image))
        outputs = self.net.forward(self.net.getUnconnectedOutLayersNames())
        boxes, scores, labels = [], [], []
        for level, stride in enumerate((8, 16, 32, 64)[:len(outputs)//2]):
            probs = outputs[2*level].reshape(-1, 80)
            logits = outputs[2*level+1].reshape(-1, 4, 8)
            chosen = np.flatnonzero(probs.max(axis=1) >= .4)
            if not len(chosen):
                continue
            logits = logits[chosen]
            distribution = np.exp(logits-logits.max(axis=2, keepdims=True))
            distances = (distribution/distribution.sum(axis=2, keepdims=True) @ np.arange(8))*stride
            size = 416//stride
            cx = (chosen % size)*stride+(stride-1)/2
            cy = (chosen//size)*stride+(stride-1)/2
            x1 = np.clip((cx-distances[:, 0]-left)/nw, 0, 1)
            y1 = np.clip((cy-distances[:, 1]-top)/nh, 0, 1)
            x2 = np.clip((cx+distances[:, 2]-left)/nw, 0, 1)
            y2 = np.clip((cy+distances[:, 3]-top)/nh, 0, 1)
            valid = (x2 > x1) & (y2 > y1)
            boxes.extend(np.column_stack((x1, y1, x2-x1, y2-y1))[valid].tolist())
            scores.extend(probs[chosen[valid]].max(axis=1).tolist())
            labels.extend(probs[chosen[valid]].argmax(axis=1).tolist())
        found = []
        for category in sorted(set(labels)):
            candidates = [i for i, label in enumerate(labels) if label == category]
            keep = cv.dnn.NMSBoxes([boxes[i] for i in candidates], [scores[i] for i in candidates], .4, .5)
            for offset in np.asarray(keep).reshape(-1):
                i = candidates[int(offset)]
                found.append({"kind": "object", "class": CLASSES[category], "label": CLASSES[category],
                              "box": boxes[i], "score": scores[i]})
        return sorted(found, key=lambda item: -item["score"])[:20]


def overlap(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0]+a[2], b[0]+b[2]), min(a[1]+a[3], b[1]+b[3])
    intersection = max(0, right-left)*max(0, bottom-top)
    return intersection/max(1e-9, a[2]*a[3]+b[2]*b[3]-intersection)


class Tracks:
    """Short-lived geometry IDs; geometry never assigns a person's name."""
    def __init__(self):
        self.previous, self.counter = [], 0

    def update(self, objects, now):
        previous = [item for item in self.previous if now-item["at"] < 1.5]
        used = set()
        for item in objects:
            choices = [(overlap(item["box"], old["box"]), old) for old in previous
                       if old["id"] not in used and old["class"] == item["class"]]
            match = max(choices, key=lambda pair: pair[0], default=(0, None))
            if match[0] >= .3:
                item["id"] = match[1]["id"]
                item["hits"] = match[1].get("hits", 1)+1
            else:
                self.counter += 1
                item["id"] = str(self.counter)
                item["hits"] = 1
            used.add(item["id"])
            item["at"] = now
        self.previous = [dict(item) for item in objects] + [old for old in previous if old["id"] not in used]
        return objects


class ObjectMemory:
    def __init__(self, root=ROOT):
        import cv2
        self.cv = cv2
        self.path = Path(root)/"Media/object-memories.json"
        self.orb = cv2.ORB_create(nfeatures=350)
        self.saved = JsonCache()

    def known(self):
        return self.saved.get(self.path)

    def has_category(self, category):
        return any(example.get("class") == category for examples in self.known().values()
                   if isinstance(examples, list) for example in examples[:4] if isinstance(example, dict))

    def signature(self, frame, box):
        h, w = frame.shape[:2]
        x, y, bw, bh = box
        crop = frame[max(0, int(y*h)):min(h, int((y+bh)*h)), max(0, int(x*w)):min(w, int((x+bw)*w))]
        if not crop.size:
            return None
        crop = self.cv.resize(crop, (320, 320))
        points, descriptors = self.orb.detectAndCompute(self.cv.cvtColor(crop, self.cv.COLOR_BGR2GRAY), None)
        if descriptors is None or len(points) < 20:
            return None
        return {"points": [list(point.pt) for point in points], "descriptors": descriptors.tolist()}

    def learn(self, name, category, signature):
        if category == "person":
            raise ValueError("Use face learning for people.")
        if not signature:
            raise ValueError("This object has too little visible detail to remember reliably. Try a closer view or a different angle.")
        name = clean_name(name)
        known = read_json(self.path)
        if name not in known and len(known) >= 100:
            raise ValueError("Object memory is full. Forget an old object first.")
        examples = known.get(name, [])
        known[name] = (examples + [{"class": category, **signature}])[-4:]
        write_json(self.path, known)

    def identify(self, category, signature):
        if not signature or category == "person":
            return None
        import numpy as np
        cv = self.cv
        ranked = []
        for name, examples in self.known().items():
            if not isinstance(examples, list):
                continue
            best = 0
            for example in examples[:4]:
                if not isinstance(example, dict) or example.get("class") != category:
                    continue
                try:
                    pairs = cv.BFMatcher(cv.NORM_HAMMING).knnMatch(
                        np.asarray(example["descriptors"], dtype=np.uint8),
                        np.asarray(signature["descriptors"], dtype=np.uint8), k=2)
                    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < .7*pair[1].distance]
                    if len(good) < 12:
                        continue
                    source = np.float32([example["points"][m.queryIdx] for m in good])
                    target = np.float32([signature["points"][m.trainIdx] for m in good])
                    _, mask = cv.findHomography(source, target, cv.RANSAC, 4)
                    count = int(mask.sum()) if mask is not None else 0
                    if count >= 10 and count/len(good) >= .65:
                        best = max(best, count)
                except (cv.error, ValueError, KeyError, IndexError, TypeError):
                    continue
            ranked.append((best, name))
        ranked.sort(reverse=True)
        if ranked and ranked[0][0] >= 10 and (len(ranked) == 1 or ranked[0][0]-ranked[1][0] >= 5):
            return ranked[0][1]
        return None


class AutoEnrollment:
    """One explicitly named newcomer, only beside an already recognized person."""
    def __init__(self):
        self.samples = []
        self.request = None

    def observe(self, faces, request, known, now):
        from camera_presence import selected_enrollment_feature
        key = (request.get("name"), request.get("alongside"), request.get("until"))
        if key != self.request:
            self.samples, self.request = [], key
        name, alongside, until = key
        if (not name or not alongside or name == alongside or name in known
                or type(until) not in (int, float) or now >= until):
            self.samples = []
            return None
        recognized = [face for face in faces if face.get("name") == alongside]
        unknown = [face for face in faces if face.get("name") is None]
        if (len(faces) != 2 or len(recognized) != 1 or len(unknown) != 1
                or not recognized[0].get("box") or not unknown[0].get("box")
                or overlap(recognized[0]["box"], unknown[0]["box"]) > .1):
            self.samples = []
            return None
        feature = unknown[0]["feature"]
        if self.samples and selected_enrollment_feature([feature], self.samples[0]) is None:
            self.samples = []
        self.samples.append(feature)
        if len(self.samples) >= 5:
            samples, self.samples = self.samples, []
            return name, samples
        return None


class SceneTracker:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.detector = ObjectDetector(root)
        self.memory = ObjectMemory(root)
        self.tracks = Tracks()
        self.cache = {}
        self.auto = AutoEnrollment()
        self.saved = JsonCache()

    def process(self, frame, faces, face_model):
        import cv2
        h, w = frame.shape[:2]
        scale = min(1., 960/w, 720/h)
        if scale < 1:
            frame = cv2.resize(frame, (max(1, round(w*scale)), max(1, round(h*scale))))
        now = time.monotonic()
        known_people = self.saved.get(self.root/"Media/face-embeddings.json")
        named_faces = [{**face, "name": face_model.identify(face["feature"]) if face_model else None} for face in faces]
        config_path = self.root/"Media/webcam-tracking.json"
        config = dict(self.saved.get(config_path))
        request = config.get("learn_other", {})
        enrollment = self.auto.observe(named_faces, request, known_people, time.time()) if isinstance(request, dict) else None
        learning = ""
        if enrollment:
            from face_identity import enroll
            if enroll(*enrollment):
                config.pop("learn_other", None)
                write_json(config_path, config)
                known_people = read_json(self.root/"Media/face-embeddings.json")
                learning = "Learned " + enrollment[0] + ". Future recognition is automatic."
                for face in named_faces:
                    face["name"] = face_model.identify(face["feature"])
        elif request and request.get("name") not in known_people and time.time() < request.get("until", 0):
            learning = "Learning " + request["name"] + " automatically beside " + request["alongside"] + f" · {len(self.auto.samples)}/5 samples"
        focus = config.get("focus_box")
        detections = self.detector.detect_region(frame, focus) if valid_region(focus) else self.detector.detect(frame)
        items = self.tracks.update(detections, now)
        # Suppress weak single-frame guesses without hiding strong detections.
        items = [item for item in items if item.get("hits", 1) >= 2 or item.get("score", 0) >= .65]
        private = {}
        for item in items:
            if item["class"] != "person":
                private[item["id"]] = {"box": item["box"], "class": item["class"]}
                name = None
                if self.memory.has_category(item["class"]):
                    signature = self.memory.signature(frame, item["box"])
                    name = self.memory.identify(item["class"], signature)
                if name:
                    item["label"], item["remembered"] = name, True
        for index, face in enumerate(named_faces):
            if not face.get("box"):
                continue
            name = face["name"]
            key = "face-" + str(index)
            items.append({"kind": "face", "class": "face", "id": key, "box": face["box"],
                          "label": name or "Unknown person", "remembered": bool(name)})
            private[key] = {"feature": face["feature"], "class": "face"}
        token = uuid.uuid4().hex
        self.cache = {key: value for key, value in self.cache.items() if now-value["at"] < 20}
        self.cache[token] = {"at": now, "items": private, "frame": frame}
        while len(self.cache) > 32:
            self.cache.pop(next(iter(self.cache)))
        h, w = frame.shape[:2]
        preview = cv2.resize(frame, (min(960, w), max(1, round(h*min(960, w)/w))))
        ok, raw = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return {"frame_id": token, "at": time.time(), "items": items,
                "image": base64.b64encode(raw.tobytes()).decode("ascii") if ok else "",
                "learning": learning,
                "focus_box": focus if valid_region(focus) else None,
                "known_people": list(known_people),
                "known_objects": list(self.memory.known())}

    def handle_request(self):
        path = self.root/"Media/webcam-learn-request.json"
        if not path.is_file():
            return None
        request = read_json(path)
        path.unlink(missing_ok=True)
        try:
            scene = self.cache.get(request.get("frame_id"), {})
            if time.monotonic()-scene.get("at", 0) > 20:
                raise ValueError("That view expired. Select the person or object again.")
            item = scene.get("items", {}).get(request.get("id"))
            if request.get("id") == "selected-area" and valid_region(request.get("box")):
                category = str(request.get("category", "")).lower().strip()
                if category not in CLASSES or category == "person":
                    raise ValueError("Choose a known object type, such as spoon, cup or book.")
                item = {"class": category, "box": request["box"]}
            if not item:
                raise ValueError("Select a visible face or object first.")
            name = clean_name(request.get("name", ""))
            if item["class"] == "face":
                from face_identity import request_enrollment
                request_enrollment(name, reference=item["feature"])
                return "Learning " + name + ": keep that face in view for a few seconds."
            signature = self.memory.signature(scene["frame"], item["box"])
            self.memory.learn(name, item["class"], signature)
            return "Remembered " + name + " locally. Teach another angle to improve matching."
        except (OSError, ValueError, TypeError) as error:
            return str(error)
