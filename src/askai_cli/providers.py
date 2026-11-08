"""Reuse CLI authentication or call an OpenAI-compatible Chat Completions API."""

import json
import os
import re
from pathlib import Path
import shutil
import selectors
import signal
import subprocess

from .execution import child_env
import tempfile
import time
from urllib import request
from urllib.parse import urlparse

from .core import AskError, MAX_RESPONSE_BYTES, api_key_env_name, clean_display
from .debug import CliTiming, DebugTrace
from .http_transport import fetch
from .dsh_policy import dsh_patch
from .registry import AUTO_ORDER, DEFAULTS


def providers(config: dict) -> dict:
    result = {name: dict(value) for name, value in DEFAULTS.items()}
    for name, value in config.get("providers", {}).items():
        base = result.get(name, {})
        # Changing an API preset into a CLI must not retain its API model/key.
        if "type" in value and value["type"] != base.get("type"):
            base = {}
        # A preset model belongs to its original endpoint. A custom gateway
        # must supply its own model or let the user fetch its catalog.
        if base.get("type") == "api" and "base_url" in value and "model" not in value:
            endpoint = value["base_url"]
            if not isinstance(endpoint, str) or endpoint.rstrip("/") != base["base_url"].rstrip("/"):
                base = {key: item for key, item in base.items() if key != "model"}
        result[name] = {**base, **value}
        api_key_env_name(result[name])
    return result


def available(provider: dict) -> bool:
    if provider.get("type") == "api":
        key_env = api_key_env_name(provider)
        return bool(key_env) and bool(os.environ.get(key_env))
    executable = provider.get("executable", "")
    return isinstance(executable, str) and bool(executable) and "\x00" not in executable and bool(shutil.which(executable))


def select_provider(config: dict, chosen: str | None) -> tuple[str, dict]:
    choices = providers(config)
    name = chosen or os.environ.get("ASKAI_PROVIDER") or config.get("provider", "auto")
    if not isinstance(name, str):
        raise AskError("provider 必须为字符串")
    if name == "auto":
        for candidate in AUTO_ORDER:
            provider = choices[candidate]
            if available(provider):
                return candidate, provider
        raise AskError("未找到可用后端；安装并登录 Codex，或配置 API key。运行 askai doctor 查看状态")
    if name not in choices:
        raise AskError(f"未知后端: {name}；运行 askai providers 查看列表")
    provider = choices[name]
    if not available(provider):
        raise AskError(f"后端 {name} 未就绪：检查 executable 或 api_key_env；运行 askai doctor")
    return name, provider


def _signal_cli_group(process, sig) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        # An exit racing our timeout does not replace the original diagnosis.
        pass


