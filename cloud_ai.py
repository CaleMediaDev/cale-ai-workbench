"""Optional free cloud routing. No paid models, billing, Codex, or password handling.

Installed credentials are Windows DPAPI encrypted for this Windows user.
The loopback API requires its own random bearer token; it never exposes a key.
"""
from __future__ import annotations
import argparse
import base64
import concurrent.futures
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser
import win32crypt
from model_library import CATALOG

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'Cloud'
PORT = 11435
LOCAL_MODEL = 'cale-local'
PERMISSIVE_MODEL = 'cale-permissive'
API = 'https://openrouter.ai/api/v1'
LOCK = threading.RLock()
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def credentials():
    """Use an OS file lock as GUI, launcher and router are separate processes."""
    import msvcrt
    STATE.mkdir(exist_ok=True)
    with (STATE / 'credentials.lock').open('a+b') as lock:
        lock.seek(0); lock.write(b'1'); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            path = STATE / 'credentials.dpapi'
            if path.exists():
                return json.loads(win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1])
            result = {'router_token': secrets.token_urlsafe(32),
                      'cloud_key': ''}
            save_credentials(result)
            return result
        finally:
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def save_credentials(value):
    atomic(STATE / 'credentials.dpapi', win32crypt.CryptProtectData(
        json.dumps(value).encode(), 'Local AI account connection', None, None, None, 0))


def disconnect():
    # An already in-flight cloud request may finish. All following requests are local.
    value = credentials(); value['cloud_key'] = ''; save_credentials(value)
    record('local', 'Disconnected. Future requests use local Qwen.')


def save_openrouter_key(key):
    if not isinstance(key,str) or not key.startswith('sk-or-') or not 20 <= len(key) <= 300:
        raise ValueError('Enter an OpenRouter API key from your account.')
    value=credentials(); value['cloud_key']=key; save_credentials(value)
    record('connected','OpenRouter key saved. Only zero-price routes are enabled.')


def record(backend, reason, model=''):
    event = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'backend': backend,
             'reason': reason, 'model': model}
    with LOCK:
        atomic(STATE / 'status.json', json.dumps(event).encode())
        with (STATE / 'routing.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')


def status():
    connected = bool(credentials().get('cloud_key'))
    try: result = json.loads((STATE / 'status.json').read_text())
    except (OSError, ValueError): result = {}
    result['connected'] = connected
    result['local_profile'] = local_profile()
    return result


def local_profile():
    try:
        profile = json.loads((STATE / 'model-choice.json').read_text()).get('profile')
        return profile if profile in CATALOG else 'main'
    except (OSError, ValueError): return 'main'


def select_profile(profile):
    if profile not in CATALOG: raise ValueError('Unknown model profile')
    model = CATALOG[profile]['alias']
    info = post('http://127.0.0.1:11434/api/show', {'model': model}, timeout=10)
    if info.get('remote_host') or info.get('remote_model'): raise ValueError('This profile requires local model files.')
    if 'tools' not in info.get('capabilities', []): raise ValueError('This model does not advertise tool support.')
    atomic(STATE / 'model-choice.json', json.dumps({'profile': profile}).encode())
    record('local', 'Model selection applies to the next request.', model)


def post(url, payload, key='', timeout=180):
    headers = {'Content-Type': 'application/json'}
    if key: headers['Authorization'] = 'Bearer ' + key
    with urlopen(Request(url, data=json.dumps(payload).encode(), headers=headers), timeout=timeout) as response:
        return json.load(response)


def callback_code(path, expected_path, expected_state):
    parsed = urlsplit(path); params = parse_qs(parsed.query)
    if parsed.path != expected_path or not secrets.compare_digest(params.get('state', [''])[0], expected_state):
        raise ValueError('Account connection callback did not match this sign-in attempt.')
    code = params.get('code', [''])[0]
    if not code or len(code) > 4096: raise ValueError('No valid authorization code received.')
    return code


