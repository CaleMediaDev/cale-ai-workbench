"""Render a bundled, animated Cale portrait when the assistant speaks."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

SIZE = (320, 132)
CAMERA_SIZE = (320, 132)


def camera_frame(jpeg: bytes) -> Image.Image:
    """Style a live, never-saved webcam frame to sit beside Cale's portrait."""
    with Image.open(BytesIO(jpeg)) as source:
        fitted = ImageOps.fit(
            ImageOps.mirror(source.convert("RGB")),
            (CAMERA_SIZE[0] - 4, CAMERA_SIZE[1] - 4),
            method=Image.Resampling.LANCZOS,
        )
    border = Image.new("RGB", CAMERA_SIZE, "#151d31")
    border.paste(fitted, (2, 2))
    mask = Image.new("L", CAMERA_SIZE, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, CAMERA_SIZE[0] - 1, CAMERA_SIZE[1] - 1),
        radius=15,
        fill=255,
    )
    rounded = Image.new("RGB", CAMERA_SIZE, "#0b1020")
    rounded.paste(border, (0, 0), mask)
    ImageDraw.Draw(rounded).rounded_rectangle(
        (0, 0, CAMERA_SIZE[0] - 1, CAMERA_SIZE[1] - 1),
        radius=15,
        outline="#51e5d0",
        width=2,
    )
    return rounded


def load_frames(path: str | Path) -> tuple[list[Image.Image], list[int]]:
    """Decode a short animated WebP/GIF into bounded, polished UI frames."""
    frames, durations = [], []
    with Image.open(path) as source:
        count = min(getattr(source, "n_frames", 1), 72)
        for index in range(count):
            source.seek(index)
            image = ImageOps.fit(
                source.convert("RGB"),
                (SIZE[0] - 4, SIZE[1] - 4),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.4),
            )
            border = Image.new("RGB", SIZE, "#151d31")
            border.paste(image, (2, 2))
            mask = Image.new("L", SIZE, 0)
            drawing = ImageDraw.Draw(mask)
            drawing.rounded_rectangle(
                (0, 0, SIZE[0] - 1, SIZE[1] - 1), radius=15, fill=255
            )
            rounded = Image.new("RGB", SIZE, "#0b1020")
            rounded.paste(border, (0, 0), mask)
            ImageDraw.Draw(rounded).rounded_rectangle(
                (0, 0, SIZE[0] - 1, SIZE[1] - 1),
                radius=15,
                outline="#6e82ff",
                width=2,
            )
            frames.append(rounded)
            durations.append(max(45, min(int(source.info.get("duration", 90)), 160)))
    return frames, durations


def attach(parent, path: str | Path, speaking_event) -> bool:
    """Replace the heading with video only when a real portrait asset exists."""
    if not Path(path).is_file():
        return False
    import tkinter as tk

    from PIL import ImageTk

    pictures, durations = load_frames(path)
    if not pictures:
        return False
    frames = [ImageTk.PhotoImage(picture) for picture in pictures]
    label = tk.Label(parent, image=frames[0], bg="#0b1020", borderwidth=0)
    label.pack(anchor="w")
    position = [0]

    def update():
        if not label.winfo_exists():
            return
        position[0] = (position[0] + 1) % len(frames) if speaking_event.is_set() else 0
        label.configure(image=frames[position[0]])
        label.after(durations[position[0]], update)

    label.after(durations[0], update)
    label.cale_frames = frames  # Keep Tk image references alive for the window.
    return True


