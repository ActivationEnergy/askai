"""Copy a selected command through a platform clipboard utility."""

import os
import platform
import shutil
import subprocess

from .core import AskError, clean_display


def copy_command(command: str) -> None:
    """Copy exact text, without a trailing newline or shell interpretation."""
    if platform.system() == "Darwin":
        candidates = [("pbcopy",)]
    elif os.name == "nt":
        candidates = [("clip.exe",)]
    else:
        candidates = []
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(("wl-copy",))
        candidates.extend([("xclip", "-selection", "clipboard"),
                           ("xsel", "--clipboard", "--input")])
        if not os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(("wl-copy",))
    for name, *args in candidates:
        executable = shutil.which(name)
        if executable:
            result = subprocess.run([executable, *args], input=command,
                                    capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise AskError("复制到剪贴板失败: " + clean_display(result.stderr.strip() or name))
            return
    raise AskError("找不到可用的剪贴板工具；macOS 需要 pbcopy，Linux 需要 wl-copy、xclip 或 xsel。可用 p编号打印命令")
