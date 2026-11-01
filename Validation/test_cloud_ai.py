"""Checks use fake upstreams and temporary DPAPI files, never a real cloud account."""
import hashlib
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_ai as cloud

ANSWER = {'id': 'test', 'created': 1, 'model': 'free-test', 'choices': [
    {'index': 0, 'message': {'role': 'assistant', 'content': 'checked'}, 'finish_reason': 'stop'}]}
REQUEST = {'model': cloud.LOCAL_MODEL, 'messages': [{'role': 'user', 'content': 'test'}]}


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = patch.object(cloud, 'STATE', Path(self.temp.name)); self.state.start()
        self.root = patch.object(cloud, 'ROOT', Path(self.temp.name)); self.root.start()
        self.addCleanup(self.temp.cleanup); self.addCleanup(self.state.stop); self.addCleanup(self.root.stop)
    def connected(self):
        data = cloud.credentials(); data['cloud_key'] = 'sk-or-test-secret'; cloud.save_credentials(data)

    def test_credentials_encrypted_for_windows_user(self):
        self.connected()
        self.assertNotIn(b'sk-or-test-secret', (cloud.STATE / 'credentials.dpapi').read_bytes())
        self.assertEqual(cloud.credentials()['cloud_key'], 'sk-or-test-secret')
        token = cloud.credentials()['router_token']; cloud.disconnect()
        self.assertEqual(cloud.credentials(), {'router_token': token, 'cloud_key': ''})

    def test_disconnected_never_calls_cloud(self):
        with patch.object(cloud, 'post', return_value=ANSWER) as post:
            cloud.Router().complete(REQUEST)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].startswith('http://127.0.0.1:11434/'))

    def test_paid_model_rejected_before_network(self):
        self.connected()
        with patch.object(cloud, 'post') as post:
            with self.assertRaises(ValueError): cloud.Router().complete(dict(REQUEST, model='paid/model'))
        post.assert_not_called()

    def test_cloud_forces_free_removes_paid_plugins_and_fallbacks(self):
        self.connected()
        request = dict(REQUEST, plugins=[{'id': 'web'}], models=['paid/model'], provider={'max_price': {'prompt': 100}})
        with patch.object(cloud, 'post', return_value=ANSWER) as post:
            cloud.Router().complete(request)
        body = post.call_args.args[1]
        self.assertEqual(body['model'], 'openrouter/free')
        self.assertNotIn('models', body); self.assertNotIn('plugins', body)
        self.assertTrue(all(v == 0 for v in body['provider']['max_price'].values()))
        self.assertEqual(post.call_count, 1)

    def test_quota_http_error_falls_back_without_cloud_key(self):
        self.connected()
        with patch.object(cloud, 'post', side_effect=[HTTPError('cloud', 429, 'quota', {}, None), ANSWER, ANSWER]) as post:
            router = cloud.Router(); router.complete(REQUEST); router.complete(REQUEST)
        self.assertEqual(post.call_count, 3)
        self.assertTrue(post.call_args_list[1].args[0].startswith('http://127.0.0.1:11434/'))
        self.assertEqual(len(post.call_args_list[1].args), 2)

    def test_invalid_cloud_response_falls_back(self):
        self.connected()
        with patch.object(cloud, 'post', side_effect=[{'error': 'bad'}, ANSWER]) as post:
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        self.assertEqual(post.call_count, 2)

    def test_permissive_profile_stays_local_even_when_cloud_connected(self):
        self.connected()
        (cloud.STATE / 'model-choice.json').write_text('{"profile":"permissive"}')
        with patch.object(cloud, 'post', return_value=ANSWER) as post:
            cloud.Router().complete(REQUEST)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].startswith('http://127.0.0.1:11434/'))
        self.assertEqual(post.call_args.args[1]['model'], cloud.PERMISSIVE_MODEL)

    def test_daily_budget_persists_across_router_restart(self):
        self.connected()
        import time
        (cloud.STATE / 'quota.json').write_text(json.dumps({'day': time.strftime('%Y-%m-%d', time.gmtime()), 'attempts': 50}))
        with patch.object(cloud, 'post', return_value=ANSWER) as post:
            cloud.Router().complete(REQUEST)
        self.assertTrue(post.call_args.args[0].startswith('http://127.0.0.1:11434/'))

    def test_cloud_unreachable_and_local_fails_propagates_failure(self):
        self.connected()
        with patch.object(cloud, 'post', side_effect=OSError('offline')):
            with self.assertRaises(OSError): cloud.Router().complete(REQUEST)

    def test_callback_rejects_wrong_state_and_path(self):
        for path in ('/connect?state=wrong&code=x', '/wrong?state=good&code=x', '/connect?state=good'):
            with self.assertRaises(ValueError): cloud.callback_code(path, '/connect', 'good')
        self.assertEqual(cloud.callback_code('/connect?state=good&code=once', '/connect', 'good'), 'once')

    def test_browser_oauth_callback_pkce_and_encrypted_save(self):
        observed = {}; threads = []
        def browser(url):
            params = parse_qs(urlsplit(url).query); observed.update(params)
            callback = params['callback_url'][0] + '?code=test-once&state=' + params['state'][0]
            def visit():
                with urlopen(callback, timeout=10) as response: observed['page'] = response.read().decode()
            thread = threading.Thread(target=visit); threads.append(thread); thread.start(); return True
        def exchange(url, payload, **kwargs):
            self.assertEqual(url, cloud.API + '/auth/keys')
            actual = base64.urlsafe_b64encode(hashlib.sha256(payload['code_verifier'].encode()).digest()).rstrip(b'=').decode()
            self.assertEqual(actual, observed['code_challenge'][0])
            self.assertEqual(payload['code'], 'test-once')
            return {'key': 'sk-or-test-oauth-key'}
        with patch.object(cloud.webbrowser, 'open', side_effect=browser), patch.object(cloud, 'post', side_effect=exchange):
            cloud.connect_account()
        for thread in threads: thread.join(10)
        self.assertIn('Connected', observed['page'])
        self.assertEqual(cloud.credentials()['cloud_key'], 'sk-or-test-oauth-key')

    def test_stream_tool_calls_have_indices_and_preserve_arguments(self):
        result = dict(ANSWER, choices=[{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"x"}'}}]}, 'finish_reason': 'tool_calls'}])
        cloud.valid_completion(result)
        call = list(cloud.stream_chunks(result))[0]['choices'][0]['delta']['tool_calls'][0]
        self.assertEqual(call['index'], 0); self.assertEqual(json.loads(call['function']['arguments']), {'path': 'x'})


if __name__ == '__main__': unittest.main(verbosity=2)
