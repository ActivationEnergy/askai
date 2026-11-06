"""Small, theme-following display helpers; never used to produce command data."""

import argparse
from contextvars import ContextVar
from dataclasses import dataclass
import os
import sys

from .core import clean_display


MODES = ("auto", "always", "never")
STYLES = {"title": "1;36", "action": "1", "accent": "36", "current": "32",
          "success": "32", "attention": "33", "error": "31",
          "diff-add": "32", "diff-remove": "31"}


def argument_parser(**kwargs) -> argparse.ArgumentParser:
    """Keep help/errors plain, including on Python versions with auto coloring."""
    if sys.version_info >= (3, 14):
        kwargs["color"] = False
    kwargs.setdefault("allow_abbrev", False)
    return argparse.ArgumentParser(**kwargs)


def add_color_argument(parser) -> None:
    parser.add_argument("--color", choices=MODES, default=None,
                        help="显示样式：auto 自动（尊重 NO_COLOR）、always 强制、never 纯文本；不影响命令/JSON/脚本数据")


def saved_mode(config: dict) -> tuple[str, bool]:
    """Describe a preference without displaying arbitrary invalid config values."""
    ui = config.get("ui", {})
    if not isinstance(ui, dict):
        return "无效类型", False
    value = ui.get("color", "auto")
    if not isinstance(value, str) or value not in MODES:
        return "不支持的取值", False
    return value, True


@dataclass(frozen=True)
class ColorPolicy:
    mode: str = "auto"
    source: str = "自动"
    no_color: bool = False
    term: str = ""

    def status(self, writer) -> tuple[bool, str]:
        if self.mode == "always":
            return True, f"{self.source}明确开启"
        if self.mode == "never":
            return False, f"{self.source}禁用样式"
        if self.no_color:
            return False, "NO_COLOR 禁用样式"
        if not self.term or self.term == "dumb":
            return False, "TERM 缺失或为 dumb"
        try:
            # StringIO-like writers need not implement fileno; degrade cleanly.
            writer.fileno()
            if not writer.isatty():
                return False, "不是终端输出"
        except (AttributeError, OSError, ValueError):
            return False, "无法确认终端输出"
        return True, "终端输出"


_policy: ContextVar[ColorPolicy | None] = ContextVar("askai_color", default=None)


def current_policy() -> ColorPolicy:
    return _policy.get() or ColorPolicy(no_color=bool(os.environ.get("NO_COLOR")),
                                       term=os.environ.get("TERM", ""))


def configure(config: dict | None = None, explicit: str | None = None, warning_writer=None) -> ColorPolicy:
    """Freeze one invocation's preference; stdout/stderr still decide separately."""
    value, valid = saved_mode(config or {}) if explicit is None else (explicit, True)
    source = "参数" if explicit is not None else "配置文件" if config is not None and "ui" in config else "自动"
    if not valid:
        value, source = "never", "无效颜色偏好"
    policy = ColorPolicy(value, source, bool(os.environ.get("NO_COLOR")), os.environ.get("TERM", ""))
    _policy.set(policy)
    if not valid:
        print("提示：ui.color 无效，本次使用纯文本；可在 askai-config 中检查颜色设置。",
              file=warning_writer if warning_writer is not None else sys.stderr)
    return policy


class Terminal:
    """Style only cleaned display text; each line is closed before writing."""

    def __init__(self, writer, policy: ColorPolicy | None = None):
        self.writer = writer
        self.policy = policy or current_policy()

    def text(self, value, role: str | None = None) -> str:
        text = clean_display(str(value))
        style = STYLES.get(role)
        if not style or not self.policy.status(self.writer)[0]:
            return text
        result = []
        for line in text.splitlines(keepends=True):
            content = line.rstrip("\n")
            suffix = line[len(content):]
            result.append((f"\x1b[{style}m{content}\x1b[22;39m" if content else "") + suffix)
        return "".join(result)

    def line(self, value="", role: str | None = None) -> None:
        text = self.text(value, role)
        if role == "title":
            marker = self.text(">", "accent") + " "
            text = "\n".join(marker + line if line else "" for line in text.split("\n"))
        self.writer.write(text + "\n")

    def prompt(self, value) -> None:
        self.writer.write(self.text(value, "action"))
        self.writer.flush()

    def notice(self, label: str, detail="", role: str = "attention") -> None:
        # Give short semantic labels a visible color, while the full reason or
        # result stays in the theme foreground and remains readable on its own.
        marker = {"success": "+", "error": "!", "attention": "!", "current": "*"}.get(role, ">")
        message = self.text(marker, role) + " " + self.text(label, role)
        if detail:
            message += "：" + self.text(detail)
        self.writer.write(message + "\n")

    def menu(self, text: str) -> None:
        """Render app-owned menu templates; never interpret model text as menus."""
        for line in text.splitlines():
            key, separator, label = line.partition(" ")
            self.writer.write(self.text(key, "action") + separator + self.text(label) + "\n")
