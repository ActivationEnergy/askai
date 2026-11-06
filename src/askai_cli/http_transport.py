"""One-shot HTTP transport with a parent-enforced wall-clock deadline.

urllib's timeout only bounds individual socket operations. A short-lived,
isolated child owns the exchange so its parent can stop DNS, connection setup,
headers, and slowly arriving bodies without leaving a background thread behind.
The private protocol uses stdin/stdout; request headers never appear in argv.
"""

import base64
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib import error, request
from urllib.parse import urlsplit

from .core import AskError, MAX_RESPONSE_BYTES


MAX_ERROR_BYTES = 8 * 1024


class NoRedirect(request.HTTPRedirectHandler):
    """An authenticated POST must never forward credentials to a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TransportFailure(Exception):
    def __init__(self, kind: str):
        self.kind = kind


def _exchange(packet: dict) -> dict:
    """Run only in the owned child; the parent supplies the hard deadline."""
    url = packet["url"]
    if urlsplit(url).scheme not in ("http", "https"):
        raise _TransportFailure("request")
    data = base64.b64decode(packet["body"], validate=True)
    method = packet.get("method", "POST")
    if method not in ("GET", "POST") or method == "GET" and data:
        raise _TransportFailure("request")
    req = request.Request(url, data=None if method == "GET" else data,
                          headers=packet["headers"], method=method)
    try:
        with request.build_opener(NoRedirect).open(req, timeout=packet["timeout"]) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _TransportFailure("too_large")
            return {"status": response.status,
                    "reason": str(getattr(response, "reason", ""))[:256],
                    "body": base64.b64encode(raw).decode("ascii")}
    except error.HTTPError as exc:
        with exc:
            # HTTP failures retain their code and a bounded body for the
            # caller's existing redacted diagnostics, never a free-form error.
            try:
                raw = exc.read(MAX_ERROR_BYTES)
            except (OSError, TimeoutError):
                raw = b""
            return {"status": exc.code, "reason": str(exc.reason)[:256],
                    "body": base64.b64encode(raw).decode("ascii")}
    except (error.URLError, TimeoutError, OSError):
        raise _TransportFailure("connection") from None


def _worker_main() -> None:
    """Emit one bounded result, without exception text or credential logging."""
    try:
        packet = json.load(sys.stdin)
        result = _exchange(packet)
    except _TransportFailure as exc:
        result = {"error": exc.kind}
    except (ValueError, KeyError, TypeError, UnicodeError):
        result = {"error": "request"}
    except Exception as exc:
        # Keep worker failures diagnosable without printing request objects,
        # exception messages containing URLs, or authorization headers.
        result = {"error": "internal", "exception_type": type(exc).__name__}
    json.dump(result, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")


def _worker_argv() -> list[str]:
    root = str(Path(__file__).resolve().parents[1])
    code = (f"import sys; sys.path.insert(0, {root!r}); "
            "from askai_cli.http_transport import _worker_main; _worker_main()")
    return [sys.executable, "-I", "-S", "-c", code]


def _kill_and_reap(process: subprocess.Popen) -> None:
    """The child has its own process group and owns no persistent resources."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def fetch(url: str, headers: dict[str, str], body_bytes: bytes, timeout: float,
          *, method: str = "POST") -> dict:
    """GET or POST once with a total deadline; return status, reason and bytes.

    HTTP error statuses are returned rather than raised so callers can retain
    existing status-specific diagnostics. Connection, deadline, size and worker
    failures raise AskError containing no response body, URL or credentials.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise AskError("API 超时必须为有限正数")
    if method not in ("GET", "POST") or method == "GET" and body_bytes:
        raise AskError("API 传输只支持 GET 或 POST；GET 不能包含请求内容")
    started = time.monotonic()
    packet = json.dumps({"url": url, "headers": headers,
                         "body": base64.b64encode(body_bytes).decode("ascii"),
                         "timeout": timeout, "method": method}, ensure_ascii=True)
    if timeout - (time.monotonic() - started) <= 0:
        raise AskError(f"API 请求总超时（{timeout:g} 秒），传输未启动")
    try:
        with subprocess.Popen(_worker_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              start_new_session=True) as process:
            try:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                output, _ = process.communicate(packet, timeout=remaining)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                _kill_and_reap(process)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                raise AskError(f"API 请求总超时（{timeout:g} 秒），传输已停止") from None
            if process.returncode:
                raise AskError(f"API 传输进程异常退出（{process.returncode}）")
    except OSError:
        raise AskError("无法启动 API 传输进程") from None
    try:
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError("not an object")
        kind = result.get("error")
        if kind:
            messages = {
                "too_large": "API 响应超过 8 MiB，传输已停止",
                "connection": "API 连接失败；请检查网络、服务地址和证书",
                "request": "API 请求格式无效；请检查地址和请求配置",
                "internal": "API 传输进程处理失败",
            }
            message = messages.get(kind, "API 传输失败")
            if kind == "internal":
                name = result.get("exception_type", "")
                if isinstance(name, str) and name.isascii() and name.isidentifier() and len(name) <= 80:
                    message += f"（{name}）"
            raise AskError(message)
        status = result["status"]
        encoded = result["body"]
        limit = MAX_ERROR_BYTES if isinstance(status, int) and status >= 300 else MAX_RESPONSE_BYTES
        if (type(status) is not int or not 100 <= status <= 599
                or not isinstance(encoded, str) or len(encoded) > 4 * ((limit + 2) // 3)
                or not isinstance(result.get("reason", ""), str)):
            raise ValueError("invalid response")
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > limit:
            raise ValueError("oversized response")
        return {"status": status, "reason": result.get("reason", ""), "body": raw}
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AskError("API 传输进程未返回有效结果") from None
