"""An upgraded legacy install must still be able to launch its model service."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "read_settings", Path(__file__).resolve().parents[1] / "read-settings.py"
)
read_settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(read_settings)


def _config(model, *, key="LOCAL_AI_ROUTER_KEY", url="http://127.0.0.1:11435/v1"):
    return {
        "model": {
            "provider": "custom", "base_url": url, "default": model,
            "key_env": key, "context_length": 65536,
        },
        "auxiliary": {
            "vision": {
                "provider": "custom", "base_url": "http://127.0.0.1:11434/v1",
                "model": "cale-vision",
            }
        },
    }


@pytest.mark.parametrize("model", ["cale-local", "qwen3-coder-local"])
def test_current_and_legacy_router_aliases_work(model):
    assert read_settings.validate_config(_config(model))["model"] == model


@pytest.mark.parametrize("config", [
    _config("qwen3-coder-local", key="OTHER_KEY"),
    _config("unknown-model"),
    _config("cale-local", url="https://example.com/v1"),
    {**_config("cale-local"), "model": {**_config("cale-local")["model"], "context_length": 32768}},
])
def test_untrusted_or_unauthenticated_routes_are_rejected(config):
    with pytest.raises(ValueError):
        read_settings.validate_config(config)
