"""Local shell setup UI and a non-blocking first-use hint; no model calls."""

from contextlib import contextmanager
import fcntl
import json
import os

from .credentials import api_key, source_label, stored_key, save_key, store_path, validate_key
from .interaction import read_secret
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib

from . import __version__
from .core import AskError, SHELLS, api_key_env_name, clean_display, expand_path, menu_index, xdg_directory
from . import startup
from . import core, preferences
from .providers import api_base_url, available, list_api_models, model_id_allowed, providers
from .registry import AUTO_ORDER, MENU_ORDER, PROVIDER_SPECS, ModelSpec, common_models, display_name, implicit_model_label
from .terminal import Terminal, add_color_argument, argument_parser, configure, saved_mode


def state_path() -> Path:
    root = xdg_directory("XDG_STATE_HOME", Path.home() / ".local/state")
    return root / "askai/setup-state.json"


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise AskError(f"设置状态不支持符号链接: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        raise AskError(f"设置状态文件所有权或权限异常: {path}")


def read_state() -> dict:
    path = state_path()
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "first_hint_seen": False, "locations": {}}
    try:
        _private_file(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"version", "first_hint_seen", "locations"}:
            raise ValueError("字段不受支持")
        if type(value["version"]) is not int or value["version"] != 1 or type(value["first_hint_seen"]) is not bool:
            raise ValueError("版本或提示字段不受支持")
        locations = value["locations"]
        if not isinstance(locations, dict) or any(key not in SHELLS for key in locations):
            raise ValueError("路径索引格式错误")
        for paths in locations.values():
            if not isinstance(paths, list) or any(not isinstance(p, str) or not Path(p).is_absolute() or any(ord(c) < 32 for c in p) for p in paths):
                raise ValueError("路径索引必须为绝对路径数组")
        return value
    except (OSError, ValueError, RecursionError) as exc:
        raise AskError(f"无法读取设置状态 {path}: {exc}；未自动重置") from exc