# Neural avatar inputs are immutable; imported code keeps its upstream MIT license.
AVATAR_SOURCE_COMMIT = "9b294b3d0536135442ea73cb01e6cb3ca7029dd3"
AVATAR_MODEL_COMMIT = "82a4fa6735ca58432b6ce39301b4b9ee066dea47"
AVATAR_SOURCE_FILES = {
    "LICENSE": {"blob": "8450f81af95aef497dd4064cdc481c54278df66b", "size": 1417},
    "src/config/__init__.py": {
        "blob": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "size": 0,
    },
    "src/config/argument_config.py": {
        "blob": "055f5f83e49225e0ce9f702b5d3851e4da19e6b0",
        "size": 4969,
    },
    "src/config/base_config.py": {
        "blob": "216b8be50aecc8af4b9d1d2a9401e034dd7769e4",
        "size": 802,
    },
    "src/config/crop_config.py": {
        "blob": "eaf6cc2020088a2c512a965d45a17f570c442d7f",
        "size": 1703,
    },
    "src/config/inference_config.py": {
        "blob": "2d9f365b97ccf541b23e5c65d9b606f2420dcf77",
        "size": 4433,
    },
    "src/config/models.yaml": {
        "blob": "131d1c65025c31e37af9239e211ea14454128a2e",
        "size": 1235,
    },
    "src/gradio_pipeline.py": {
        "blob": "4ccd9517ace91ffc16054cc59ab7c83d00f02eb5",
        "size": 34343,
    },
    "src/live_portrait_pipeline.py": {
        "blob": "2a58d4034d7d1f103572e3a0fc40692bed202696",
        "size": 31901,
    },
    "src/live_portrait_pipeline_animal.py": {
        "blob": "9e8227862abdec68f2add53c0dd1aa2cb6ecba77",
        "size": 11324,
    },
    "src/live_portrait_wrapper.py": {
        "blob": "af95812b5ea8c567b2780fe5723767d7f335c572",
        "size": 16270,
    },
    "src/modules/__init__.py": {
        "blob": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "size": 0,
    },
    "src/modules/appearance_feature_extractor.py": {
        "blob": "8d89e4f18a2fbe58447f52ab4c5e3f2011a4ec80",
        "size": 1974,
    },
    "src/modules/convnextv2.py": {
        "blob": "83ea12662b607854915df8c7abb160b588d330b1",
        "size": 5126,
    },
    "src/modules/dense_motion.py": {
        "blob": "c1a7f9a5a1b5463c4a1ddcee2bf36dcd34735706",
        "size": 5547,
    },
    "src/modules/motion_extractor.py": {
        "blob": "b2982e53c52d9ec1e0bec0453cc05edb51a15d23",
        "size": 1083,
    },
    "src/modules/spade_generator.py": {
        "blob": "147a9aed0c7707fe6ae3d59ce1a30154ef75afcc",
        "size": 2659,
    },
    "src/modules/stitching_retargeting_network.py": {
        "blob": "5f50b7cf5a21cd71c70a7bbaaa4b6b68b4762ea3",
        "size": 1501,
    },
    "src/modules/util.py": {
        "blob": "dc6b925ff4d93dbb89d0d1e593bee15c888c39ee",
        "size": 15937,
    },
    "src/modules/warping_network.py": {
        "blob": "9191a197055a954272ee8ed86c5e34f3f33f9ad5",
        "size": 2846,
    },
    "src/utils/__init__.py": {
        "blob": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "size": 0,
    },
    "src/utils/animal_landmark_runner.py": {
        "blob": "c20300427ca705725e23c0c3f17c62978378fa4e",
        "size": 6104,
    },
    "src/utils/camera.py": {
        "blob": "a3dd942697e1f00a96dc3efc75b883d98b52e525",
        "size": 1809,
    },
    "src/utils/check_windows_port.py": {
        "blob": "c2f9728b59c18454ea330b146bc80f27d92936be",
        "size": 345,
    },
    "src/utils/crop.py": {
        "blob": "fd8b8a47fda1d8ae8909ff24558c96d04cdfde16",
        "size": 13758,
    },
    "src/utils/cropper.py": {
        "blob": "64fd75ec53ebcf9768345f3038ef5d0ea383763a",
        "size": 13021,
    },
    "src/utils/face_analysis_diy.py": {
        "blob": "f13a659134216958da3c7273aabf3b0f96fb320d",
        "size": 3077,
    },
    "src/utils/filter.py": {
        "blob": "a8e27ca2a3c2aa58ac73171cacdcf583bdc42778",
        "size": 767,
    },
    "src/utils/helper.py": {
        "blob": "dda9a690a56a1306c250a7080892e478e19e6805",
        "size": 6189,
    },
    "src/utils/human_landmark_runner.py": {
        "blob": "62c3b96a2f63f1bde901230cb07f42c7a0bfe33b",
        "size": 2995,
    },
    "src/utils/io.py": {
        "blob": "9e4bc69536fdc7c668821eec5ec069c8e3154527",
        "size": 3195,
    },
    "src/utils/resources/clip_embedding_68.pkl": {
        "blob": "d69ca7f8481d3d87ee079bfb51f96d98b68961cc",
        "size": 71352,
    },
    "src/utils/resources/clip_embedding_9.pkl": {
        "blob": "2b1e6aedd5b9c211ec3ca37114a0cec2e2507e96",
        "size": 10923,
    },
    "src/utils/resources/lip_array.pkl": {
        "blob": "61fe803f2326624d775674a72e1318f57577d5b1",
        "size": 658,
    },
    "src/utils/resources/mask_template.png": {
        "blob": "bca6ca5977ba820d0d2c05b3793c6231cc82e715",
        "size": 3470,
    },
    "src/utils/retargeting_utils.py": {
        "blob": "ae2e5f52effe8107503586c9f5a24f39dfdbbbcf",
        "size": 991,
    },
    "src/utils/rprint.py": {
        "blob": "c43a42f9855bbb019725e6c2b6c6c50e6fa4d0c5",
        "size": 248,
    },
    "src/utils/timer.py": {
        "blob": "3570fa45d3ff36376471b82a5b3c02efe46eed98",
        "size": 606,
    },
    "src/utils/video.py": {
        "blob": "2e34e6d896d443ba5c88f89f1a38abca1c5030f9",
        "size": 7339,
    },
    "src/utils/viz.py": {
        "blob": "59443cbf207f3395bee241f63c7acb95b9402530",
        "size": 524,
    },
}
AVATAR_MODEL_FILES = {
    "cale-warp-decoder-opset20-baked.onnx": {
        "sha256": "d3713a0ac98a248460604852844f44a692d174e337bdb5cda13264ad45328357",
        "size": 202085302,
        "url": "https://github.com/CaleMediaDev/cale-ai-workbench/releases/download/v0.5.511/Cale-Avatar-Decoder-v1.onnx",
    },
    "liveportrait/base_models/appearance_feature_extractor.pth": {
        "sha256": "5279bb8654293dbdf327030b397f107237dd9212fb11dd75b83dfb635211ceb5",
        "size": 3387959,
        "url": "https://huggingface.co/KlingTeam/LivePortrait/resolve/82a4fa6735ca58432b6ce39301b4b9ee066dea47/liveportrait/base_models/appearance_feature_extractor.pth",
    },
    "liveportrait/base_models/motion_extractor.pth": {
        "sha256": "251e6a94ad667a1d0c69526d292677165110ef7f0cf0f6d199f0e414e8aa0ca5",
        "size": 112545506,
        "url": "https://huggingface.co/KlingTeam/LivePortrait/resolve/82a4fa6735ca58432b6ce39301b4b9ee066dea47/liveportrait/base_models/motion_extractor.pth",
    },
    "liveportrait/retargeting_models/stitching_retargeting_module.pth": {
        "sha256": "3652d5a3f95099141a56986aaddec92fadf0a73c87a20fac9a2c07c32b28b611",
        "size": 2393098,
        "url": "https://huggingface.co/KlingTeam/LivePortrait/resolve/82a4fa6735ca58432b6ce39301b4b9ee066dea47/liveportrait/retargeting_models/stitching_retargeting_module.pth",
    },
}


