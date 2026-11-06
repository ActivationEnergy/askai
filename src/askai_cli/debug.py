"""Opt-in diagnostic output with credential redaction."""

import json
import os
import re
import shlex
import sys

from .core import clean_display
from .terminal import Terminal


TITLES = {
    "backend": "后端与模型", "AI request": "发送给 AI 的内容",
    "CLI invocation": "调用模型 CLI", "CLI response": "模型 CLI 返回",
    "CLI interrupted": "模型 CLI 已中断", "Claude response envelope": "Claude 返回详情",
    "HTTP request": "发送 API 请求", "HTTP raw response": "API 返回内容",
    "HTTP response": "API 模型与用量", "HTTP error": "API 请求失败",
    "AI failure": "AI 调用失败", "AI raw response": "AI 回答内容",
    "answer format repair": "修正回答格式", "answer validation failure": "回答格式修正失败",
}

FIELDS = {
    "name": "后端", "type": "类型", "requested_model": "请求模型",
    "reported_model": "后端报告的模型", "model": "模型", "timeout_seconds": "超时",
    "shell": "目标 shell", "web_search": "网页搜索", "config_file": "配置文件",
    "system_prompt": "系统提示词", "user_context": "用户上下文", "question": "问题",
    "os": "操作系统", "release": "系统版本", "cwd": "当前目录", "path": "文件路径",
    "argv": "调用命令", "stdin": "发送给 CLI 的文本", "stdout": "CLI 输出",
    "stderr": "CLI 诊断", "exit_code": "退出码", "elapsed_seconds": "耗时",
    "method": "请求方法", "url": "地址", "headers": "请求头", "body": "请求内容",
    "content": "内容", "status": "HTTP 状态", "reason": "原因", "error": "错误",
    "body_preview": "错误响应（最多 8 KiB）", "usage": "用量", "total_tokens": "总 token 数",
    "prompt_tokens": "输入 token 数", "completion_tokens": "输出 token 数",
    "input_tokens": "输入 token 数", "output_tokens": "输出 token 数",
    "messages": "消息", "role": "角色", "choices": "返回结果", "message": "消息",
    "explanation": "说明", "commands": "候选命令", "label": "用途",
    "command": "命令", "warning": "副作用", "result": "回答", "is_error": "是否出错",
    "stream": "流式返回", "data": "数据",
    "kind": "方案类型", "language": "脚本语言", "code": "脚本源码", "args": "运行参数",
    "details": "逐步解释",
    "validation_error": "格式问题", "attempt": "修正次数",
}