def _communicate_bounded(process, payload: bytes, timeout: float, timing=None) -> tuple[bytes, bytes]:
    """Drain both pipes while writing stdin, with a deadline and per-stream cap."""
    deadline = time.monotonic() + timeout
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending = memoryview(payload)
    with selectors.DefaultSelector() as selector:
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if pending:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
            if timing:
                timing.mark("请求输入已关闭")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in selector.select(remaining):
                stream, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        pending = pending[os.write(stream.fileno(), pending[:4096]):]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                        if timing:
                            timing.mark("请求输入已关闭")
                else:
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    if len(buffers[name]) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise AskError(f"模型 CLI {name} 超过 8 MiB 上限，已停止；未采纳任何候选命令")
                    buffers[name].extend(chunk)
                    if timing:
                        timing.received(name, chunk)
        process.wait(timeout=max(0, deadline - time.monotonic()))
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def run_cli(argv: list[str], payload: str, timeout: float, trace: DebugTrace | None = None) -> str:
    """Bound provider lifetime, including descendants, on timeout or cancellation."""
    if any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise AskError("模型 CLI 参数无效，未启动程序")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AskError("发送内容不是有效 UTF-8，未启动模型 CLI") from exc
    if trace:
        trace.emit("CLI invocation", {"argv": argv, "stdin": payload})
    started = time.monotonic()
    timing = CliTiming() if trace and trace.enabled else None
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True, env=child_env()) as process:
        try:
            if timing:
                timing.mark("子进程已创建")
                stdout, stderr = _communicate_bounded(process, encoded, timeout, timing=timing)
            else:
                stdout, stderr = _communicate_bounded(process, encoded, timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt, AskError, OSError) as exc:
            _signal_cli_group(process, signal.SIGTERM)
            # Close the pipes instead of buffering more output during cleanup.
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # The parent may exit before a descendant that ignores TERM.
                _signal_cli_group(process, signal.SIGKILL)
                process.wait()
            if trace:
                trace.emit("CLI interrupted", {"reason": type(exc).__name__, "elapsed_seconds": round(time.monotonic() - started, 3)})
            if isinstance(exc, (KeyboardInterrupt, AskError)):
                raise
            if isinstance(exc, OSError):
                raise AskError("无法读取模型 CLI 输出，已停止") from exc
            raise AskError(f"模型 CLI 超时（{timeout:g} 秒），已停止") from exc
        finally:
            if timing:
                timing.report(trace)
    diagnostic = stderr.decode("utf-8", errors="replace")
    if trace:
        trace.emit("CLI response", {"exit_code": process.returncode, "stdout": stdout.decode("utf-8", errors="backslashreplace"),
                                    "stderr": diagnostic, "elapsed_seconds": round(time.monotonic() - started, 3)})
    if process.returncode:
        # Redact before truncation: slicing can otherwise remove a credential's
        # prefix/authorization label and leave its still-sensitive suffix.
        detail = (trace or DebugTrace()).redact_text(diagnostic or stdout.decode("utf-8", errors="backslashreplace")).strip()[-3000:]
        raise AskError(f"模型 CLI 退出码 {process.returncode}:\n{detail}")
    try:
        return stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AskError("模型 CLI 输出不是有效 UTF-8，未采纳任何候选命令") from exc