@contextmanager
def settings_lock():
    directory = state_path().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise AskError(f"设置目录所有权或权限异常: {directory}")
    descriptor = os.open(directory / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise AskError("设置锁文件异常")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AskError("同一设置目录已有另一个向导正在写入，请稍后重试") from exc
        yield
    finally:
        os.close(descriptor)


def write_state(value: dict) -> None:
    """Call with settings_lock held; preserve locations before touching rc files."""
    path = state_path()
    if path.exists() or path.is_symlink():
        _private_file(path)
    descriptor, name = tempfile.mkstemp(prefix=".setup-state-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def known_paths(shell: str, state: dict, overrides: list[Path] | None = None) -> list[Path]:
    paths = overrides if overrides else startup.default_paths(shell, all_bash=True)
    return list(dict.fromkeys([expand_path(p).absolute() for p in paths] +
                              [Path(p) for p in state["locations"].get(shell, [])]))


def configuration(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file():
        raise AskError(f"启动路径不是普通文件: {path}")
    text, _ = startup.decode(path.read_bytes())
    found = startup.locate(text)
    return found[2] if found else None


def show_status(shells, state: dict, writer, overrides=None) -> bool:
    ui = Terminal(writer)
    success = True
    ui.line("已保存的终端设置（重新打开终端后生效）", "title")
    for shell in shells:
        try:
            paths = known_paths(shell, state, overrides)
        except AskError as exc:
            success = False
            ui.notice(f"{shell} 无法自动定位", str(exc))
            paths = [Path(p) for p in state["locations"].get(shell, [])]
        for path in paths:
            try:
                meta = configuration(path)
                if meta:
                    mode = "直接输入问题" if meta["hook"] else "先输入 askai 等命令再提问"
                    shortcuts = "保留在当前终端" if meta["aliases"] else "仅在子进程中生效"
                    description = f"已配置 {meta['shell']}；提问方式: {mode}；快捷命令执行的 cd/环境变量修改: {shortcuts}"
                else:
                    description = "未配置"
                ui.line(f"{path}: {description}")
            except (OSError, AskError, RuntimeError) as exc:
                success = False
                ui.notice("配置异常", f"{path}: {exc}", "error")
    ui.line("这里显示文件中保存的设置，不能直接确认当前窗口是否已加载。")
    ui.line("Bash/zsh 直接输入问题时，选中执行的 cd/环境变量修改不会保留到当前终端；使用 askai 提问可保留。")
    return success


def maybe_hint() -> None:
    """Never read input. All excluded modes must return before calling this."""
    if os.environ.get("ASKAI_NO_SETUP") == "1" or os.environ.get("ASKAI_HOOK_INVOCATION") == "1" or os.environ.get("CI"):
        return
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        return
    try:
        state = read_state()
        if state["first_hint_seen"]:
            return
        for shell in SHELLS:
            for path in known_paths(shell, state):
                if configuration(path):
                    return
        with settings_lock():
            state = read_state()
            if state["first_hint_seen"]:
                return
            state["first_hint_seen"] = True
            write_state(state)
        ui = Terminal(sys.stderr)
        sys.stderr.write(ui.text(">", "accent") + " 可选设置：运行 " + ui.text("askai-config", "action") + "，启用当前会话执行和直接输入中文提问。\n")
    except (OSError, AskError, RuntimeError):
        # Optional discovery must not turn a valid question into a settings error.
        return


def _read(reader, writer, prompt: str) -> str | None:
    Terminal(writer).prompt(prompt)
    line = reader.readline()
    return line.strip() if line else None


def build_changes(shell: str, paths: list[Path], state: dict, hook: bool, aliases: bool,
                  remove: bool, executable: str | None, include_defaults: bool = True):
    if remove:
        return startup.plan(known_paths(shell, state, paths), shell, False, False, True, executable)
    changes = startup.plan(paths, shell, hook, aliases, executable=executable)
    targets = {path.resolve() for path in paths}
    candidates = known_paths(shell, state) if include_defaults else [Path(p) for p in state["locations"].get(shell, [])]
    old = [path for path in candidates if path.resolve() not in targets and
           (path.exists() or path.is_symlink()) and configuration(path)]
    return [*changes, *startup.plan(old, shell, False, False, True, executable)] if old else changes


def commit_changes(changes, shell: str) -> list[str]:
    with settings_lock():
        state = read_state()
        locations = set(state["locations"].get(shell, []))
        locations.update(str(path) for change in changes for path in change.paths)
        state["locations"][shell] = sorted(locations)
        state["first_hint_seen"] = True
        write_state(state)
        try:
            backups = startup.apply(changes)
        except AskError as exc:
            # Keep the superset: apply can have completed some replacements.
            raise AskError(f"{exc}\n设置位置记录已保留；请用 askai-config --status 核对实际状态。") from exc
        # Only forget positions once removal is known to have completed.
        try:
            state["locations"][shell] = [p for p in state["locations"][shell] if configuration(Path(p))]
            write_state(state)
        except (OSError, AskError) as exc:
            raise AskError(f"启动文件已更新，但定位清理未完成: {exc}；原定位记录仍可用于检查。备份: {backups}") from exc
        return backups


class _EndSetup(Exception):
    """EOF ends the entire local session without confirming pending changes."""


class Wizard:
    """Keep navigation local: completing a child action returns to its parent."""

    def __init__(self, args, reader, writer):
        self.reader, self.writer = reader, writer
        self.ui = Terminal(writer)
        self.explicit_color = args.color
        detected = args.shell or os.environ.get("ASKAI_SHELL") or Path(os.environ.get("SHELL", "")).name
        self.shell = detected if detected in SHELLS else "fish"
        self.paths = {self.shell: [expand_path(p).absolute() for p in args.startup_file]} if args.startup_file else {}
        self.executables = {self.shell: args.executable} if args.executable else {}

    def say(self, text="", role=None):
        self.ui.line(text, role)

    def menu(self, text):
        self.ui.menu(text)

    def ask(self, prompt="请选择 [0]: "):
        answer = _read(self.reader, self.writer, prompt)
        if answer is None:
            raise _EndSetup
        return answer

    def error(self, exc):
        self.ui.notice("本次操作未完成", str(exc), "error")
        self.say("未确认的设置不会保存，可以继续选择。")

    def selected_paths(self):
        return self.paths.get(self.shell) or startup.default_paths(self.shell)

    def hook_enabled(self):
        metas = [configuration(p) for p in known_paths(self.shell, read_state(), self.paths.get(self.shell))]
        return any(meta and meta["shell"] == self.shell and meta["hook"] for meta in metas)

    def run(self):
        while True:
            self.say("\naskai 设置", "title")
            self.say(f"当前 Shell：{self.shell}\n")
            self.menu("1 查看当前配置\n2 设置 Shell\n3 配置 AI 模型与后端\n4 设置直接提问\n0 退出设置")
            choice = self.ask()
            if choice in ("", "0"):
                return
            try:
                if choice == "1":
                    self.details()
                elif choice == "2":
                    self.shell_menu()
                elif choice == "3":
                    self.ai_menu()
                elif choice == "4":
                    self.question_menu()
                else:
                    self.say("请选择 0、1、2、3 或 4。")
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)

    def details(self):
        while True:
            self.say("\n当前保存的配置", "title")
            show_status([self.shell], read_state(), self.writer, self.paths.get(self.shell))
            config = core.load_config()
            self.say(f"AI 配置文件：{core.config_path()}")
            self.say(f"默认后端：{config.get('provider', 'auto')}")
            for name, provider in providers(config).items():
                self.say(f"  {name}：{provider.get('type', '?')}；模型：{self.model_text(provider)}；{self.readiness(provider)}")
            self.environment_note()
            self.menu("1 调整配置文件路径或 Shell 程序\n2 移除此 Shell 的 askai 终端设置\n3 设置颜色显示\n0 返回主菜单")
            choice = self.ask()
            if choice in ("", "0"):
                return
            try:
                if choice == "1":
                    if self.edit_paths():
                        self.shell_change(self.hook_enabled())
                elif choice == "2":
                    self.shell_change(False, remove=True)
                elif choice == "3":
                    self.color_menu()
                else:
                    self.say("请选择当前菜单的编号。")
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)

    def shell_menu(self):
        while True:
            self.say(f"\n设置 Shell · 当前：{self.shell}（不会更换登录 Shell）", "title")
            self.menu("1 fish\n2 zsh\n3 bash\n4 调整配置文件路径或 Shell 程序\n5 启用或更新显式命令集成\n6 移除此 Shell 的 askai 终端设置\n0 返回主菜单")
            choice = self.ask()
            if choice in ("", "0"):
                return
            try:
                if choice in ("1", "2", "3"):
                    self.shell = SHELLS[int(choice) - 1]
                    self.say(f"现在设置 {self.shell}；尚未修改文件。")
                elif choice == "4":
                    if self.edit_paths():
                        self.shell_change(self.hook_enabled())
                elif choice == "5":
                    self.shell_change(self.hook_enabled())
                elif choice == "6":
                    self.shell_change(False, remove=True)
                else:
                    self.say("请选择当前菜单的编号。")
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)

    def question_menu(self):
        while True:
            try:
                enabled = self.hook_enabled()
                self.say(f"\n设置直接提问 · {self.shell} · 当前：{'已开启' if enabled else '未开启'}", "title")
                self.say("开启后，可以直接在命令行输入中文或英文问题，无需加 askai。只有开头的单词或中文词语未被 Shell 识别为已有命令时，才会交给 askai。")
                self.say("例如：怎样查看磁盘占用。已有命令、别名和函数照常执行。输入需至少两个以空格分隔的词，或开头的词包含至少两个汉字；带参数的命令拼写错误也可能触发。执行建议前仍需你选择确认。")
                self.say("askai、askgpt、askds 等命令始终可用。Bash/zsh 直接提问时的 cd/环境变量修改不保留到父会话；需要保留时使用 askai。")
                self.menu(f"1 {'关闭' if enabled else '开启'}直接提问\n0 返回主菜单")
                choice = self.ask()
                if choice in ("", "0"):
                    return
                if choice == "1":
                    self.shell_change(not enabled)
                else:
                    self.say("请选择 1 或 0。")
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)
                return

    def edit_paths(self, executable_only=False):
        if not executable_only:
            edited = []
            try:
                defaults = self.selected_paths()
            except AskError as exc:
                self.say(str(exc))
                value = self.ask("启动文件绝对路径（回车取消）: ")
                if not value:
                    self.say("已取消路径修改。")
                    return False
                if not Path(value).is_absolute() or any(ord(c) < 32 for c in value):
                    raise AskError("请提供不含控制字符的绝对启动文件路径")
                edited.append(Path(value))
                defaults = []
            for path in defaults:
                answer = self.ask(f"启动文件路径（回车保留 {path}）: ")
                if any(ord(c) < 32 for c in answer):
                    raise AskError("配置路径不能包含控制字符")
                edited.append(expand_path(answer).absolute() if answer else path)
            self.paths[self.shell] = edited
        current = self.executables.get(self.shell) or self.shell
        value = self.ask(f"{self.shell} 程序路径（回车保留 {current}）: ")
        self.executables[self.shell] = startup.interpreter(self.shell, value or current)
        return True

    def shell_change(self, hook, remove=False):
        while True:
            try:
                executable = startup.interpreter(self.shell, self.executables.get(self.shell), allow_missing=remove)
                paths = self.selected_paths()
                changes = build_changes(self.shell, paths, read_state(), hook, True, remove,
                                        self.executables.get(self.shell) or executable,
                                        include_defaults=self.shell not in self.paths)
                self.say(f"\nShell：{self.shell}；程序：{executable or '未检测到'}")
                if executable is None:
                    self.say("未检测到此 Shell，仅移除已识别的 askai 管理块；无法进行该 Shell 的语法检查。")
                self.say("移除 askai 终端设置；保留用户其他内容和 AI 配置。" if remove else
                         f"直接提问：{'开启' if hook else '关闭'}；askai 和模型快捷命令可在当前会话执行选中的命令。")
                if not changes:
                    self.say("无需修改配置文件：设置已相同，或没有可移除的 askai 设置。")
                    # Preserve the location index even when a managed block is
                    # already identical (e.g. after moving the state root).
                    if not remove:
                        with settings_lock():
                            state = read_state()
                            state["locations"][self.shell] = sorted(set(state["locations"].get(self.shell, [])) | {str(p) for p in paths})
                            write_state(state)
                    return
                for change in changes:
                    self.say(f"配置文件：{', '.join(map(str, change.paths))}")
                    if any(p != change.target for p in change.paths):
                        self.say(f"链接实际指向：{change.target}")
                    self.say("修改前将创建仅你可读写的备份。" if change.before is not None else "将新建文件及必要的目录。")
                    for line in change.diff().splitlines():
                        role = "diff-add" if line.startswith("+") else "diff-remove" if line.startswith("-") else None
                        # Keep the actual diff text in the theme foreground;
                        # only the redundant +/- marker has a semantic color.
                        if role:
                            self.writer.write(self.ui.text(line[:1], role) + self.ui.text(line[1:]) + "\n")
                        else:
                            self.say(line)
                self.menu("y 确认保存\nn 不保存并返回上一级\np 更换配置文件和程序\ne 更换 Shell 程序")
                answer = self.ask("确认应用以上变更？[y/N]: ").lower()
                if answer == "p":
                    if not self.edit_paths():
                        return
                    continue
                if answer == "e":
                    if not self.edit_paths(executable_only=True):
                        return
                    continue
                if answer != "y":
                    self.say("已取消本次修改，返回上一级。")
                    return
                backups = commit_changes(changes, self.shell)
                activation = ("已更新选择的启动文件；请确认Shell会读取该文件，再重新打开终端。"
                              if self.shell in self.paths else "重新打开终端后生效。")
                self.ui.notice("设置已保存", activation + "返回上一级，可继续设置。", "success")
                for backup in backups:
                    self.say(f"恢复备份：{backup}")
                return True
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)
                self.menu("1 修改文件或程序路径\n2 重试\n0 返回上一级")
                answer = self.ask()
                if answer == "1":
                    if not self.edit_paths():
                        return
                elif answer != "2":
                    return

    @staticmethod
    def model_text(provider):
        value = provider.get("model")
        if value and provider.get("type") == "api":
            key = api_key(provider)
            if key and key in str(value):
                return "已隐藏疑似密钥，请重新选择模型"
        return value or implicit_model_label(provider)

    @staticmethod
    def backend_text(name, provider):
        kind = "API" if provider.get("type") == "api" else "自定义命令" if provider.get("type") == "command" else "CLI"
        return f"{display_name(name)} · {kind}"

    @staticmethod
    def readiness(provider):
        if provider.get("type") == "api":
            return source_label(provider) + "（未验证连接）"
        return "已检测到 CLI（未验证登录）" if available(provider) else "未检测到 CLI"

    def environment_note(self):
        for variable in ("ASKAI_PROVIDER", "ASKAI_MODEL"):
            if os.environ.get(variable):
                self.ui.notice("提示", f"当前 {variable} 环境变量会覆盖文件中的对应设置；命令行参数优先级更高。")

    def ai_menu(self):
        while True:
            try:
                config = core.load_config()
                items = providers(config)
                names = [n for n in MENU_ORDER if n in items]
                names += [n for n in items if n not in names and n != "auto"]
                names.append("auto")
                default = config.get("provider", "auto")
                default_label = "自动选择" if default == "auto" else self.backend_text(default, items[default]) if default in items else str(default)
                self.say("\nAI 模型与后端", "title")
                self.say(f"已保存默认：{default_label}\n")
                self.environment_note()
                for number, name in enumerate(names, 1):
                    description = "自动选择" if name == "auto" else self.backend_text(name, items[name])
                    action = "[已保存默认]" if name == default else f"[d{number} 设为默认]"
                    marker = self.ui.text("* ", "current") if name == default else ""
                    self.writer.write(self.ui.text(number, "action") + " " + self.ui.text(description) + "  " + marker + self.ui.text(action, "current" if name == default else "action") + "\n")
                    if name == "auto":
                        self.say("  优先 Codex；输入编号查看选择顺序。")
                    else:
                        provider = items[name]
                        if provider.get("type") == "api":
                            status = "已配置密钥" if available(provider) else "未配置密钥"
                        else:
                            status = "已安装" if available(provider) else "未安装或路径无效"
                        self.say(f"  模型：{self.model_text(provider)}")
                        if available(provider):
                            self.say(f"  状态：{status}")
                        else:
                            self.writer.write("  " + self.ui.text("!", "attention") + " " + self.ui.text("状态：") + self.ui.text(status, "attention") + "\n")
                    self.say()
                self.say("状态仅检查程序或密钥是否存在，不验证登录与连接。")
                self.say("先选择调用方式，再设置具体模型；输入 d编号设为默认（如 d3）。")
                self.menu("0 返回主菜单")
                answer = self.ask()
                if answer in ("", "0"):
                    return
                is_default = answer.startswith("d")
                number = answer[1:] if is_default else answer
                index = menu_index(number, len(names))
                if index is None:
                    self.say("请选择列表中的编号，或 d编号。")
                    continue
                name = names[index]
                if is_default:
                    if name == default:
                        self.say("这已经是默认后端。")
                        continue
                    self.save_preference(("provider",), name, f"默认后端：{default} → {name}；用于 askai 和直接提问。各后端模型及快捷命令保持不变。")
                elif name == "auto":
                    self.say("自动选择顺序：" + " → ".join(AUTO_ORDER))
                    self.say("Claude 仅在明确选择时使用。调用失败会报错，不静默换服务。可在任意行输入 d编号，固定使用该后端。")
                else:
                    if items[name].get("type") == "api":
                        self.api_menu(name)
                    else:
                        self.model_menu(name)
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)
                if self.ask("输入 1 重试读取 AI 配置，0 返回主菜单 [0]: ") != "1":
                    return

    def api_menu(self, name):
        """Keep model selection and credential management in separate flows."""
        while True:
            provider = providers(core.load_config()).get(name)
            if not provider or provider.get("type") != "api":
                return
            self.say(f"\n{self.backend_text(name, provider)}", "title")
            self.say(f"当前模型：{self.model_text(provider)}")
            self.say(self.readiness(provider))
            self.menu("1 选择模型\n2 管理 API 密钥\n0 返回 AI 模型与后端列表")
            choice = self.ask()
            if choice in ("", "0"):
                return
            if choice == "1":
                selected = self.model_menu(name)
                provider = providers(core.load_config()).get(name)
                if selected and provider and provider.get("type") == "api" and not api_key(provider):
                    self.say("模型已选定，还需配置 API 密钥才能提问。")
                    self.key_menu(name)
            elif choice == "2":
                self.key_menu(name)
            else:
                self.say("请选择菜单中的编号。")

    def key_menu(self, name):
        while True:
            provider = providers(core.load_config()).get(name)
            if not provider or provider.get("type") != "api":
                return
            self.say(f"\n{display_name(name)} · API 密钥", "title")
            self.say(self.readiness(provider))
            self.menu("1 粘贴并保存密钥（已有密钥可替换）")
            self.menu(f"2 从环境变量导入并保存 {api_key_env_name(provider)}\n3 删除已保存的密钥\n0 返回服务设置")
            choice = self.ask()
            if choice in ("", "0"):
                if not api_key(provider):
                    self.say("尚未完成配置：缺少 API 密钥，此服务暂不可用于提问。")
                return
            if choice not in ("1", "2", "3"):
                self.say("请选择菜单中的编号。")
                continue
            try:
                self.configure_api_key(name, provider, paste=choice == "1", delete=choice == "3")
            except (AskError, OSError) as exc:
                self.error(exc)

    def configure_api_key(self, name, provider, *, paste=False, delete=False):
        """Configure one key source in place, without adding a menu layer."""
        base = api_base_url(provider)
        variable = api_key_env_name(provider)
        if delete:
            if not stored_key(provider):
                self.say("此连接没有本地保存的密钥；环境变量不由 AskAI 保存或删除。")
                return
            value = None
            self.say("将删除此连接本地保存的密钥，不影响其他 API 连接。")
            self.say(f"删除后仍会读取环境变量 {variable}。" if variable and os.environ.get(variable)
                     else "删除后此 API 将处于未配置密钥状态。")
        elif paste:
            previous = stored_key(provider)
            value = read_secret(self.reader, self.writer, existing=bool(previous))
            if value is None or (not value and not previous):
                self.say("已取消，密钥未改变。")
                return
            if not value:
                value = previous
                self.say("将使用已保存的本地密钥。")
            else:
                self.say("将保存粘贴的密钥并优先使用它；替换此连接的旧密钥，不保留旧密钥备份。")
            validate_key(value)
        else:
            if not variable or not os.environ.get(variable):
                self.say(f"当前进程未检测到 {variable or '指定环境变量'}；可以直接选择粘贴密钥。")
                return
            value = validate_key(os.environ[variable])
            self.say(f"将从环境变量 {variable} 导入并保存密钥，以后无需再次设置环境变量。")
            if stored_key(provider):
                self.say("将替换此连接已保存的密钥，不保留旧密钥备份。")
        self.say(f"API 地址：{base}\n保存位置：{store_path().absolute()}")
        self.say("专用密钥文件：仅当前用户可读写（0600），明文未加密；不写入 config.toml 或 Shell 启动文件。")
        if self.ask("确认密钥设置？[y/N]: ").lower() != "y":
            self.say("未保存，密钥未改变。")
            return
        if providers(core.load_config()).get(name) != provider:
            self.say("连接设置已变化，未保存；请重新选择。")
            return
        save_key(provider, value)
        self.say("密钥设置已更新（未验证连接）。")

    def model_menu(self, name):
        while True:
            provider = providers(core.load_config()).get(name)
            if provider is None:
                self.say("此调用方式已被移除，请从上一级重新选择。")
                return
            if provider.get("type") == "command":
                self.say("此后端使用自定义命令适配器，不支持单独的模型参数；请在配置文件的 args 或该程序自身配置中设置模型。默认后端仍可从列表选择。")
                return
            api = provider.get("type") == "api"
            key = api_key(provider) if api else ""
            choices = [item for item in common_models(name, provider) if model_id_allowed(item.id, key)]
            if not api:
                choices.append(ModelSpec("", implicit_model_label(provider) if provider.get("type") == "claude" else f"沿用 {display_name(name)} 当前模型（推荐）"))
            current = provider.get("model") or ""
            if model_id_allowed(current, key) and all(item.id != current for item in choices):
                choices.append(ModelSpec(current, f"当前配置：{current}"))
            label = next((item.label for item in choices if item.id == current), self.model_text(provider))
            self.say(f"\n{self.backend_text(name, provider)}", "title")
            self.say(f"当前具体模型：{label}")
            self.say(self.readiness(provider))
            if api and common_models(name, provider):
                self.say(f"官方目录核对日期：{PROVIDER_SPECS[name].models_checked_on}；不代表账号已获调用权限。")
            for number, item in enumerate(choices, 1):
                self.menu(f"{number} {item.label}" + ("  [当前]" if item.id == current else ""))
            more = len(choices) + 1 if api else None
            advanced = len(choices) + 2 if api else len(choices) + 1
            if api:
                self.menu(f"{more} 更多型号（从服务商获取，需 API 密钥）")
            else:
                self.say("Claude 使用隔离设置，不读取用户或项目设置文件；可在高级设置中指定型号。" if provider.get("type") == "claude" else "更换原生模型可在该 CLI 自身设置中选择；askai 沿用其配置。")
            self.menu(f"{advanced} 高级设置")
            self.menu("0 返回服务设置" if api else "0 返回 AI 模型与后端列表")
            answer = self.ask()
            if answer in ("", "0"):
                return
            if answer == str(advanced):
                value = self.advanced_model_id(provider)
                if value is None:
                    continue
            elif api and answer == str(more):
                self.say("正在向当前 API 服务获取模型目录（最多 10 秒）；不发起模型生成，不修改设置。")
                try:
                    models = list_api_models(provider)
                except AskError as exc:
                    self.ui.notice("未能获取模型列表", str(exc), "attention")
                    self.say("现有设置未改变；可选择常用或当前型号，或配置密钥后重试。")
                    continue
                value = self.choose_model_id(models)
                if value is None:
                    continue
            else:
                index = menu_index(answer, len(choices))
                if index is None:
                    self.say("请选择菜单中的编号。")
                    continue
                value = choices[index].id
            if providers(core.load_config()).get(name) != provider:
                self.say("此调用方式的配置已发生变化；未保存旧目录中的选择，请重新选择。")
                return
            if value == current:
                self.say("这已经是当前型号，无需保存。")
                return True
            return self.save_preference(("providers", name, "model"), value,
                           f"{name} 模型：{self.model_text(provider)} → {value or '沿用 CLI 当前配置'}；默认后端和其他配置保持不变。",
                           destination="服务设置" if api else "AI 模型与后端列表",
                           expected_provider=(name, provider))

    def advanced_model_id(self, provider):
        """Keep custom IDs available without making normal users fill a form."""
        while True:
            self.say("\n高级设置", "title")
            self.menu("1 手动输入模型 ID\n0 返回模型选择")
            answer = self.ask()
            if answer in ("", "0"):
                return None
            if answer != "1":
                self.say("请选择 1 或 0。")
                continue
            value = _read(self.reader, self.writer, "模型 ID（回车返回）: ")
            if value is None:
                raise _EndSetup
            if not value:
                continue
            key = api_key(provider) if provider.get("type") == "api" else ""
            if not model_id_allowed(value, key):
                self.say("模型 ID 无效或包含敏感内容；请核对服务提供的型号名称。")
                continue
            return value

    def choose_model_id(self, models):
        """Pick from one immutable response; cancellation never saves a value."""
        query, page = "", 0
        while True:
            matches = [model for model in models if query.casefold() in model.casefold()]
            pages = max(1, (len(matches) + 9) // 10)
            start = page * 10
            self.say(f"\n服务商返回的模型目录 · {len(matches)} 个匹配 · 第 {page + 1}/{pages} 页", "title")
            self.say("仅列出服务返回的 ID，不保证当前账号有调用权限；选择后仍需确认保存。")
            if query:
                self.say(f"当前筛选：{query}")
            for number, model in enumerate(matches[start:start + 10], start + 1):
                self.menu(f"{number} {model}")
            if not matches:
                self.say("没有匹配的模型；可重新筛选或返回模型选择。")
            self.menu("编号选择 · /关键词筛选（单独 / 清除）· n下一页 · p上一页 · 0返回")
            answer = self.ask()
            if answer in ("", "0"):
                return None
            if answer.startswith("/"):
                query, page = answer[1:], 0
            elif answer == "n":
                page = min(page + 1, pages - 1)
            elif answer == "p":
                page = max(page - 1, 0)
            else:
                index = menu_index(answer, len(matches))
                if index is not None and start <= index < start + 10:
                    return matches[index]
                self.say("请输入当前页显示的编号，或使用筛选、翻页和返回。")

    def save_preference(self, keys, value, description, destination="AI 模型与后端列表", message=None,
                        expected_provider=None):
        while True:
            try:
                change = preferences.plan(keys, value)
                if expected_provider is not None and change is not None:
                    name, expected = expected_provider
                    baseline = tomllib.loads((change.before or b"").decode("utf-8"))
                    if providers(baseline).get(name) != expected:
                        raise AskError("调用方式在获取目录后发生变化；请返回重新选择，未保存旧目录中的型号")
                self.say("\n确认要保存的设置", "title")
                self.say(f"{description}\n配置文件：{core.config_path()}")
                self.say("不修改或显示密钥，不调用模型验证。保存前备份，保留其他字段与注释。")
                if change is None:
                    self.say(f"设置已经相同，返回{destination}。")
                    return
                self.menu(f"y 确认保存，返回{destination}\nn 不保存，返回{destination}")
                answer = self.ask("保存？[y/N]: ").lower()
                if answer != "y":
                    self.say(f"已取消本次修改，返回{destination}。")
                    return
                with settings_lock():
                    backups = startup.apply([change])
                self.ui.notice("设置已保存", message or f"AI 设置无需重开终端；返回{destination}。", "success")
                for backup in backups:
                    self.say(f"恢复备份：{backup}")
                return True
            except (AskError, OSError, RuntimeError) as exc:
                self.error(exc)
                if self.ask(f"输入 1 重新预览，0 返回{destination} [0]: ") != "1":
                    return

    def color_menu(self):
        while True:
            config = core.load_config()
            saved, _ = saved_mode(config)
            policy = self.ui.policy
            self.say("\n颜色显示", "title")
            self.say(f"已保存偏好：{saved}\n本次策略：{policy.mode}（来源：{policy.source}）")
            for name, stream in (("标准输出", self.writer), ("诊断输出", sys.stderr)):
                enabled, reason = policy.status(stream)
                self.say(f"{name}：{'有色' if enabled else '无色'}（{reason}）")
            self.menu("1 自动：跟随输出环境与 NO_COLOR（推荐）\n2 始终开启：包括重定向的人类可读输出\n3 始终关闭：纯文本，不使用颜色或加粗\n0 返回查看当前配置")
            self.say("命令、JSON、初始化脚本、剪贴板和执行内容始终不添加样式。")
            choice = self.ask()
            if choice in ("", "0"):
                return
            if choice not in ("1", "2", "3"):
                self.say("请选择 0、1、2 或 3。")
                continue
            value = {"1": "auto", "2": "always", "3": "never"}[choice]
            note = "本次显示保持不变，下次启动采用新的颜色偏好。"
            if self.explicit_color is not None:
                note += f"当前会话仍按 --color {self.explicit_color} 显示。"
            if value == "auto":
                note += "自动模式尊重 NO_COLOR，并分别检查输出是否为终端及 TERM。"
            elif value == "always":
                note += "此程序偏好会覆盖 NO_COLOR，包括重定向的人类文本；受保护数据仍纯净。"
            self.save_preference(("ui", "color"), value, f"颜色偏好：{saved} → {value}\n{note}",
                                 destination="查看当前配置", message=note + "返回查看当前配置。")
            return


def main(argv=None, reader=None, writer=None) -> int:
    from .terminal import configure_stdio
    configure_stdio()
    configure()
    parser = argument_parser(prog="askai-config", description="管理 Shell 集成、AI 模型、API 密钥和默认后端；不会自动发起模型生成。")
    add_color_argument(parser)
    parser.add_argument("--shell", choices=SHELLS)
    parser.add_argument("--status", action="store_true", help="只读查看终端和 AI 配置状态")
    parser.add_argument("--startup-file", action="append", default=[], help="显式选择启动文件，可重复；仅作用于初始 Shell，仍检查状态索引的旧位置")
    parser.add_argument("--executable", help="初始 Shell 的实际程序路径")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    configure(explicit=args.color)
    writer, reader = writer or sys.stdout, reader or sys.stdin
    if any(any(ord(c) < 32 for c in p) for p in args.startup_file):
        raise AskError("配置路径不能包含控制字符")
    wizard = Wizard(args, reader, writer)
    if args.status:
        success = True
        config_error = None
        try:
            config = core.load_config()
            configure(config, args.color)
            items = providers(config)
        except (AskError, OSError, RuntimeError) as exc:
            config_error = exc
        try:
            state = read_state()
        except (AskError, OSError, RuntimeError) as exc:
            wizard.ui.notice("设置状态异常", str(exc), "error")
            state = {"locations": {}}
            success = False
        if not show_status([args.shell] if args.shell else SHELLS, state, writer,
                           [expand_path(p).absolute() for p in args.startup_file] or None):
            success = False
        if config_error is not None:
            wizard.ui.notice("AI 配置异常", str(config_error), "error")
            return 1
        wizard.say(f"AI 配置文件：{core.config_path()}；已保存默认后端：{config.get('provider', 'auto')}")
        for name, provider in items.items():
            try:
                wizard.say(f"{name}：模型 {wizard.model_text(provider)}；{wizard.readiness(provider)}")
            except (AskError, OSError, RuntimeError) as exc:
                wizard.ui.notice(f"{name} 配置异常", str(exc), "error")
                success = False
        wizard.environment_note()
        return 0 if success else 1
    config = core.load_config()
    configure(config, args.color)
    if not reader.isatty() or not writer.isatty():
        raise AskError("设置向导需要交互终端；脚本可使用 askai-config --status")
    wizard.say("修改文件前会展示将保存的设置，由你确认。每层菜单可继续操作，输入 0 返回；EOF 退出，未确认的修改不保存。")
    try:
        wizard.run()
    except _EndSetup:
        wizard.say("输入已结束，未确认的修改已取消。")
    return 0
