"""Plan and apply narrowly scoped, backed-up shell startup file changes."""

from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from .execution import child_env
import tempfile

from .core import AskError, SHELLS, clean_display, xdg_directory


BEGIN = "# >>> askai shell integration "
END = "# <<< askai shell integration <<<"


def default_paths(shell: str, home: Path | None = None, env=None, all_bash: bool = False) -> list[Path]:
    home = home or Path.home()
    env = os.environ if env is None else env
    if shell == "fish":
        return [xdg_directory("XDG_CONFIG_HOME", home / ".config", env) / "fish/config.fish"]
    if shell == "zsh":
        value = env.get("ZDOTDIR")
        if value is None:
            return [home / ".zshrc"]
        if not Path(value).is_absolute() or any(ord(c) < 32 for c in value):
            raise AskError("ZDOTDIR 为空、相对或含控制字符，无法可靠自动定位；请指定绝对目录，或使用 --startup-file 选择绝对启动文件路径")
        return [Path(value) / ".zshrc"]
    profiles = [home / name for name in (".bash_profile", ".bash_login", ".profile")]
    if all_bash:
        return [home / ".bashrc", *profiles]
    selected = next((path for path in profiles if path.is_file() and os.access(path, os.R_OK)), None)
    if selected is None and any(path.exists() or path.is_symlink() for path in profiles):
        raise AskError("Bash 登录配置存在但不可读，无法安全选择；请先检查文件权限")
    return [home / ".bashrc", selected or profiles[0]]


def loader(shell: str, hook: bool, aliases: bool) -> str:
    flags = (" --hook" if hook else "") + (" --aliases" if aliases else "")
    if shell == "fish":
        return f'''if status is-interactive; and not set -q __askai_startup_loaded
    if command --search --quiet askai
        set -l _askai_init (command mktemp)
        if test $status -eq 0
            if command askai init fish{flags} > "$_askai_init"
                if source "$_askai_init"
                    set -gu __askai_startup_loaded 1
                end
            end
            command rm -f "$_askai_init"
        end
    end
end
'''
    variable = "BASH_VERSION" if shell == "bash" else "ZSH_VERSION"
    lookup = "type -P askai >/dev/null" if shell == "bash" else "whence -p askai >/dev/null"
    unexport = "export -n __askai_startup_loaded" if shell == "bash" else "typeset -g +x __askai_startup_loaded"
    guard = f'[ -n "${{{variable}-}}" ]'
    if shell == "bash":
        guard += ' && [ "${0##*/}" != sh ] && [ "${0##*/}" != -sh ]'
    return f'''if {guard}; then
    case $- in *i*)
        if [ "${{__askai_startup_loaded-}}" != 1 ] && {lookup}; then
            _askai_init=$(command mktemp) || _askai_init=
            if [ -n "$_askai_init" ]; then
                if command askai init {shell}{flags} > "$_askai_init"; then
                    if . "$_askai_init"; then
                        __askai_startup_loaded=1
                        {unexport}
                    fi
                fi
                command rm -f "$_askai_init"
                unset _askai_init
            fi
        fi
    ;; esac
fi
'''


def block(shell: str, hook: bool, aliases: bool, created: bool, eol: str) -> str:
    meta = json.dumps({"v": 1, "shell": shell, "hook": hook, "aliases": aliases, "created_file": created},
                      separators=(",", ":"))
    return ("\n\n" + BEGIN + meta + " >>>\n" + loader(shell, hook, aliases) + END + "\n").replace("\n", eol)


def locate(text: str) -> tuple[int, int, dict] | None:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if not starts and not ends:
        if "# >>> askai shell integration" in text or "# <<< askai shell integration" in text:
            raise AskError("askai 配置标记损坏，请人工检查")
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise AskError("askai 管理块重复或不完整，请人工检查")
    first, last = starts[0], ends[0]
    header = lines[first].rstrip("\r\n")
    try:
        meta = json.loads(header[len(BEGIN):-4]) if header.endswith(" >>>") else None
        if not isinstance(meta, dict) or set(meta) != {"v", "shell", "hook", "aliases", "created_file"}:
            raise ValueError("invalid metadata")
        if type(meta["v"]) is not int or meta["v"] != 1 or meta["shell"] not in SHELLS or any(type(meta[key]) is not bool for key in ("hook", "aliases", "created_file")):
            raise ValueError("unsupported metadata")
    except (ValueError, TypeError) as exc:
        raise AskError("无法识别 askai 管理块版本或选项") from exc
    eol = "\r\n" if lines[first].endswith("\r\n") else "\n"
    start = sum(map(len, lines[:first])) - 2 * len(eol)
    end = sum(map(len, lines[:last + 1]))
    expected = block(meta["shell"], meta["hook"], meta["aliases"], meta["created_file"], eol)
    if start < 0 or text[start:end] != expected:
        raise AskError("askai 管理区被修改或分隔换行损坏；为保留你的修改，未自动覆盖")
    return start, end, meta


def decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AskError("配置文件含 BOM，首版不自动转换")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise AskError("配置文件不是 UTF-8，未修改") from exc
    if "\r" in text.replace("\r\n", "") or ("\r\n" in text and "\n" in text.replace("\r\n", "")):
        raise AskError("配置文件换行格式混合，未自动转换")
    return text, "\r\n" if "\r\n" in text else "\n"


def inspect_metadata(path: Path, *, require_write: bool = True):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise AskError(f"仅支持本人拥有、没有硬链接的普通文件: {path}")
    if require_write and (not info.st_mode & stat.S_IWUSR or not os.access(path, os.W_OK)):
        raise AskError(f"配置文件不可写: {path}")
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) or getattr(info, "st_flags", 0):
        raise AskError(f"配置文件带特殊权限或文件标志，未修改: {path}")
    # Conservatively reject metadata which an atomic replacement would lose.
    provenance = None
    if sys_platform() == "darwin":
        attributes = subprocess.run(["/usr/bin/xattr", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=10, env=child_env())
        names = set(attributes.stdout.splitlines())
        if attributes.returncode or names - {"com.apple.provenance"}:
            raise AskError(f"无法安全保留配置文件扩展属性: {path}")
        if names:
            provenance = mac_provenance(path)
        result = subprocess.run(["/bin/ls", "-lde", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=10, env=child_env())
        if result.returncode or any(re.match(r"\s*\d+: ", line) for line in result.stdout.splitlines()[1:]):
            raise AskError(f"无法安全保留配置文件 ACL: {path}")
    elif hasattr(os, "listxattr"):
        if os.listxattr(path):
            raise AskError(f"配置文件含扩展属性/访问控制，首版不自动替换: {path}")
    else:
        raise AskError("此平台无法可靠检查文件元数据，当前版本不自动修改配置文件")
    return info, provenance


def mac_provenance(path: Path) -> str:
    result = subprocess.run(["/usr/bin/xattr", "-px", "com.apple.provenance", str(path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=10, env=child_env())
    if result.returncode:
        raise AskError(f"无法验证配置文件系统属性: {path}")
    return "".join(result.stdout.split())


def sys_platform() -> str:
    import sys
    return sys.platform


@dataclass
class Change:
    paths: list[Path]
    target: Path
    before: bytes | None
    after: bytes | None
    identity: tuple | None
    mode: int = 0o600
    gid: int | None = None
    provenance: str | None = None
    backup: Path | None = None

    def diff(self) -> str:
        return "".join(difflib.unified_diff((self.before or b"").decode().splitlines(keepends=True),
                                          (self.after or b"").decode().splitlines(keepends=True),
                                          fromfile=str(self.paths[0]), tofile=str(self.target)))


def identity(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_mode, info.st_gid


def check_syntax(text: str, executable: str) -> None:
    name = Path(executable).name
    flags = ["--no-config", "-n"] if name == "fish" else ["-f", "-n"] if name == "zsh" else ["--noprofile", "--norc", "-n"] if name == "bash" else ["-n"]
    env = child_env()
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    result = subprocess.run([executable, *flags], input=text, text=True, encoding="utf-8", capture_output=True, env=env, timeout=10)
    if result.returncode:
        raise AskError("拟写入文件未通过无执行语法检查，未修改:\n" + clean_display(result.stderr.strip()))


def interpreter(shell: str, value: str | None = None, *, allow_missing: bool = False) -> str | None:
    """Permit actual absence only for removal; invalid shell identities still fail."""
    candidate = value
    if not candidate and Path(os.environ.get("SHELL", "")).name == shell:
        candidate = os.environ["SHELL"]
    candidate = candidate or shell
    if any(ord(c) < 32 for c in candidate):
        raise AskError("Shell 程序路径不能包含控制字符")
    if Path(candidate).name != shell:
        raise AskError(f"Shell 程序名称须为 {shell}；请明确选择实际使用的可执行文件")
    executable = shutil.which(candidate)
    if not executable and os.path.dirname(candidate) and (Path(candidate).exists() or Path(candidate).is_symlink()):
        raise AskError(f"Shell 程序不可执行: {candidate}")
    if not executable and allow_missing:
        return None
    if not executable or Path(executable).name != shell:
        raise AskError(f"找不到 {shell} 解释器；请明确选择实际使用的可执行文件")
    return executable


def bash_hook_supported(executable: str) -> bool:
    result = subprocess.run([executable, "--noprofile", "--norc", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
                            capture_output=True, text=True, encoding="utf-8", timeout=10, env=child_env())
    return result.returncode == 0 and result.stdout.strip().isdigit() and int(result.stdout.strip()) >= 4


def plan(paths: list[Path], shell: str, hook: bool, aliases: bool, remove: bool = False,
         executable: str | None = None) -> list[Change]:
    executable = interpreter(shell, executable, allow_missing=remove)
    if hook and shell == "bash" and not remove and not bash_hook_supported(executable):
        raise AskError("所选 Bash 低于 4，不支持直接输入问题；请选择“先输入 askai 再提问”，或实际使用新版 Bash")
    groups: dict[Path, list[Path]] = {}
    for value in paths:
        logical = value.expanduser().absolute()
        if logical.is_symlink() and not logical.exists():
            raise AskError(f"启动文件是悬空符号链接: {logical}")
        try:
            target = logical.resolve()
        except (OSError, RuntimeError) as exc:
            raise AskError(f"无法解析启动路径: {logical}") from exc
        if target.is_relative_to(Path("/etc").resolve()):
            raise AskError("设置向导不修改 /etc 系统配置")
        groups.setdefault(target, []).append(logical)
    changes = []
    for target, logicals in groups.items():
        exists = target.exists()
        info, provenance = inspect_metadata(target) if exists else (None, None)
        raw = target.read_bytes() if exists else None
        text, eol = decode(raw or b"")
        found = locate(text)
        if found and found[2]["shell"] != shell:
            raise AskError(f"同一启动文件已由其他 shell 的 askai 管理块占用: {target}")
        if found:
            start, end, meta = found
            outside = text[:start] + text[end:]
        else:
            outside = text
            meta = {"created_file": not exists}
        if re.search(r"\baskai\s+init\s+(?:fish|zsh|bash)\b", outside):
            line = next(i for i, value in enumerate(outside.splitlines(), 1) if re.search(r"\baskai\s+init\b", value))
            raise AskError(f"{logicals[0]}:{line} 疑似已有手工初始化，请先人工处理；未自动替换")
        if remove:
            if not found:
                continue
            new = outside.encode()
            after = None if meta["created_file"] and not outside and not any(p.is_symlink() for p in logicals) else new
        else:
            replacement = block(shell, hook, aliases, meta["created_file"], eol)
            new_text = text[:start] + replacement + text[end:] if found else text + replacement
            after = new_text.encode()
        if after == raw:
            continue
        new_text = (after or b"").decode()
        if executable is not None:
            check_syntax(new_text, executable)
        if any(path.name == ".profile" for path in logicals):
            check_syntax(new_text, "/bin/sh")
        if shell == "zsh":
            for candidate in [target, *logicals]:
                if Path(str(candidate) + ".zwc").exists():
                    raise AskError(f"检测到 zsh 预编译配置，首版请先人工处理: {candidate}.zwc")
        changes.append(Change(logicals, target, raw, after, identity(info) if info else None,
                              stat.S_IMODE(info.st_mode) if info else 0o600, info.st_gid if info else None, provenance))
    return changes


def recheck(change: Change) -> None:
    for path in change.paths:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise AskError(f"无法重新解析预览后的启动文件路径: {path}") from exc
        if resolved != change.target:
            raise AskError(f"启动文件链接在预览后发生变化: {path}")
    if change.identity is None:
        if change.target.exists() or change.target.is_symlink():
            raise AskError(f"启动文件在预览后被创建: {change.target}")
    elif not change.target.exists() or identity(change.target.stat()) != change.identity or change.target.read_bytes() != change.before:
        raise AskError(f"启动文件在预览后发生变化，未覆盖: {change.target}")


def apply(changes: list[Change]) -> list[str]:
    completed = []
    backups = []
    try:
        for change in changes:
            recheck(change)
        for change in changes:
            recheck(change)
            change.target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if change.before is not None:
                fd, name = tempfile.mkstemp(prefix=change.target.name + ".askai-backup-", dir=change.target.parent)
                change.backup = Path(name)
                with os.fdopen(fd, "wb") as output:
                    output.write(change.before)
                    output.flush()
                    os.fsync(output.fileno())
                backups.append(str(change.backup))
            if change.after is None:
                recheck(change)
                change.target.unlink()
            else:
                fd, name = tempfile.mkstemp(prefix=".askai-write-", dir=change.target.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(change.after)
                        output.flush()
                        os.fsync(output.fileno())
                        if change.gid is not None and os.fstat(output.fileno()).st_gid != change.gid:
                            os.fchown(output.fileno(), -1, change.gid)
                        os.fchmod(output.fileno(), change.mode)
                    if change.provenance is not None and mac_provenance(temporary) != change.provenance:
                        raise AskError(f"暂存文件系统属性与原文件不同，未替换: {change.target}")
                    recheck(change)
                    os.replace(temporary, change.target)
                finally:
                    temporary.unlink(missing_ok=True)
            completed.append(str(change.target))
    except (OSError, AskError, KeyboardInterrupt) as exc:
        report = (f"设置停止: {exc}\n已变更: {', '.join(completed) or '无'}\n"
                  f"备份: {', '.join(backups) or '无'}\n未自动回滚，请核对以上文件。")
        if isinstance(exc, KeyboardInterrupt):
            print(clean_display(report), file=__import__('sys').stderr)
            raise
        raise AskError(report) from exc
    return backups
