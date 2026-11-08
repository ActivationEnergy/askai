"""Preview, validate, copy, save and execute independent Python scripts."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess

from .execution import child_env
import tempfile

from .core import AskError, clean_display, expand_path, validate_command, validate_python_encoding


MAX_INLINE_BYTES = 32 * 1024


def is_script(item: dict) -> bool:
    return item.get("kind") == "script"


def quote_argument(text: str, shell: str) -> str:
    """Quote literal arguments for fish or POSIX shell syntax."""
    if shell == "fish":
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return shlex.quote(text)


def python_executable() -> str:
    executable = shutil.which("python3")
    if not executable:
        raise AskError("找不到 python3；请安装 Python 3 后重试")
    return executable


def standalone_command(item: dict, shell: str) -> str:
    """Return a self-contained invocation; never reference a temporary script."""
    if not is_script(item):
        return item["command"]
    validate_python_encoding(item["code"])
    command = "python3 -I -X utf8 -c " + " ".join(quote_argument(arg, shell)
                                        for arg in [item["code"], *item.get("args", [])])
    if len(command.encode("utf-8")) > MAX_INLINE_BYTES:
        raise AskError("脚本过长，不适合复制为内联命令；请用 v编号复制源码，或 s编号保存脚本")
    return command


def preview(item: dict) -> str:
    if not is_script(item):
        return item["command"]
    args = item.get("args", [])
    detail = "运行参数: （无）" if not args else "运行参数:\n" + "\n".join(
        f"  {index}. {clean_display(value)}" for index, value in enumerate(args, 1))
    return f"Python 脚本（python3）\n{detail}\n\n{item['code']}"


def validate_candidate(item: dict, shell: str) -> None:
    if not is_script(item):
        validate_command(item["command"], shell)
        return
    validate_python_encoding(item["code"])
    # No imports from cwd/user site and no execution of the proposed code.
    result = subprocess.run([python_executable(), "-I", "-S", "-c",
                             "import sys; compile(sys.stdin.read(), '<askai>', 'exec')"],
                            input=item["code"], capture_output=True, text=True, encoding="utf-8", timeout=10, env=child_env())
    if result.returncode:
        raise AskError("Python 语法检查失败，没有执行:\n" + clean_display(result.stderr.strip()))


def run_script(item: dict) -> int:
    """Run from a temporary file, keeping stdin and cwd, with the same isolation as copied/saved commands."""
    validate_python_encoding(item["code"])
    executable = python_executable()
    with tempfile.TemporaryDirectory(prefix="askai-python-") as directory:
        path = Path(directory) / "script.py"
        path.write_text(item["code"], encoding="utf-8")
        return subprocess.call([executable, "-I", "-X", "utf8", str(path), *item.get("args", [])], env=child_env())



def save_script(item: dict, destination: str, shell: str) -> tuple[Path, str]:
    """Save only to a new user-selected file; return its durable invocation."""
    validate_python_encoding(item["code"])
    path = expand_path(destination).absolute()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AskError(f"文件已存在，未覆盖: {path}") from exc
    except (OSError, ValueError) as exc:
        raise AskError(f"无法保存脚本 {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(item["code"])
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    argv = [str(path), *item.get("args", [])]
    return path, "python3 -I -X utf8 " + " ".join(quote_argument(arg, shell) for arg in argv)