class DebugTrace:
    """Write request/response diagnostics to stderr without altering stdout."""

    def __init__(self, enabled: bool = False, secrets=()):
        self.enabled = enabled
        values = list(secrets)
        values.extend(value for key, value in os.environ.items()
                      if re.search(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)$", key, re.I))
        secrets = {value for value in values if isinstance(value, str) and value}
        secrets.update(json.dumps(value, ensure_ascii=True)[1:-1] for value in tuple(secrets))
        secrets.update(json.dumps(value, ensure_ascii=False)[1:-1] for value in tuple(secrets))
        self.secrets = sorted(secrets, key=len, reverse=True)

    def redact_text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(Bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|password|secret|token)["']?\s*[=:]\s*["']?)[^\s"'&,}]+''',
                       r"\1[REDACTED]", value)
        return clean_display(value)

    def sanitize(self, value, _depth: int = 0, _budget=None):
        if _budget is None:
            _budget = [2000]
        if _depth >= 24 or _budget[0] <= 0:
            return "[嵌套或过长内容已省略]"
        _budget[0] -= 1
        if isinstance(value, dict):
            sensitive = {"authorization", "proxy-authorization", "api_key", "api-key",
                         "apikey", "x-api-key", "x-auth-token", "access_token", "refresh_token",
                         "id_token", "token", "password", "secret", "client_secret"}
            result = {}
            for key, item in value.items():
                if _budget[0] <= 0:
                    result["[内容已省略]"] = "达到调试内容数量限制"
                    break
                safe_key = self.redact_text(str(key))
                result[safe_key] = ("[REDACTED]" if str(key).lower() in sensitive else
                                    self.sanitize(item, _depth + 1, _budget))
            return result
        if isinstance(value, (list, tuple)):
            result = []
            hide_next = False
            for item in value:
                if _budget[0] <= 0:
                    result.append("[嵌套或过长内容已省略]")
                    break
                result.append("[REDACTED]" if hide_next else self.sanitize(item, _depth + 1, _budget))
                if hide_next:
                    _budget[0] -= 1
                hide_next = isinstance(item, str) and item.lower() in (
                    "--api-key", "--token", "--password", "--secret", "--access-token")
            return result
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def _json_content(self, text: str):
        """Expand structured payloads, including JSON embedded in model messages."""
        candidate = text.strip()
        if candidate.startswith("```json\n") and candidate.endswith("\n```"):
            candidate = candidate[8:-4]
        if not candidate.startswith(("{", "[")):
            return text
        try:
            value = json.loads(candidate)
        except RecursionError:
            return {"调试内容": "[嵌套或过长内容已省略]"}
        except ValueError:
            return text
        return self.sanitize(value)

    def _lines(self, value, indent: int = 2, key: str = "", depth: int = 0) -> list[str]:
        """Render values as labelled prose and indented text, preserving scripts."""
        pad = " " * indent
        if depth >= 32:
            return [pad + "[嵌套或过长内容已省略]"]
        # Do not interpret JSON-looking shell scripts, paths or literal prompts.
        if isinstance(value, str) and key not in ("command", "code", "system_prompt", "path", "cwd") and depth < 12:
            expanded = self._json_content(value)
            if isinstance(expanded, (dict, list)):
                return self._lines(expanded, indent, key, depth + 1)
        if isinstance(value, dict):
            lines = []
            for field, item in value.items():
                name = FIELDS.get(field, field)
                if field == "argv" and isinstance(item, list) and all(isinstance(arg, str) for arg in item):
                    lines.extend(f"{pad}{line}" for line in (f"{name}: {shlex.join(item)}").splitlines())
                    continue
                content = self._lines(item, indent + 2, field, depth + 1)
                if len(content) == 1 and not isinstance(item, (dict, list)):
                    lines.append(f"{pad}{name}: {content[0][indent + 2:]}")
                else:
                    lines.append(f"{pad}{name}:")
                    lines.extend(content)
            return lines or [pad + "（空）"]
        if isinstance(value, (list, tuple)):
            lines = []
            for index, item in enumerate(value, 1):
                label = f"方案 {index}" if key == "commands" else str(index)
                lines.append(f"{pad}{label}:")
                lines.extend(self._lines(item, indent + 2, depth=depth + 1))
            return lines or [pad + "（无）"]
        if value is None:
            return [pad + "未提供"]
        if isinstance(value, bool):
            return [pad + ("是" if value else "否")]
        if isinstance(value, (int, float)) and key in ("elapsed_seconds", "timeout_seconds"):
            return [pad + f"{value:g} 秒"]
        if value == "":
            return [pad + "（空）"]
        lines = []
        for line in str(value).splitlines():
            # CLI stdin may append the context as one JSON line after the prompt.
            expanded = self._json_content(line) if depth < 12 and key in ("stdin", "stdout", "stderr") else line
            if isinstance(expanded, (dict, list)):
                lines.extend(self._lines(expanded, indent, depth=depth + 1))
            else:
                lines.append(pad + line)
        return lines

    def emit(self, label: str, value) -> None:
        if self.enabled:
            text = "\n".join(self._lines(self.sanitize(value)))
            title = TITLES.get(label, label)
            ui = Terminal(sys.stderr)
            ui.line()
            if label in ("HTTP error", "AI failure", "CLI interrupted", "answer validation failure"):
                ui.notice("[askai debug] " + title, role="error")
            else:
                ui.line("[askai debug] " + title, "title")
            ui.line(text)
            sys.stderr.flush()
