"""Validate that Hermes uses installed Ollama or the authenticated loopback router."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from model_identity import DEFAULT_MAIN, LEGACY_MAIN


def validate_config(config):
    """Return the main model settings after checking every enabled route."""
    if config.get("fallback_providers"):
        raise ValueError("This launcher requires model fallback to be disabled.")
    context = int(config["model"].get("context_length", 0))
    if context < 64000:
        raise ValueError("Long tasks require at least a 64K model context window.")
    auxiliary = config.get("auxiliary", {})
    if "main" in auxiliary:
        raise ValueError("The auxiliary model name 'main' is reserved for the main route.")
    models = {"main": config["model"], **auxiliary}
    for name, setting in models.items():
        if name != "main" and setting.get("enabled") is False:
            continue
        url = urlsplit(setting.get("base_url", ""))
        model = setting.get("default") if name == "main" else setting.get("model")
        if (
            setting.get("provider") != "custom"
            or url.hostname not in ("127.0.0.1", "localhost", "::1")
            or url.port not in ((11434, 11435) if name == "main" else (11434,))
            or url.path.rstrip("/") != "/v1"
            or not model
            or ":cloud" in model.lower()
        ):
            raise ValueError(f"{name} must use an installed local Ollama model.")
        if url.port == 11435 and (
            model not in (
                DEFAULT_MAIN, DEFAULT_MAIN + ":latest",
                LEGACY_MAIN, LEGACY_MAIN + ":latest",
            )
            or setting.get("key_env") != "LOCAL_AI_ROUTER_KEY"
        ):
            raise ValueError(
                "The free router requires its dedicated model and authentication."
            )
    return {
        "model": config["model"]["default"],
        "context_length": context,
    }


def main():
    from ruamel.yaml import YAML

    root = Path(__file__).resolve().parent
    config = YAML(typ="safe").load(
        (root / "data/config.yaml").read_text(encoding="utf-8")
    )
    try:
        print(json.dumps(validate_config(config)))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
