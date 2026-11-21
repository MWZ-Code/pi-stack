"""Run bin/pi-web-cli against stub_piweb and assert literal stdout and requests."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from stub_piweb import StubPiWeb

CLI = Path(__file__).resolve().parents[2] / "bin" / "pi-web-cli"
LIVE = "http://127.0.0.1:8504"

LIST_STDOUT = """\
{
  "ok": true,
  "status": 200,
  "sessions": [
    {
      "id": "s1",
      "cwd": "/work"
    }
  ]
}
"""

SPAWN_STDOUT = """\
{
  "ok": true,
  "status": 200,
  "session": {
    "id": "s1"
  }
}
"""

PROMPT_STDOUT = """\
{
  "ok": true,
  "status": 200,
  "result": {
    "accepted": true
  }
}
"""

STATUS_STDOUT = """\
{
  "ok": true,
  "status": 200,
  "session": {
    "sessionId": "sess-1",
    "isStreaming": false
  }
}
"""

COMMANDS_STDOUT = """\
{
  "ok": true,
  "status": 200,
  "commands": [
    {
      "name": "help"
    }
  ]
}
"""

OK_STDOUT = """\
{
  "ok": true,
  "status": 200,
  "result": {
    "ok": true
  }
}
"""

PROMPT_400_STDOUT = """\
{
  "ok": false,
  "status": 400,
  "result": {
    "error": "nope"
  }
}
"""

PROMPT_500_STDOUT = """\
{
  "ok": false,
  "status": 500,
  "result": {
    "error": "down"
  }
}
"""


class PiWebCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pi-web-cli-test-")
        self.addCleanup(temporary.cleanup)
        self.home = temporary.name
        self.stub = StubPiWeb()
        self.stub.start()
        self.assertNotEqual(self.stub.url.rstrip("/"), LIVE)

    def tearDown(self) -> None:
        self.stub.stop()

    def run_cli(self, *args: str, url: str | None = None) -> subprocess.CompletedProcess[str]:
        target = self.stub.url if url is None else url
        if target.rstrip("/") == LIVE:
            raise AssertionError("refusing to call the live PI WEB server")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": self.home,
            "PI_WEB_URL": target,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        return subprocess.run(
            [os.fspath(CLI), *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def assert_completed(self, proc: subprocess.CompletedProcess[str], code: int, stdout: str) -> None:
        self.assertEqual(proc.returncode, code)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout, stdout)

    def assert_request(self, method: str, path: str, body: object, query: str = "") -> None:
        self.assertEqual(self.stub.requests, [(method, path, query, body)])

    def test_list(self) -> None:
        self.stub.set_routes([("GET", "/api/sessions", 200, [{"id": "s1", "cwd": "/work"}])])
        proc = self.run_cli("list", "--cwd", "/work")
        self.assert_completed(proc, 0, LIST_STDOUT)
        self.assert_request("GET", "/api/sessions", None, "cwd=%2Fwork")

    def test_spawn(self) -> None:
        self.stub.set_routes([("POST", "/api/sessions", 200, {"id": "s1"})])
        proc = self.run_cli("spawn", "/work")
        self.assert_completed(proc, 0, SPAWN_STDOUT)
        self.assert_request("POST", "/api/sessions", {"cwd": "/work"})

    def test_spawned_session_uses_its_cwd_across_invocations(self) -> None:
        cwd = "/stream + #1"
        path = "/api/sessions/sess-1"
        self.stub.required_session_cwds["sess-1"] = cwd
        self.stub.set_routes([
            ("POST", "/api/sessions", 200, {"id": "sess-1", "cwd": cwd}),
            ("GET", f"{path}/location", 404, {"error": "Session not found"}),
            ("GET", f"{path}/status", 200, {"sessionId": "sess-1", "isStreaming": False}),
            ("POST", f"{path}/prompt", 200, {"accepted": True}),
        ])
        self.assertEqual(self.run_cli("spawn", cwd).returncode, 0)
        for argv, stdout, request in (
            (("model", "acme/org/model-1"), OK_STDOUT, ("POST", f"{path}/model", "", {"cwd": cwd, "provider": "acme", "modelId": "org/model-1"})),
            (("thinking-level", "xhigh"), OK_STDOUT, ("POST", f"{path}/thinking-level", "", {"cwd": cwd, "level": "xhigh"})),
            (("status",), STATUS_STDOUT, ("GET", f"{path}/status", "cwd=%2Fstream+%2B+%231", None)),
            (("prompt", "hi", "--steer"), PROMPT_STDOUT, ("POST", f"{path}/prompt", "", {"cwd": cwd, "text": "hi", "streamingBehavior": "steer"})),
            (("messages",), OK_STDOUT, ("GET", f"{path}/messages", "cwd=%2Fstream+%2B+%231", None)),
            (("archive",), OK_STDOUT, ("POST", f"{path}/archive", "", {"cwd": cwd})),
        ):
            with self.subTest(command=argv[0]):
                proc = self.run_cli(argv[0], "sess-1", *argv[1:])
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assert_completed(proc, 0, stdout)
                self.assertIn(request, self.stub.requests)

    def test_existing_session_resolves_the_exact_workspace(self) -> None:
        self.stub.required_session_cwds["sess-1"] = "/repo/worktree"
        self.stub.set_routes([
            ("GET", "/api/sessions/sess-1/location", 200, {"sessionId": "sess-1", "projectId": "p1", "workspaceId": "w2"}),
            ("GET", "/api/projects/p1/workspaces", 200, {"workspaces": [
                {"id": "w1", "path": "/repo"}, {"id": "w2", "path": "/repo/worktree"},
            ]}),
        ])
        self.assert_completed(self.run_cli("queue-clear", "sess-1"), 0, OK_STDOUT)
        self.assertEqual(self.stub.requests[-1], ("POST", "/api/sessions/sess-1/queue/clear", "", {"cwd": "/repo/worktree"}))

    def test_explicit_cwd_skips_lookup(self) -> None:
        self.stub.required_session_cwds["sess-1"] = "/repo/worktree"
        self.assert_completed(self.run_cli("abort", "sess-1", "--cwd", "/repo/worktree"), 0, OK_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/abort", {"cwd": "/repo/worktree"})

    def test_unknown_workspace_does_not_guess_cwd(self) -> None:
        self.stub.required_session_cwds["sess-1"] = "/unknown"
        self.stub.set_routes([("GET", "/api/sessions/sess-1/location", 404, {"error": "Session not found"})])
        proc = self.run_cli("abort", "sess-1")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("Pass --cwd for the session.", proc.stdout)
        self.assertEqual(self.stub.requests, [
            ("POST", "/api/sessions/sess-1/abort", "", {}),
            ("GET", "/api/sessions/sess-1/location", "", None),
        ])

    def test_prompt(self) -> None:
        self.stub.set_routes([("POST", "/api/sessions/sess-1/prompt", 200, {"accepted": True})])
        proc = self.run_cli("prompt", "sess-1", "hi")
        self.assert_completed(proc, 0, PROMPT_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/prompt", {"text": "hi"})

    def test_prompt_steer(self) -> None:
        self.stub.set_routes([("POST", "/api/sessions/sess-1/prompt", 200, {"accepted": True})])
        proc = self.run_cli("prompt", "sess-1", "hi", "--steer")
        self.assert_completed(proc, 0, PROMPT_STDOUT)
        self.assert_request(
            "POST",
            "/api/sessions/sess-1/prompt",
            {"text": "hi", "streamingBehavior": "steer"},
        )

    def test_prompt_follow_up(self) -> None:
        self.stub.set_routes([("POST", "/api/sessions/sess-1/prompt", 200, {"accepted": True})])
        proc = self.run_cli("prompt", "sess-1", "hi", "--follow-up")
        self.assert_completed(proc, 0, PROMPT_STDOUT)
        self.assert_request(
            "POST",
            "/api/sessions/sess-1/prompt",
            {"text": "hi", "streamingBehavior": "followUp"},
        )

    def test_status(self) -> None:
        self.stub.set_routes([
            ("GET", "/api/sessions/sess-1/status", 200, {"sessionId": "sess-1", "isStreaming": False}),
        ])
        proc = self.run_cli("status", "sess-1")
        self.assert_completed(proc, 0, STATUS_STDOUT)
        self.assert_request("GET", "/api/sessions/sess-1/status", None)

    def test_commands(self) -> None:
        self.stub.set_routes([("GET", "/api/sessions/sess-1/commands", 200, [{"name": "help"}])])
        proc = self.run_cli("commands", "sess-1")
        self.assert_completed(proc, 0, COMMANDS_STDOUT)
        self.assert_request("GET", "/api/sessions/sess-1/commands", None)

    def test_stop(self) -> None:
        proc = self.run_cli("stop", "sess-1")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/stop", {})

    def test_abort(self) -> None:
        proc = self.run_cli("abort", "sess-1")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/abort", {})

    def test_queue_clear(self) -> None:
        proc = self.run_cli("queue-clear", "sess-1")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/queue/clear", {})

    def test_archive(self) -> None:
        proc = self.run_cli("archive", "sess-1")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/archive", {})

    def test_messages(self) -> None:
        proc = self.run_cli("messages", "sess-1")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request("GET", "/api/sessions/sess-1/messages", None)

    def test_model(self) -> None:
        proc = self.run_cli("model", "sess-1", "acme/org/model-1")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request(
            "POST",
            "/api/sessions/sess-1/model",
            {"provider": "acme", "modelId": "org/model-1"},
        )

    def test_thinking_level(self) -> None:
        proc = self.run_cli("thinking-level", "sess-1", "xhigh")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/thinking-level", {"level": "xhigh"})

    def test_answer(self) -> None:
        proc = self.run_cli("answer", "sess-1", "ask-9", "q-1", "no")
        self.assert_completed(proc, 0, OK_STDOUT)
        self.assert_request(
            "POST",
            "/api/sessions/sess-1/ask/submit",
            {
                "askId": "ask-9",
                "answers": [{"id": "q-1", "values": [], "otherText": "no"}],
            },
        )

    def test_prompt_400(self) -> None:
        self.stub.set_routes([("POST", "/api/sessions/sess-1/prompt", 400, {"error": "nope"})])
        proc = self.run_cli("prompt", "sess-1", "hi")
        self.assert_completed(proc, 1, PROMPT_400_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/prompt", {"text": "hi"})

    def test_prompt_500(self) -> None:
        self.stub.set_routes([("POST", "/api/sessions/sess-1/prompt", 500, {"error": "down"})])
        proc = self.run_cli("prompt", "sess-1", "hi")
        self.assert_completed(proc, 1, PROMPT_500_STDOUT)
        self.assert_request("POST", "/api/sessions/sess-1/prompt", {"text": "hi"})

    def test_pi_web_url_trailing_slash(self) -> None:
        self.stub.set_routes([("GET", "/api/sessions", 200, [{"id": "s1", "cwd": "/work"}])])
        proc = self.run_cli("list", "--cwd", "/work", url=self.stub.url + "/")
        self.assert_completed(proc, 0, LIST_STDOUT)
        self.assert_request("GET", "/api/sessions", None, "cwd=%2Fwork")


if __name__ == "__main__":
    unittest.main()