def connect_account():
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    callback_path = '/connect/' + secrets.token_urlsafe(24)
    done = threading.Event(); outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_): pass  # Callback URLs contain a single-use secret.
        def do_GET(self):
            try:
                code = callback_code(self.path, callback_path, state)
            except ValueError:
                self.send_error(400, 'Invalid or expired sign-in callback'); return
            try:
                data = post(API + '/auth/keys', {'code': code, 'code_verifier': verifier,
                            'code_challenge_method': 'S256'}, timeout=30)
                key = data.get('key', '')
                if not isinstance(key, str) or not key.startswith('sk-or-'):
                    raise ValueError('The account service did not return a valid key.')
                value = credentials(); value['cloud_key'] = key; save_credentials(value)
                record('connected', 'Free cloud enabled; paid routes disabled.')
                message = 'Connected. You can close this tab and return to Local AI. Only free models are enabled.'
                outcome['ok'] = True
            except Exception:
                message = 'Connection failed. Return to Local AI and try Connect free cloud AI again.'
                outcome['ok'] = False
            self.send_response(200); self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store'); self.end_headers()
            self.wfile.write(message.encode()); done.set()

    with ThreadingHTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = 1
        callback = f'http://127.0.0.1:{server.server_port}{callback_path}'
        url = 'https://openrouter.ai/auth?' + urlencode({'callback_url': callback,
              'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': state,
              'key_label': 'Local AI - free models only'})
        if not webbrowser.open(url):
            raise RuntimeError('The browser could not open. Check your default browser and try again.')
        deadline = time.monotonic() + 600
        while not done.is_set() and time.monotonic() < deadline: server.handle_request()
    if not outcome.get('ok'): raise RuntimeError('Sign-in was not completed. You can try again; local AI still works.')


def cloud_payload(payload):
    # Build from an allowlist. Never forward paid plugins, routes, models or presets.
    allowed = ('messages', 'tools', 'tool_choice', 'temperature', 'top_p', 'stop', 'seed',
               'response_format', 'parallel_tool_calls')
    result = {k: payload[k] for k in allowed if k in payload}
    result.update(model='openrouter/free', stream=False,
                  max_tokens=min(int(payload.get('max_tokens') or payload.get('max_completion_tokens') or 8192), 8192),
                  provider={'require_parameters': True,
                            'max_price': {'prompt': 0, 'completion': 0, 'request': 0, 'image': 0}})
    return result


def valid_completion(result):
    if not isinstance(result, dict) or result.get('error') or not result.get('choices'):
        raise ValueError('Invalid inference response')
    message = result['choices'][0].get('message', {})
    if not message.get('content') and not message.get('tool_calls'):
        raise ValueError('Empty inference response')
    for call in message.get('tool_calls', []):
        if not call.get('id') or not call.get('function', {}).get('name'):
            raise ValueError('Invalid tool response')
        json.loads(call['function']['arguments'])
    return result


class Router:
    def __init__(self):
        self.cooldown = 0
        self.chatgpt_cooldown = 0
        self.last_attempt = 0
        self.lock = threading.Lock()

    def reserve_free_request(self):
        with self.lock:
            now = time.time()
            if now < self.cooldown or now - self.last_attempt < 3.1: return False
            today = time.strftime('%Y-%m-%d', time.gmtime())
            try: usage = json.loads((STATE / 'quota.json').read_text())
            except (OSError, ValueError): usage = {}
            count = usage.get('attempts', 0) if usage.get('day') == today else 0
            if count >= 50: return False
            atomic(STATE / 'quota.json', json.dumps({'day': today, 'attempts': count + 1}).encode())
            self.last_attempt = now
            return True

    def complete(self, payload):
        if payload.get('model') not in (LOCAL_MODEL, LOCAL_MODEL + ':latest'):
            raise ValueError('Only the configured local model and automatic free routing are supported.')
        profile = local_profile()
        key = credentials().get('cloud_key') if profile == 'main' else ''
        reason = 'Cloud is not connected.'
        if profile == 'main' and time.time() >= self.chatgpt_cooldown:
            try:
                import chatgpt_auth
                if chatgpt_auth.status()['enabled']:
                    result = valid_completion(chatgpt_auth.complete(payload))
                    record('ChatGPT plan', 'User-enabled ChatGPT plan response received.', result.get('model',''))
                    return result
            except Exception:
                self.chatgpt_cooldown = time.time() + 120
                reason = 'ChatGPT plan unavailable; trying free cloud or local AI.'
        if key and self.reserve_free_request():
            try:
                result = valid_completion(post(API + '/chat/completions', cloud_payload(payload), key, timeout=120))
                record('free cloud', 'Free cloud response received.', result.get('model', 'openrouter/free'))
                return result
            except HTTPError as exc:
                # No response body, keys or project text is written into logs.
                self.cooldown = time.time() + (3600 if exc.code in (401, 402, 403, 429) else 120)
                reason = f'Free cloud HTTP {exc.code}; using local Qwen.'
            except Exception:
                self.cooldown = time.time() + 120
                reason = 'Free cloud unavailable or incompatible; using local Qwen.'
        elif key:
            reason = 'Free quota, rate limit or cooldown reached; using local Qwen.'
        selected_model = CATALOG[profile]['alias']
        if profile != 'main': reason = 'Optional model selected; this profile runs locally.'
        local = dict(payload); local.update(model=selected_model, stream=False)
        local.pop('stream_options', None)
        record('local', reason, selected_model)
        return valid_completion(post('http://127.0.0.1:11434/v1/chat/completions', local, timeout=1800))


