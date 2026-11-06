"""Configuration, model output validation, and shell syntax checking."""

import json
import math
import codecs
import io
from importlib.resources import files
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
import tokenize
import unicodedata


class AskError(Exception):
    """An actionable user-facing failure."""


SHELLS = ("fish", "zsh", "bash")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def expand_path(value: str | Path) -> Path:
    """Expand an explicit user path without leaking platform lookup errors."""
    try:
        if any(ord(c) < 32 for c in str(value)):
            raise ValueError("路径不能包含控制字符")
        return Path(value).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise AskError("无法展开路径；请检查用户目录和路径字符，或使用绝对路径") from exc


def xdg_directory(name: str, default: Path, env=None) -> Path:
    """Empty/relative XDG values never create implicit project configuration."""
    value = (os.environ if env is None else env).get(name, "")
    path = Path(value) if value else default
    return path if path.is_absolute() else default


def config_path() -> Path:
    explicit = os.environ.get("ASKAI_CONFIG")
    if explicit is not None:
        if not explicit:
            raise AskError("ASKAI_CONFIG 不能为空；请指定配置文件或取消该环境变量")
        return expand_path(explicit)
    return xdg_directory("XDG_CONFIG_HOME", Path.home() / ".config") / "askai/config.toml"


def menu_index(value: str, count: int) -> int | None:
    """Parse a bounded ASCII menu index without arbitrary integer conversion."""
    if not value or not value.isascii() or not value.isdecimal() or len(value) > len(str(count)):
        return None
    number = int(value)
    return number - 1 if 1 <= number <= count else None


def validate_python_encoding(code: str) -> None:
    """All execution forms must interpret the exact same UTF-8 source text."""
    try:
        if code.startswith("\ufeff"):
            raise AskError("独立 Python 源码不接受 BOM；请使用无 BOM 的 UTF-8")
        encoding, _ = tokenize.detect_encoding(io.BytesIO(code.encode("utf-8")).readline)
        if codecs.lookup(encoding).name not in ("utf-8", "utf-8-sig"):
            raise AskError("独立 Python 脚本仅支持 UTF-8 源码，不能使用改变解释方式的编码声明")
    except (UnicodeError, SyntaxError, LookupError) as exc:
        raise AskError("Python 源码编码声明无效；请使用 UTF-8") from exc


def _reject_json_constant(value: str):
    raise ValueError("non-standard JSON constant")


def _validate_json_values(value) -> None:
    # Includes metadata and unknown JSON fields/keys, which --json also emits.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise AskError("模型响应包含无效 Unicode 字符，请重新生成")
        if isinstance(item, float) and not math.isfinite(item):
            raise AskError("模型响应包含非有限数值，不是有效的 JSON 结果")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def api_key_env_name(provider: dict) -> str:
    """Validate a configured environment key before any getenv or subprocess."""
    if "api_key_env" not in provider:
        return ""
    value = provider["api_key_env"]
    if not isinstance(value, str) or not value or "\x00" in value or "=" in value:
        raise AskError("api_key_env 必须为非空环境变量名，且不能包含 NUL 或等号")
    return value


def load_config(path: Path | None = None) -> dict:
    """Load only the user's configuration, never a file from the working directory."""
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise AskError(f"无法读取配置 {path}: {exc}") from exc
    if not isinstance(config.get("provider", "auto"), str):
        raise AskError("provider 必须为字符串")
    if not isinstance(config.get("providers", {}), dict):
        raise AskError("providers 必须为 TOML 表")
    for name, provider in config.get("providers", {}).items():
        if not isinstance(provider, dict):
            raise AskError(f"providers.{name} 必须为 TOML 表")
        api_key_env_name(provider)
    return config


def shell_name(value: str | None = None) -> str:
    name = value or Path(os.environ.get("SHELL", "bash")).name
    if name not in SHELLS:
        raise AskError(f"暂不支持 shell: {name}；请选择 fish、zsh 或 bash")
    return name


