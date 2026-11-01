"""Allow installed Ollama or our authenticated, free-only loopback router."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from ruamel.yaml import YAML

root = Path(__file__).resolve().parent
config = YAML(typ='safe').load((root / 'data/config.yaml').read_text(encoding='utf-8'))
if config.get('fallback_providers'):
    raise SystemExit('This launcher requires model fallback to be disabled.')
models = {'main': config['model'], **config.get('auxiliary', {})}
for name, setting in models.items():
    if setting.get('enabled') is False:
        continue
    url = urlsplit(setting.get('base_url', ''))
    model = setting.get('default') if name == 'main' else setting.get('model')
    if (setting.get('provider') != 'custom' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or url.port not in ((11434, 11435) if name == 'main' else (11434,)) or url.path.rstrip('/') != '/v1'
            or not model or ':cloud' in model.lower()):
        raise SystemExit(f'{name} must use an installed local Ollama model.')
    if url.port == 11435 and (model not in ('cale-local', 'cale-local:latest') or setting.get('key_env') != 'LOCAL_AI_ROUTER_KEY'):
        raise SystemExit('The free router requires its dedicated model and authentication.')
print(json.dumps({'model': config['model']['default'], 'context_length': config['model']['context_length']}))