def stream_chunks(result):
    common = {k: result[k] for k in ('id', 'created', 'model') if k in result}
    common['object'] = 'chat.completion.chunk'
    choices = []
    for i, choice in enumerate(result['choices']):
        delta = dict(choice['message'])
        if delta.get('tool_calls'):
            delta['tool_calls'] = [dict(call, index=j) for j, call in enumerate(delta['tool_calls'])]
        choices.append({'index': i, 'delta': delta, 'finish_reason': choice.get('finish_reason')})
    yield {**common, 'choices': choices}
    if result.get('usage'): yield {**common, 'choices': [], 'usage': result['usage']}


def serve():
    router = Router(); token = credentials()['router_token']

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def authorized(self):
            return secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)
        def send_json(self, value, code=200):
            data = json.dumps(value).encode(); self.send_response(code)
            self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            if not self.authorized(): self.send_json({'error': 'Authentication required'}, 401); return
            if self.path == '/health': self.send_json({'service': 'local-ai-free-router', **status()})
            elif self.path == '/v1/models': self.send_json({'object': 'list', 'data': [
                {'id': LOCAL_MODEL, 'object': 'model', 'owned_by': 'local-ai'}]})
            else: self.send_json({'error': 'Not found'}, 404)
        def do_POST(self):
            if not self.authorized(): self.send_json({'error': 'Authentication required'}, 401); return
            if self.path != '/v1/chat/completions': self.send_json({'error': 'Not found'}, 404); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16_000_000: raise ValueError()
                payload = json.loads(self.rfile.read(length))
                if payload.get('model') not in (LOCAL_MODEL, LOCAL_MODEL + ':latest'): raise ValueError()
                if not isinstance(payload.get('messages'), list): raise ValueError()
            except (ValueError, AttributeError): self.send_json({'error': 'Invalid request or unsupported model'}, 400); return
            future = POOL.submit(router.complete, payload)
            if not payload.get('stream'):
                try: self.send_json(future.result())
                except Exception: self.send_json({'error': {'message': 'Free cloud and/or local inference unavailable. Check Ollama.', 'type': 'inference_error'}}, 502)
                return
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store'); self.end_headers()
            try:
                # Buffer upstream completion so fallback cannot replay partially emitted tools.
                while not future.done():
                    try: future.result(timeout=10)
                    except concurrent.futures.TimeoutError:
                        self.wfile.write(b': working\n\n'); self.wfile.flush()
                for chunk in stream_chunks(future.result()):
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError): pass
            except Exception:
                self.wfile.write(b'data: {"error":{"message":"Inference unavailable. Check Ollama.","type":"inference_error","code":502}}\n\ndata: [DONE]\n\n')

    with ThreadingHTTPServer(('127.0.0.1', PORT), Handler) as server:
        atomic(STATE / 'router-pid.txt', str(os.getpid()).encode())
        server.serve_forever()


def ensure_router():
    token = credentials()['router_token']
    def health():
        with urlopen(Request(f'http://127.0.0.1:{PORT}/health', headers={'Authorization': 'Bearer ' + token}), timeout=2) as response:
            return json.load(response).get('service') == 'local-ai-free-router'
    try:
        if health(): return token
    except OSError: pass
    pythonw = ROOT / 'Toolbox/python/Scripts/pythonw.exe'
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve()), '--serve'], cwd=ROOT,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(30):
        time.sleep(.2)
        try:
            if health(): return token
        except OSError: pass
    raise RuntimeError('The local AI router could not start on port 11435.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--ensure-token', action='store_true')
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.serve: serve()
    elif args.ensure_token: print(ensure_router())  # Launchers capture; never log this output.
    elif args.connect: connect_account()
    elif args.status: print(json.dumps(status()))
