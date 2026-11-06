"""Preview small TOML edits without reserializing unrelated user configuration."""

import json
from pathlib import Path
import stat
import tomllib

from . import core, startup
from .core import AskError


def _value_span(statement: str) -> tuple[int, int]:
    """Find an assignment value, respecting quoted keys and TOML strings."""
    quote = None
    depth = 0
    start = None
    i = 0
    while i < len(statement):
        c = statement[i]
        if quote:
            if quote[0] == '"' and c == "\\":
                i += 2
                continue
            if statement.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
        elif c in "\"'":
            quote = c * (3 if statement.startswith(c * 3, i) else 1)
            i += len(quote)
            continue
        elif start is None and c == "=":
            start = i + 1
            while start < len(statement) and statement[start] in " \t":
                start += 1
        elif start is not None:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
            elif c == "#":
                if depth == 0:
                    return start, len(statement[:i].rstrip())
                end = statement.find("\n", i)
                i = end if end >= 0 else len(statement)
                continue
            elif c in "\r\n" and depth == 0:
                return start, len(statement[:i].rstrip())
        i += 1
    if start is None:
        raise AskError("无法定位 TOML 配置值，未修改")
    return start, len(statement.rstrip())


def _has(tree: dict, keys: tuple[str, ...]) -> bool:
    value = tree
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True


def edit(text: str, keys: tuple[str, ...], value: str, eol: str) -> str:
    """Edit one scalar in ordinary, dotted, or quoted-key TOML.

    Inline parent tables are deliberately left untouched rather than rewritten.
    The full parsed result is compared with the intended one-field change.
    """
    original = tomllib.loads(text)
    expected = tomllib.loads(text)
    parent = expected
    for key in keys[:-1]:
        parent = parent.setdefault(key, {})
        if not isinstance(parent, dict):
            raise AskError("配置路径不是 TOML 表，未修改")
    parent[keys[-1]] = value
    encoded = json.dumps(value, ensure_ascii=False)
    context = ()
    insertion = 0
    insertion_context = ()
    offset = 0
    lines = text.splitlines(keepends=True)
    index = 0
    result = None
    while index < len(lines):
        statement = lines[index]
        index += 1
        if not statement.strip() or statement.lstrip().startswith("#"):
            offset += len(statement)
            continue
        while True:
            try:
                parsed = tomllib.loads(statement)
                break
            except ValueError:
                if index >= len(lines):
                    raise AskError("无法安全局部编辑 TOML，未修改")
                statement += lines[index]
                index += 1
        if statement.lstrip().startswith("["):
            # A probe reveals quoted/dotted table paths using tomllib itself.
            probe = tomllib.loads(statement + eol + "__askai_probe__ = 0" + eol)
            context = ()
            while isinstance(probe, dict) and len(probe) == 1 and "__askai_probe__" not in probe:
                key, probe = next(iter(probe.items()))
                context += (key,)
            if not isinstance(probe, dict) or "__askai_probe__" not in probe:
                context = None  # Array-of-table content cannot be this scalar.
        elif context is not None and keys[:len(context)] == context:
            remaining = keys[len(context):]
            if remaining and _has(parsed, remaining):
                # Only a scalar assignment can be replaced without erasing peers.
                leaf = parsed
                for key in remaining:
                    if len(leaf) != 1:
                        raise AskError("目标字段位于内联表，请在配置文件中手动修改；其他设置未改动")
                    leaf = leaf[key]
                a, b = _value_span(statement)
                candidate = text[:offset + a] + encoded + text[offset + b:]
                if tomllib.loads(candidate) != expected:
                    raise AskError("目标字段位于内联表，请在配置文件中手动修改；其他设置未改动")
                result = candidate
                break
        offset += len(statement)
        if context is not None and keys[:len(context)] == context and len(context) < len(keys):
            if len(context) >= len(insertion_context):
                insertion, insertion_context = offset, context
    if result is None:
        if _has(original, keys):
            raise AskError("无法安全定位配置字段，请在配置文件中手动修改")
        key = ".".join(json.dumps(part, ensure_ascii=False) for part in keys[len(insertion_context):])
        prefix = eol if insertion and not text[:insertion].endswith("\n") else ""
        result = text[:insertion] + prefix + key + " = " + encoded + eol + text[insertion:]
    if tomllib.loads(result) != expected:
        raise AskError("局部修改与预期不符，未修改配置文件")
    return result


def plan(keys: tuple[str, ...], value: str) -> startup.Change | None:
    """Capture a backed-up atomic write, sharing startup's concurrency checks."""
    logical = core.config_path().absolute()
    if logical.is_symlink() and not logical.exists():
        raise AskError(f"AI 配置是悬空链接: {logical}")
    target = logical.resolve()
    if target.is_relative_to(Path("/etc").resolve()):
        raise AskError("不修改 /etc 系统配置")
    info, provenance = startup.inspect_metadata(target) if target.exists() else (None, None)
    raw = target.read_bytes() if info else None
    text, eol = startup.decode(raw or b"")
    try:
        after = edit(text, keys, value, eol).encode("utf-8")
    except ValueError as exc:
        raise AskError(f"AI 配置不是有效 TOML，未修改: {logical}") from exc
    if after == raw:
        return None
    return startup.Change([logical], target, raw, after, startup.identity(info) if info else None,
                          stat.S_IMODE(info.st_mode) if info else 0o600,
                          info.st_gid if info else None, provenance)
