"""Bounded HTTP exchanges use socketpairs and owned subprocess fixtures only."""

import base64
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli.core import AskError
from askai_cli import http_transport as transport


class HttpTransportTests(unittest.TestCase):
    def packet(self):
        return {"url": "https://example.invalid/chat/completions",
                "headers": {"Authorization": "Bearer FAKE_HTTP_SECRET"},
                "body": base64.b64encode(b'{"question":"fixture"}').decode(), "timeout": 2}

    def fixture_argv(self, script):
        return [sys.executable, "-I", "-S", "-c", script]

    def test_result_protocol_keeps_request_in_stdin_not_argv(self):
        packet = self.packet()
        script = ("import sys,json,base64; p=json.load(sys.stdin); "
                  "assert p['headers']['Authorization'].startswith('Bearer '); "
                  "assert base64.b64decode(p['body'])==b'{\"question\":\"fixture\"}'; "
                  "print(json.dumps({'status':200,'reason':'OK','body':base64.b64encode(b'answer').decode()}))")
        real_popen = subprocess.Popen
        observed = []
        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            observed.append(process)
            return process
        with patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), \
             patch.object(transport.subprocess, "Popen", side_effect=recording_popen):
            result = transport.fetch(packet["url"], packet["headers"], b'{"question":"fixture"}', 2)
        self.assertEqual(result, {"status": 200, "reason": "OK", "body": b"answer"})
        self.assertNotIn("FAKE_HTTP_SECRET", " ".join(observed[0].args))
        self.assertIsNotNone(observed[0].returncode)

    def test_success_and_error_body_limits_and_redirect_handler(self):
        class Response(io.BytesIO):
            status = 200
            reason = "OK"
        with patch.object(transport, "MAX_RESPONSE_BYTES", 16), patch.object(transport.request, "build_opener") as opener:
            opener.return_value.open.return_value = Response(b"x" * 17)
            with self.assertRaises(transport._TransportFailure) as failed:
                transport._exchange(self.packet())
            self.assertEqual(failed.exception.kind, "too_large")
            self.assertIs(opener.call_args.args[0], transport.NoRedirect)
        with patch.object(transport, "MAX_ERROR_BYTES", 8), patch.object(transport.request, "build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"x" * 20))
            result = transport._exchange(self.packet())
        self.assertEqual(result["status"], 401)
        self.assertEqual(base64.b64decode(result["body"]), b"x" * 8)
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid"))

    def test_worker_errors_never_echo_credentials_or_upstream_text(self):
        for error in ("connection", "too_large", "request", "internal"):
            script = "import sys,json;sys.stdin.read();print(json.dumps({'error':" + repr(error) + "}))"
            with self.subTest(error=error), patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), self.assertRaises(AskError) as failed:
                transport.fetch(self.packet()["url"], self.packet()["headers"], b"private body", 2)
            self.assertNotIn("FAKE_HTTP_SECRET", str(failed.exception))
            self.assertNotIn("private body", str(failed.exception))
        script = "import sys;sys.stdin.read();print('Bearer FAKE_HTTP_SECRET',file=sys.stderr);sys.exit(3)"
        with patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), self.assertRaises(AskError) as failed:
            transport.fetch(self.packet()["url"], self.packet()["headers"], b"private body", 2)
        self.assertNotIn("FAKE_HTTP_SECRET", str(failed.exception))

    def test_get_method_has_no_body_and_uses_the_same_private_protocol(self):
        class Response(io.BytesIO):
            status = 200
            reason = "OK"
        packet = {**self.packet(), "url": "https://example.invalid/models", "body": "", "method": "GET"}
        with patch.object(transport.request, "build_opener") as opener:
            opener.return_value.open.return_value = Response(b'{"data":[]}')
            transport._exchange(packet)
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(), "GET")
            self.assertIsNone(req.data)
        script = ("import sys,json,base64;p=json.load(sys.stdin);"
                  "assert p['method']=='GET' and p['body']=='';"
                  "print(json.dumps({'status':200,'body':base64.b64encode(b'{}').decode()}))")
        with patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)):
            self.assertEqual(transport.fetch(packet["url"], packet["headers"], b"", 2, method="GET")["body"], b"{}")

    def test_other_methods_and_get_bodies_are_rejected_before_starting_worker(self):
        with patch.object(transport.subprocess, "Popen") as process:
            for method, body in (("DELETE", b""), ("GET", b"private body")):
                with self.assertRaises(AskError):
                    transport.fetch("https://example.invalid/models", {}, body, 1, method=method)
            process.assert_not_called()

    def test_slow_continuous_http_response_expires_at_total_deadline_and_reaps(self):
        """A real HTTPResponse can keep each socket read alive beyond the budget."""
        with tempfile.TemporaryDirectory(prefix="askai-http-deadline-") as folder:
            entered = Path(folder) / "entered"
            client, server = socket.socketpair()
            self.addCleanup(client.close)
            self.addCleanup(server.close)
            stopped = threading.Event()
            def send_slowly():
                try:
                    server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n")
                    while not stopped.wait(.03):
                        server.sendall(b"x")
                except OSError:
                    pass
            sender = threading.Thread(target=send_slowly, daemon=True)
            sender.start()
            module_root = str(Path(transport.__file__).resolve().parents[1])
            script = (
                f"import sys;sys.path.insert(0,{module_root!r});"
                "import socket,http.client,pathlib;"
                "from askai_cli import http_transport as t;"
                f"s=socket.socket(fileno={client.fileno()});s.settimeout(.08);"
                "r=http.client.HTTPResponse(s);"
                "opener=type('FixtureOpener',(),{'open':lambda self,*a,**kw:(r.begin(),r)[1]})();"
                "t.request.build_opener=lambda *a:opener;"
                f"pathlib.Path({str(entered)!r}).write_text('entered');"
                "t._worker_main()"
            )
            real_popen = subprocess.Popen
            observed = []
            def inherited_socket_popen(*args, **kwargs):
                kwargs["pass_fds"] = (client.fileno(),)
                process = real_popen(*args, **kwargs)
                observed.append(process)
                return process
            try:
                started = time.monotonic()
                with patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), \
                     patch.object(transport.subprocess, "Popen", side_effect=inherited_socket_popen), \
                     self.assertRaisesRegex(AskError, "API 请求总超时"):
                    transport.fetch(self.packet()["url"], {}, b"{}", .4)
                elapsed = time.monotonic() - started
                self.assertTrue(entered.exists(), "fixture must enter the HTTP worker before timeout")
                self.assertLess(elapsed, 1.2)
                self.assertGreater(elapsed, .3)
                self.assertIsNotNone(observed[0].returncode, "owned worker must be reaped")
            finally:
                stopped.set()
                client.close()
                server.close()
                sender.join(1)
            self.assertFalse(sender.is_alive())

    def test_blocked_connection_and_keyboard_interrupt_reap_the_owned_process(self):
        script = "import sys,time;sys.stdin.read();time.sleep(20)"
        real_popen = subprocess.Popen
        observed = []
        def recording_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            observed.append(process)
            return process
        with patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), \
             patch.object(transport.subprocess, "Popen", side_effect=recording_popen), \
             self.assertRaisesRegex(AskError, "API 请求总超时"):
            transport.fetch(self.packet()["url"], {}, b"{}", .15)
        self.assertIsNotNone(observed[-1].returncode)
        # A real owned child is interrupted at communicate, then the original
        # implementation is used by cleanup so the process is still reaped.
        observed.clear()
        def interrupted_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            original = process.communicate
            def interrupt_once(*a, **kw):
                process.communicate = original
                raise KeyboardInterrupt
            process.communicate = interrupt_once
            observed.append(process)
            return process
        with patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), \
             patch.object(transport.subprocess, "Popen", side_effect=interrupted_popen), self.assertRaises(KeyboardInterrupt):
            transport.fetch(self.packet()["url"], {}, b"{}", 2)
        self.assertIsNotNone(observed[-1].returncode)

    def test_invalid_private_protocol_is_not_an_api_success(self):
        for payload in ("not-json", "[]", '{"status":200,"body":"!"}', '{"status":true,"body":""}'):
            script = "import sys;sys.stdin.read();print(" + repr(payload) + ")"
            with self.subTest(payload=payload), patch.object(transport, "_worker_argv", return_value=self.fixture_argv(script)), self.assertRaisesRegex(AskError, "未返回有效结果"):
                transport.fetch(self.packet()["url"], {}, b"{}", 2)


if __name__ == "__main__":
    unittest.main()