def neutral_camera_avatar(portrait_path=None):
    """Return only Cale's illustrated portrait; never accept a webcam fallback."""
    import numpy as np

    path = (
        Path(portrait_path)
        if portrait_path
        else Path(__file__).resolve().parent / "assets/cale-talking.webp"
    )
    with Image.open(path) as image:
        return np.array(image.convert("RGB"))


def verify_avatar_profile(profile_root):
    """Check every executable source and tensor artifact against built-in pins."""
    import hashlib

    root = Path(profile_root).resolve(strict=True)
    source = root / "source"
    # Do not execute unpinned package initializers or stale bytecode from a
    # partially repaired profile. Python may otherwise prefer a cached .pyc.
    for path in source.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Avatar source contains an unexpected linked path")
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if path.suffix == ".pyc" and path.parent.name == "__pycache__":
            path.unlink()
        elif relative not in AVATAR_SOURCE_FILES:
            raise ValueError("Avatar source contains an unverified file: " + relative)
    records = [
        ("source", path, pin, "git") for path, pin in AVATAR_SOURCE_FILES.items()
    ]
    records += [
        ("models", path, pin, "sha256") for path, pin in AVATAR_MODEL_FILES.items()
    ]
    for area, relative, pin, method in records:
        path = root / area / relative
        if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
            raise ValueError(
                "Avatar profile file escaped its owned folder: " + relative
            )
        if path.stat().st_size != pin["size"]:
            raise ValueError("Avatar profile file has the wrong size: " + relative)
        digest = hashlib.sha1() if method == "git" else hashlib.sha256()
        if method == "git":
            digest.update(b"blob " + str(pin["size"]).encode() + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != pin["blob" if method == "git" else "sha256"]:
            raise ValueError("Avatar profile checksum failed: " + relative)
    return root


class CaleAvatar:
    """Drive the illustrated Cale with real local facial motion and head pose.

    Capture and publishing belong to camera_presence. This adapter consumes one
    BGR frame and produces illustrated RGB pixels. It never saves, uploads or
    returns the real camera image. The pinned upstream MIT implementation
    supplies feature/motion extraction; the precompiled standard ONNX decoder
    runs on CUDA with CPU fallback disabled.
    """

    def __init__(self, profile_root, portrait_path=None):
        import importlib
        import sys
        import time

        root = verify_avatar_profile(profile_root)
        source = (root / "source").resolve()
        for name, module in list(sys.modules.items()):
            if name == "src" or name.startswith("src."):
                location = getattr(module, "__file__", None)
                if location and not Path(location).resolve().is_relative_to(source):
                    raise RuntimeError(
                        "Another library already owns the portrait module namespace"
                    )
        sys.path.insert(0, str(source))
        bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            wrapper_type = importlib.import_module(
                "src.live_portrait_wrapper"
            ).LivePortraitWrapper
            config_type = importlib.import_module(
                "src.config.inference_config"
            ).InferenceConfig
            load_model = importlib.import_module("src.utils.helper").load_model
            rotation = importlib.import_module("src.utils.camera").get_rotation_matrix
        finally:
            sys.dont_write_bytecode = bytecode
            sys.path.remove(str(source))
        import cv2
        import numpy as np
        import onnxruntime as ort
        import torch
        import yaml

        if ort.__version__ != "1.26.0":
            raise RuntimeError(
                "The avatar needs its qualified ONNX Runtime 1.26.0 profile"
            )
        if not torch.cuda.is_available() or not str(torch.version.cuda).startswith(
            "12."
        ):
            raise RuntimeError(
                "The neural avatar needs the qualified NVIDIA CUDA 12 runtime"
            )
        self.cv2, self.np, self.torch = cv2, np, torch
        self.rotation = rotation
        self.closed = False
        self.initial_free_bytes = torch.cuda.mem_get_info()[0]
        torch.set_num_threads(4)
        config = config_type(flag_do_torch_compile=False, flag_force_cpu=False)
        with open(config.models_config, encoding="utf-8") as stream:
            architecture = yaml.safe_load(stream)

        # Keep upstream methods, loading only F/M/S. Clients do not need the
        # redundant PyTorch W/G weights because their verified ONNX is bundled
        # as a separate model download.
        class FeaturesOnly(wrapper_type):
            def __init__(self):
                self.inference_cfg, self.device_id = config, 0
                self.device, self.compile = "cuda:0", False
                for attribute, relative, kind in (
                    (
                        "appearance_feature_extractor",
                        "liveportrait/base_models/appearance_feature_extractor.pth",
                        "appearance_feature_extractor",
                    ),
                    (
                        "motion_extractor",
                        "liveportrait/base_models/motion_extractor.pth",
                        "motion_extractor",
                    ),
                    (
                        "stitching_retargeting_module",
                        "liveportrait/retargeting_models/stitching_retargeting_module.pth",
                        "stitching_retargeting_module",
                    ),
                ):
                    setattr(
                        self,
                        attribute,
                        load_model(
                            str(root / "models" / relative),
                            architecture,
                            self.device,
                            kind,
                        ),
                    )

        self.model = FeaturesOnly()
        self.portrait = neutral_camera_avatar(portrait_path)
        height, width = self.portrait.shape[:2]
        self.crop_box = (round(width * 250 / 880), 0, round(width * 610 / 880), height)
        x1, y1, x2, y2 = self.crop_box
        prepared = self.model.prepare_source(self.portrait[y1:y2, x1:x2])
        self.source_info = self.model.get_kp_info(prepared)
        self.source_rotation = rotation(
            self.source_info["pitch"], self.source_info["yaw"], self.source_info["roll"]
        )
        self.source_keys = (
            self.model.transform_keypoint(self.source_info).half().contiguous()
        )
        self.features = self.model.extract_feature_3d(prepared).half().contiguous()
        self.driving_keys = self.source_keys.clone()
        mask = cv2.imread(
            str(source / "src/utils/resources/mask_template.png"), cv2.IMREAD_GRAYSCALE
        )
        if mask is None:
            raise RuntimeError("The verified portrait blend mask could not be decoded")
        self.mask = (
            cv2.resize(mask, (x2 - x1, y2 - y1)).astype(np.float32)[..., None] / 255
        )
        self.detector = cv2.CascadeClassifier(
            str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        )
        if self.detector.empty():
            raise RuntimeError("The local face detector is unavailable")
        ort.preload_dlls(directory=str(Path(torch.__file__).resolve().parent / "lib"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
        self.session = ort.InferenceSession(
            str(root / "models/cale-warp-decoder-opset20-baked.onnx"),
            sess_options=options,
            providers=[
                (
                    "CUDAExecutionProvider",
                    {
                        "cudnn_conv_algo_search": "EXHAUSTIVE",
                        "cudnn_conv_use_max_workspace": "1",
                        "gpu_mem_limit": str(8 * 1024**3),
                        "prefer_nhwc": "1",
                        "enable_cuda_graph": "1",
                    },
                )
            ],
        )
        self.binding = self.session.io_binding()
        for name, tensor in (
            ("features", self.features),
            ("source_keys", self.source_keys),
            ("driving_keys", self.driving_keys),
        ):
            self.binding.bind_input(
                name, "cuda", 0, np.float16, tuple(tensor.shape), tensor.data_ptr()
            )
        self.binding.bind_output("image", "cuda")
        self.initial = self.initial_rotation = self.last_box = None
        self.last_detection = float("-inf")
        self.last_seen = float("-inf")
        self.last_inference_ms = None

    def _decode(self, keys):
        """Reuse fixed GPU buffers; a captured CUDA graph still sees new motion."""
        if self.closed:
            raise RuntimeError("The avatar engine is closed")
        self.driving_keys.copy_(keys.half())
        self.torch.cuda.synchronize()
        self.session.run_with_iobinding(self.binding)
        self.binding.synchronize_outputs()
        output = self.binding.copy_outputs_to_cpu()[0]
        if output.shape != (1, 3, 512, 512) or not self.np.isfinite(output).all():
            raise RuntimeError("The neural avatar produced invalid pixels")
        return self.np.clip(
            output[0].transpose(1, 2, 0).astype(self.np.float32) * 255, 0, 255
        ).astype(self.np.uint8)

    def probe(self):
        """Qualify actual GPU decoding without opening a camera or exporting faces."""
        import statistics
        import time

        durations = []
        for index in range(4):
            started = time.monotonic()
            image = self._decode(self.source_keys)
            if index:
                durations.append((time.monotonic() - started) * 1000)
        latency = statistics.median(durations)
        allocated = max(0, self.initial_free_bytes - self.torch.cuda.mem_get_info()[0])
        return {
            "ok": bool(self.np.isfinite(image).all()),
            "provider": "CUDAExecutionProvider",
            "frames": 4,
            "finite": True,
            "latency_ms": latency,
            "fps": 1000 / latency,
            "gpu_allocation_bytes": allocated,
            "camera_opened": False,
        }

    def render(self, frame, sequence=None, captured_at=None):
        """Track genuine driver motion; face loss always produces the neutral illustration."""
        import time

        if self.closed:
            return None
        if (
            not isinstance(frame, self.np.ndarray)
            or frame.dtype != self.np.uint8
            or frame.ndim != 3
            or frame.shape[2] != 3
        ):
            raise ValueError("Avatar input must be one BGR camera frame")
        now = time.monotonic()
        height, width = frame.shape[:2]
        if not height or not width:
            return self.portrait.copy()
        if now - self.last_detection >= 0.25:
            small = self.cv2.resize(frame, (640, max(1, round(height * 640 / width))))
            faces = self.detector.detectMultiScale(
                self.cv2.cvtColor(small, self.cv2.COLOR_BGR2GRAY),
                1.1,
                5,
                minSize=(55, 55),
            )
            self.last_detection = now
            if len(faces):
                x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
                ratio = width / 640
                side = max(w, h) * ratio * 1.55
                cx, cy = (x + w / 2) * ratio, (y + h / 2) * ratio
                self.last_box = (cx - side / 2, cy - side * 0.53, side)
                self.last_seen = now
        if self.last_box is None or now - self.last_seen > 0.75:
            self.initial = self.initial_rotation = None
            return self.portrait.copy()
        x, y, side = self.last_box
        transform = self.np.float32([[1, 0, -x], [0, 1, -y]])
        crop = self.cv2.warpAffine(
            frame,
            transform,
            (max(1, round(side)), max(1, round(side))),
            borderMode=self.cv2.BORDER_REPLICATE,
        )
        started = time.monotonic()
        info = self.model.get_kp_info(
            self.model.prepare_source(self.cv2.cvtColor(crop, self.cv2.COLOR_BGR2RGB))
        )
        rotation = self.rotation(info["pitch"], info["yaw"], info["roll"])
        if self.initial is None:
            self.initial, self.initial_rotation = info, rotation
        rotation = (
            rotation @ self.initial_rotation.transpose(1, 2) @ self.source_rotation
        )
        expression = self.source_info["exp"] + info["exp"] - self.initial["exp"]
        target = self.source_info["scale"][..., None] * (
            self.source_info["kp"] @ rotation + expression
        )
        target[..., :2] += self.source_info["t"][:, None, :2]
        target = self.model.stitching(self.source_keys.float(), target)
        pixels = self._decode(target)
        self.last_inference_ms = (time.monotonic() - started) * 1000
        if self.closed:
            return None
        result = self.portrait.copy()
        x1, y1, x2, y2 = self.crop_box
        pixels = self.cv2.resize(
            pixels, (x2 - x1, y2 - y1), interpolation=self.cv2.INTER_LANCZOS4
        )
        result[y1:y2, x1:x2] = self.np.clip(
            pixels * self.mask + result[y1:y2, x1:x2] * (1 - self.mask), 0, 255
        ).astype(self.np.uint8)
        return result

    def close(self):
        """Release only this process's avatar sessions and tensors."""
        self.closed = True
        self.binding = self.session = self.model = None
        self.features = self.source_keys = self.driving_keys = None
        self.initial = self.initial_rotation = self.source_info = (
            self.source_rotation
        ) = None
        self.torch.cuda.empty_cache()