def _codex_policy(executable: str, timeout: float) -> list[str]:
    """Disable native local tools/hooks and every configured MCP server.

    Listing metadata does not connect to MCP servers. A mapping-valued override
    is necessary: Codex merges an empty table and does not parse quoted dotted
    override keys as TOML keys. Unknown CLI behavior fails before model input.
    """
    flags = [arg for name in ("shell_tool", "view_image", "apps", "plugins", "hooks", "multi_agent")
             for arg in ("--disable", name)]
    flags += ["-c", "project_doc_max_bytes=0"]
    try:
        raw = run_cli([executable, *flags, "mcp", "list", "--json"], "", min(timeout, 10))
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError("oversized MCP metadata")
        servers = json.loads(raw)
        if not isinstance(servers, list):
            raise ValueError("not a list")
        names = []
        for server in servers:
            name = server.get("name") if isinstance(server, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("invalid server name")
            name.encode("utf-8")
            names.append(json.dumps(name, ensure_ascii=True) + "={enabled=false}")
        if names:
            flags += ["-c", "mcp_servers={" + ",".join(names) + "}"]
    except (AskError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise AskError("无法核实 Codex 的工具配置，未启动生成请求；请检查 CLI 版本及配置") from exc
    return flags


def cli_answer(provider: dict, model: str | None, system: str, prompt: str,
               timeout: float, trace: DebugTrace | None = None) -> str:
    kind = provider.get("type")
    command = provider.get("executable", "")
    if not isinstance(command, str) or "\x00" in command:
        raise AskError("模型 CLI 路径无效")
    executable = shutil.which(command)
    if not executable:
        raise AskError("模型 CLI 不存在")
    model = model or provider.get("model")
    if model is not None and (not isinstance(model, str) or "\x00" in model):
        raise AskError("model 必须是不含空字符的字符串")
    if kind == "codex":
        started = time.monotonic()
        web_search = provider.get("web_search", True)
        if not isinstance(web_search, bool):
            raise AskError("Codex 的 web_search 必须为 true 或 false")
        policy = _codex_policy(executable, timeout)
        with tempfile.TemporaryDirectory(prefix="askai-codex-") as directory:
            output = Path(directory) / "answer.json"
            argv = [executable, *policy, "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                    "--ephemeral", "--color", "never",
                    "-c", 'web_search="live"' if web_search else 'web_search="disabled"',
                    "-o", str(output)]
            if model:
                argv += ["--model", model]
            argv += ["-"]
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AskError("Codex 请求总超时，未启动生成请求")
            run_cli(argv, system + "\n\n" + prompt, remaining, trace=trace)
            if not output.is_file():
                raise AskError("Codex 没有写入最终响应；请检查 CLI 版本和登录状态")
            try:
                with output.open("rb") as source:
                    raw = source.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AskError("Codex 最终响应文件超过 8 MiB 上限，未采纳任何候选命令")
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AskError("Codex 最终响应文件不是有效 UTF-8，未采纳任何候选命令") from exc
    if kind == "claude":
        argv = [executable, "-p", "--tools", "", "--setting-sources", "",
                "--strict-mcp-config", "--output-format", "json", "--system-prompt", system]
        if model:
            argv += ["--model", model]
        raw = run_cli(argv, prompt, timeout, trace=trace)
        if trace:
            trace.emit("Claude response envelope", raw)
        try:
            result = json.loads(raw)
            if result.get("is_error"):
                raise AskError(f"Claude 返回错误: {clean_display(str(result.get('result', '')))}")
            return result["result"]
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise AskError("Claude 未返回有效的最终响应") from exc
    if kind == "dsh":
        # rc.5 accepts --patch at the launcher layer, not permission flags on
        # headless. Keep policy and credential reuse scoped to this invocation.
        overlay = dsh_patch(model, system, provider.get("reasoning_effort", DEFAULTS["dsh"]["reasoning_effort"]))
        with tempfile.TemporaryDirectory(prefix="askai-dsh-") as directory:
            patch = Path(directory) / "askai.patch.yml"
            patch.write_text(overlay, encoding="utf-8")
            if trace:
                trace.emit("DSH invocation policy", {"sandbox": "read-only", "approval": "never", "tools": "全部禁用（启动前必需的工具守卫）",
                                                    "credentials": "复用 DSH 已保存凭据，仅本次调用启用",
                                                    "model": model or "沿用 DSH 配置", "推理强度（请求值）": provider.get("reasoning_effort", DEFAULTS["dsh"]["reasoning_effort"])})
            return run_cli([executable, "--profile", "headless", "--patch", str(patch), "-"],
                           prompt, timeout, trace=trace)
    if kind == "command":
        args = provider.get("args", [])
        if not isinstance(args, list) or any(not isinstance(v, str) for v in args):
            raise AskError("自定义 CLI 的 args 必须为字符串数组")
        if model:
            raise AskError("自定义 CLI 仅接收 stdin 文本；模型参数请在适配器中配置")
        return run_cli([executable, *args], system + "\n\n" + prompt, timeout, trace=trace)
    raise AskError(f"未知后端类型: {kind}")


def api_base_url(provider: dict) -> str:
    """Validate a configured API origin before attaching credentials."""
    base = provider.get("base_url", "")
    if not isinstance(base, str) or not base:
        raise AskError("API 后端需要有效的 base_url")
    try:
        url = urlparse(base)
        if not url.hostname or url.port is not None and not 0 < url.port <= 65535:
            raise ValueError("missing hostname or invalid port")
        if url.username is not None or url.password is not None or url.query or url.fragment or any(c.isspace() or ord(c) < 32 for c in base):
            raise ValueError("unsupported URL components")
    except ValueError as exc:
        raise AskError("base_url 无效；请使用包含主机名的 API 地址，不要包含凭据、查询参数或片段") from exc
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in
                                      ("localhost", "127.0.0.1", "::1")):
        raise AskError("API 地址必须使用 HTTPS（本地 localhost 可使用 HTTP）")
    return base.rstrip("/")


MODEL_ID = re.compile(r"[A-Za-z0-9._:/@-]{1,256}")


def model_id_allowed(value, key: str = "") -> bool:
    """Accept literal IDs without displaying or saving a reflected API key."""
    return isinstance(value, str) and MODEL_ID.fullmatch(value) is not None and (not key or key not in value)


def list_api_models(provider: dict, timeout: float = 10) -> list[str]:
    """Read an explicitly requested catalog, without inference or configuration writes.

    The catalog reports server-supplied IDs, not verified account entitlements.
    CLI providers are deliberately excluded: their login/config files stay private.
    """
    if provider.get("type") != "api":
        raise AskError("此调用方式没有可查询的 API 模型目录；请沿用 CLI 配置或返回选择其他型号")
    base = api_base_url(provider)
    key = os.environ.get(api_key_env_name(provider), "")
    if not key:
        raise AskError("尚未设置此 API 的密钥环境变量；请配置密钥后重试")
    response = fetch(base + "/models", {"Accept": "application/json", "Authorization": "Bearer " + key},
                     b"", timeout, method="GET")
    if not 200 <= response["status"] < 300:
        raise AskError(f"获取模型列表失败（HTTP {response['status']}）；请检查密钥权限及服务是否支持 /models，或返回选择其他型号")
    try:
        data = json.loads(response["body"])
    except (ValueError, TypeError, RecursionError) as exc:
        raise AskError("服务未返回有效的 JSON 模型目录；可返回模型选择或使用高级设置") from exc
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 5000:
        raise AskError("模型目录格式不受支持或条目过多；可返回模型选择或使用高级设置")
    models = []
    seen = set()
    for row in rows:
        value = row.get("id") if isinstance(row, dict) else None
        if model_id_allowed(value, key) and value not in seen:
            seen.add(value)
            models.append(value)
    if not models:
        raise AskError("服务未返回可供选择的模型 ID；请返回模型选择，现有模型设置未改变")
    return models


def api_answer(provider: dict, model: str | None, system: str, prompt: str,
               timeout: float, trace: DebugTrace | None = None) -> str:
    started = time.monotonic()
    base = api_base_url(provider)
    model = model or provider.get("model")
    if not isinstance(model, str) or not model:
        raise AskError("API 后端需要 model")
    key = os.environ.get(api_key_env_name(provider), "")
    body = {"model": model, "stream": False, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    try:
        req = request.Request(base.rstrip("/") + "/chat/completions",
                          data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    except ValueError as exc:
        raise AskError("无法构造 API 请求；请检查 base_url 和请求参数") from exc
    if trace:
        trace.emit("HTTP request", {"method": "POST", "url": req.full_url,
                                    "headers": dict(req.header_items()), "body": body})
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise AskError(f"API 请求总超时（{timeout:g} 秒），传输未启动")
        response = fetch(req.full_url, dict(req.header_items()), req.data, remaining)
        raw = response["body"]
        if not 200 <= response["status"] < 300:
            if trace:
                trace.emit("HTTP error", {"status": response["status"], "reason": response.get("reason", ""),
                                          "body_preview": raw.decode("utf-8", errors="replace"),
                                          "elapsed_seconds": round(time.monotonic() - started, 3)})
            raise AskError(f"API HTTP {response['status']}；检查地址、模型、密钥权限及额度")
        if trace:
            trace.emit("HTTP raw response", {"status": response["status"],
                                             "content": raw.decode("utf-8", errors="replace")})
        data = json.loads(raw)
        if trace:
            trace.emit("HTTP response", {"reported_model": data.get("model") if isinstance(data, dict) else None,
                                         "usage": data.get("usage") if isinstance(data, dict) else None,
                                         "elapsed_seconds": round(time.monotonic() - started, 3)})
        text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise AskError("API 返回空答案；请检查模型、输出限制和账户状态")
        return text
    except (ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
        raise AskError("API 响应不符合 Chat Completions 格式") from exc


def answer(provider: dict, model: str | None, system: str, prompt: str,
           timeout: float, trace: DebugTrace | None = None) -> str:
    key_env = api_key_env_name(provider)
    trace = trace or DebugTrace()
    trace.add_secrets([os.environ.get(key_env, "")])
    method = api_answer if provider.get("type") == "api" else cli_answer
    if trace:
        trace.emit("AI request", {"system_prompt": system, "user_context": prompt})
    started = time.monotonic()
    try:
        raw = method(provider, model, system, prompt, timeout, trace=trace)
    except Exception as exc:
        if trace:
            trace.emit("AI failure", {"error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)})
            if isinstance(exc, AskError):
                raise AskError(trace.redact_text(str(exc))) from exc
        raise
    if trace:
        trace.emit("AI raw response", {"content": raw, "elapsed_seconds": round(time.monotonic() - started, 3)})
    return raw
