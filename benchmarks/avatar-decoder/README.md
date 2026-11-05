# Local animated Cale camera

The camera drives an illustrated character; no photographic identity swap or
InsightFace model is used. OpenCV Haar detection crops the driver face in memory.
Pinned LivePortrait motion, appearance and stitching networks drive a standard
ONNX20 warp/decoder graph on NVIDIA CUDA. The generated face is composited back
into the original wide beach artwork. Raw frames are not saved or uploaded.

## Reproducible sources

- LivePortrait MIT source: https://github.com/KwaiVGI/LivePortrait/tree/9b294b3d0536135442ea73cb01e6cb3ca7029dd3
- Official MIT model repository: https://huggingface.co/KlingTeam/LivePortrait/tree/82a4fa6735ca58432b6ce39301b4b9ee066dea47
- Redistribution notice: [LICENSE-LivePortrait.txt](LICENSE-LivePortrait.txt).
- `avatar_video.py` pins each source Git blob and each downloaded model SHA256.
- Separate release asset `Cale-Avatar-Decoder-v1.onnx`: 202085302 bytes,
  SHA256 `d3713a0ac98a248460604852844f44a692d174e337bdb5cda13264ad45328357`.
- `export.py --source VERIFIED_CHECKOUT --portrait assets/cale-talking.webp
  --output OUTPUT.onnx` records an export receipt. The checkpoint files must be
  in the upstream checkout's expected `pretrained_weights` tree. Use trusted,
  hash-verified checkpoints only. Export qualification used Torch 2.9.1+cu128,
  ONNX 1.22, NumPy 2.2.6 and the upstream config. GPU/compiler differences can
  change exported bytes; compare numerical results before changing release pins.

The exporter freezes evaluation spectral normalization in float32, then uses
half-precision learned convolutions with float32 standard volumetric GridSample.
There are two GridSample nodes and no custom operators. ONNX Runtime GPU 1.26.0
matches CUDA 12.8/cuDNN9; it is isolated from the shared Media environment.
Inference disables CPU fallback. This implementation does not download the
upstream InsightFace detection weights that have separate noncommercial terms.

## Qualification and limits

The actual app adapter processed 48 frames of the owner's supplied video:
median 113.51ms/full frame (8.81fps). The isolated app setup then passed actual
CUDA inference at 86.18ms/decoder frame, allocating approximately 1.46GB GPU
memory. These measure different scopes and are not a 30fps claim.

A bounded physical Facecam test captured 448 frames at 1920x1080 and produced
180 generated avatar frames with changing motion. The worker stopped, its GPU
lease was removed, and the prior camera-consent file was restored. No raw face
frames were exported. The USB2 camera connection still prevents native 4K.

The real optional OBS sender accepted 30 generated-art frames at a 1920x1080
letterboxed output and exited cleanly. Device pixel readback, Zoom/meeting apps,
and a fresh PC/driver installation remain unqualified. The separate optional
sender installs pyvirtualcam 0.15.0 (GPL2) and requires an existing OBS driver.
The live-avatar model is 512px; virtual output dimensions do not increase neural
detail. Unsupported runtimes fail closed to artwork. This backend does not yet
support AMD, Intel or CPU-only neural animation.

Setup and live camera processes have explicit owner PID/birth-time checks.
Owned Windows venv launcher/child families are reaped together. Canonical GPU
leases prevent stale markers from masking the current worker; they are routing
headroom, not an exclusive reservation against unrelated applications.
