"""GitHub connection must use the person's login without reading a token."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_account


class GitHubAccountTests(unittest.TestCase):
    def test_account_returns_only_valid_login(self):
        with (
            patch.object(github_account, "executable", return_value="gh.exe"),
            patch.object(
                github_account.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="CaleMediaDev\n"),
            ) as run,
        ):
            self.assertEqual(github_account.account(), "CaleMediaDev")
            self.assertEqual(
                run.call_args.args[0], ["gh.exe", "api", "user", "--jq", ".login"]
            )

    def test_account_never_returns_error_text_or_token(self):
        with (
            patch.object(github_account, "executable", return_value="gh.exe"),
            patch.object(
                github_account.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="ghp_secret\n"),
            ),
        ):
            self.assertEqual(github_account.account(), "")

    def test_connect_uses_interactive_browser_login(self):
        with (
            patch.object(github_account, "executable", return_value="gh.exe"),
            patch.object(github_account.subprocess, "Popen") as start,
        ):
            github_account.connect()
            self.assertEqual(
                start.call_args.args[0],
                [
                    "gh.exe",
                    "auth",
                    "login",
                    "--web",
                    "--clipboard",
                    "--git-protocol",
                    "https",
                    "--hostname",
                    "github.com",
                ],
            )


if __name__ == "__main__":
    unittest.main()


def test_github_device_code_is_reported_without_console_or_raw_credentials(tmp_path):
    import subprocess
    import threading
    codes = []
    ready = threading.Event()
    actual = subprocess.Popen
    program = "import sys;sys.stdin.readline();print('! First copy your one-time code: \\x1b[32mABCD-1234\\x1b[0m',flush=True);print('ghp_do_not_forward_this_secret',flush=True)"
    def started(command, **kwargs):
        assert kwargs['creationflags'] == github_account.HIDE
        assert kwargs['stdin'] == subprocess.PIPE and kwargs['stderr'] == subprocess.STDOUT
        assert '--web' in command and '--clipboard' in command
        return actual([sys.executable, '-c', program], **{**kwargs, 'cwd': tmp_path})
    def received(code):
        codes.append(code)
        ready.set()
    with patch.object(github_account, 'executable', return_value='gh.exe'), patch.object(github_account.subprocess, 'Popen', side_effect=started):
        process = github_account.connect(on_code=received)
        assert ready.wait(5)
        assert process.wait(timeout=5) == 0
    assert codes == ['ABCD-1234']