def clean_display(value: str) -> str:
    """Prevent model diagnostics from sending terminal control sequences."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C")
                   else f"\\u{ord(c):04x}" for c in value)


def instructions(shell: str) -> str:
    """Use one provider-independent prompt, injecting only the target shell."""
    return files("askai_cli").joinpath("prompts/command.txt").read_text(
        encoding="utf-8").replace("{{shell}}", shell)


def parse_answer(raw: str) -> dict:
    """Accept a JSON object, optionally fenced; never extract executable prose."""
    if not isinstance(raw, str):
        raise AskError("模型的最终响应不是文本，没有命令被执行")
    if len(raw) > MAX_RESPONSE_BYTES or len(raw.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
        raise AskError("模型响应超过 8 MiB 上限，未采纳任何候选命令")
    raw = raw.strip()
    if raw.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", raw, re.S)
        if not match:
            raise AskError("模型未返回有效 JSON；没有命令被执行")
        raw = match.group(1)
    try:
        data = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise AskError("模型未返回有效 JSON；没有命令被执行，请重试或更换模型") from exc
    if not isinstance(data, dict) or not isinstance(data.get("explanation"), str):
        raise AskError("模型响应缺少 explanation 字符串")
    _validate_json_values(data)
    commands = data.get("commands")
    if not isinstance(commands, list) or len(commands) > 10:
        raise AskError("模型响应 commands 必须是数组，且最多 10 项")
    for item in commands:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) for key in ("label", "warning")
        ):
            raise AskError("模型响应的命令格式不完整")
        for field in ("explanation", "details"):
            if field in item and not isinstance(item[field], str):
                raise AskError(f"方案的 {field} 必须为字符串")
        kind = item.get("kind", "command")
        if kind == "command":
            code = item.get("command")
        elif kind == "script":
            if item.get("language") != "python":
                raise AskError("独立脚本目前仅支持 language = python；其他工具请用可运行命令调用")
            if "command" in item:
                redundant = item["command"]
                if redundant is None or isinstance(redundant, str) and not redundant.strip():
                    # Models sometimes populate every schema field. An empty
                    # placeholder carries no second executable representation.
                    del item["command"]
                else:
                    raise AskError("脚本方案不能同时包含非空 command，以免展示和执行内容不一致")
            code = item.get("code")
            args = item.get("args", [])
            if not isinstance(args, list) or any(not isinstance(arg, str) or any(
                unicodedata.category(c).startswith("C") and c not in "\n\t" for c in arg) for arg in args):
                raise AskError("Python 脚本 args 必须为不含隐藏控制字符的字符串数组")
        else:
            raise AskError(f"不支持的方案类型: {kind}")
        if not isinstance(code, str) or not code.strip():
            raise AskError("模型响应的命令或脚本代码为空")
        if kind == "script":
            validate_python_encoding(code)
        if len(code.encode("utf-8")) > 256 * 1024:
            raise AskError("单个命令或脚本最多 256 KiB")
        # Rendered and executed command must be the same text. Reject hidden bytes.
        if any(unicodedata.category(c).startswith("C") and c not in "\n\t"
               for c in code):
            raise AskError("模型命令包含不可见控制字符，已拒绝")
    return data


def validate_command(command: str, shell: str) -> None:
    """Parse with the target shell without executing the proposed command."""
    executable = shutil.which(shell)
    if not executable:
        raise AskError(f"找不到 {shell}，无法检查或执行命令")
    options = {"fish": ["--no-config", "-n"], "zsh": ["-f", "-n"],
               "bash": ["--noprofile", "--norc", "-n"]}[shell]
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    result = subprocess.run([executable, *options], input=command + "\n",
                            capture_output=True, text=True, env=env, timeout=10)
    if result.returncode:
        raise AskError(f"{shell} 语法检查失败，没有执行:\n{clean_display(result.stderr.strip())}")
