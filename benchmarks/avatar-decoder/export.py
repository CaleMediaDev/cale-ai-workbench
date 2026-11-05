"""Export verified LivePortrait neural W/G as standard ONNX20 with safe CUDA casts."""
import hashlib
import json
from pathlib import Path
import sys
import argparse
import os

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True, help='Verified LivePortrait checkout containing pretrained_weights')
parser.add_argument('--portrait', type=Path, required=True, help='Cale beach portrait, 880x360')
parser.add_argument('--output', type=Path, required=True)
options = parser.parse_args()
ROOT = options.output.resolve().parent
portrait_path = options.portrait.resolve()
source = options.source.resolve()
output = options.output.resolve()
ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(source))
os.chdir(source)  # The pinned upstream wrapper resolves configs/weights here.
import numpy as np
import onnx
from PIL import Image
import torch
import torch.nn.functional as functional
from src.config.inference_config import InferenceConfig
from src.live_portrait_wrapper import LivePortraitWrapper

torch.set_num_threads(4)
model = LivePortraitWrapper(InferenceConfig(flag_do_torch_compile=False))
portrait = Image.open(portrait_path).convert("RGB")
prepared = model.prepare_source(np.array(portrait.crop((250, 0, 610, 360))))
source_keys = model.transform_keypoint(model.get_kp_info(prepared)).half()
features = model.extract_feature_3d(prepared).half()
# Freeze evaluation normalization in float32 before rounding weights to half.
baked_layers = 0
for component in (model.warping_module, model.spade_generator):
    component.eval()
    for layer in component.modules():
        if hasattr(layer, "weight_orig"):
            torch.nn.utils.remove_spectral_norm(layer)
            baked_layers += 1
print("Baked fixed evaluation weights:", baked_layers, flush=True)
model.warping_module.half()
model.spade_generator.half()
class Decoder(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.warp = model.warping_module
        self.decode = model.spade_generator
    def forward(self, feature, source, target):
        return self.decode(feature=self.warp(feature, kp_source=source, kp_driving=target)["out"])

# ORT's standard CUDA volumetric GridSample supports float32. Keep surrounding
# learned convolutions/weights half precision and cast only at that operator.
original = functional.grid_sample
def grid_sample(input, grid, mode="bilinear", padding_mode="zeros", align_corners=None):
    if input.dim() == 5:
        return original(input.float(), grid.float(), mode=mode,
            padding_mode=padding_mode, align_corners=align_corners).to(input.dtype)
    return original(input, grid, mode=mode, padding_mode=padding_mode, align_corners=align_corners)

decoder = Decoder().eval()
functional.grid_sample = grid_sample
try:
    with torch.inference_mode():
        torch.onnx.export(decoder, (features, source_keys, source_keys), str(output),
            input_names=["features", "source_keys", "driving_keys"], output_names=["image"],
            opset_version=20, dynamo=False, do_constant_folding=True)
finally:
    functional.grid_sample = original
graph = onnx.load(str(output))
onnx.checker.check_model(graph)
assert all(not node.domain for node in graph.graph.node)
grids = [node for node in graph.graph.node if node.op_type == "GridSample"]
assert len(grids) == 2
result = {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    "bytes": output.stat().st_size, "opset": 20, "nodes": len(graph.graph.node),
    "standard_grid_sample_nodes": len(grids), "custom_ops": False,
    "source_commit": "9b294b3d0536135442ea73cb01e6cb3ca7029dd3",
    "weights_commit": "82a4fa6735ca58432b6ce39301b4b9ee066dea47", "torch": torch.__version__}
(ROOT / "onnx-export-baked-receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
