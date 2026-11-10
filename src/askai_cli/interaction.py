"""Lazy, non-full-screen keyboard interaction for real terminals."""

from contextlib import closing, contextmanager
import os

from .core import AskError
from .terminal import Terminal, current_policy


def _require_toolkit():
    try:
        import prompt_toolkit  # Imported only for interactive terminal input.
    except ImportError as exc:
        raise AskError("交互依赖未安装；源码运行请使用 uv run ./bin/askai，已安装版本请重新安装 askai。") from exc


def clear_pending_input(reader):
    """Discard tty input typed before a menu became visible, including fallback UI."""
    if reader.isatty():
        import termios
        termios.tcflush(reader.fileno(), termios.TCIFLUSH)


class UnsafeTerminalInput(AskError):
    """The terminal cannot provide a safe settings confirmation boundary."""


def read_line(message, reader, writer, *, strip=True):
    """Start a fresh canonical input stage without importing the interactive toolkit."""
    try:
        real_tty = reader.isatty() and os.isatty(reader.fileno())
    except (AttributeError, OSError, ValueError):
        real_tty = False
    if real_tty:
        import termios
        if not termios.tcgetattr(reader.fileno())[3] & termios.ICANON:
            raise UnsafeTerminalInput("终端不处于标准行输入模式，未读取选项；请恢复正常终端后重试")
        clear_pending_input(reader)
    Terminal(writer).prompt(message)
    line = reader.readline()
    return (line.strip() if strip else line) if line else None


@contextmanager
def _prompt_input(reader):
    """Own one prompt's input stream; never replay its keys in another stage.

    The terminal queue and prompt_toolkit's typeahead cache are separate.
    Clear both before reading; always clear the library cache before closing,
    including cancellation and exceptions. Applications own raw-mode restoration.
    """
    _require_toolkit()
    from prompt_toolkit.input import create_input
    from prompt_toolkit.input.typeahead import clear_typeahead
    clear_pending_input(reader)
    with closing(create_input(stdin=reader)) as input_stream:
        clear_typeahead(input_stream)
        try:
            yield input_stream
        finally:
            clear_typeahead(input_stream)


def supports_keys(reader, writer):
    return reader.isatty() and writer.isatty() and os.environ.get("TERM", "") not in ("", "dumb")


def record_choice(writer, key, label):
    """Leave an accepted selection in scrollback, without claiming success."""
    Terminal(writer).line(f"已选择：{key} · {label}")
    writer.flush()


def read_choice(reader, writer, count, scripts=False, *, extra=None, option_mode=False, labels=None,
                menu_options=None, menu_prompt=None, menu_default="0"):
    """Read menu keys; bracketed paste is never interpreted as menu actions."""
    _require_toolkit()
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import create_output

    pending = [""]
    pending_key = [""]
    menu_pending = [""]
    notice = [""]
    invalid_prefix = [False]
    selection = [None]
    bindings = KeyBindings()
    extra = extra or {}
    actions = {} if option_mode or menu_options is not None else {"e": "解释", "p": "打印", "c": "复制"}
    if scripts:
        actions.update(v="复制源码", s="保存脚本")
        if "r" not in extra:
            actions["r"] = "复制源码"

    def finish(event, result, key, label):
        selection[0] = (key, label)
        event.app.exit(result=result)

    def text():
        if menu_options is not None:
            value = notice[0] + ("设为默认：请选择后端编号，Esc 取消，q 退出设置: " if menu_pending[0] else menu_prompt)
        elif pending[0]:
            value = notice[0] + f"{actions[pending[0]]}哪个方案？按 1–{count}，Esc 返回："
        elif option_mode:
            value = notice[0] + "按编号选择，Esc 返回: "
        else:
            value = notice[0] + "按键选择 [默认 q]: "
        enabled = current_policy().status(writer)[0]
        style = ("bold ansiyellow" if invalid_prefix[0] else "bold ansimagenta") if enabled else ""
        return [(style, value)]

    if menu_options is not None:
        if any(key.startswith("d") and key[1:].isdigit() for key in menu_options):
            @bindings.add("d")
            @bindings.add("D")
            def menu_default_prefix(event):
                menu_pending[0] = event.key_sequence[-1].data
                invalid_prefix[0] = False
                notice[0] = ""
                event.app.invalidate()
        for key, label in menu_options.items():
            if key == "q" or key.startswith("d") and key[1:].isdigit():
                continue
            @bindings.add(*key)
            def menu_select(event, key=key, label=label):
                if invalid_prefix[0]:
                    return
                if menu_pending[0]:
                    selected = "d" + key
                    if selected not in menu_options:
                        invalid_prefix[0] = True
                        notice[0] = "无效编号，请按 Esc 重选。 "
                        event.app.invalidate()
                        return
                    finish(event, selected, menu_pending[0] + key, menu_options[selected])
                else:
                    finish(event, key, key, label)
            if key.upper() != key:
                bindings.add(*key.upper())(menu_select)

    for digit in range(1, count + 1):
        @bindings.add(str(digit))
        def select(event, digit=digit):
            if invalid_prefix[0]:
                return
            result = pending[0] + str(digit)
            label = (labels[digit - 1] if labels else f"选项 {digit}") if option_mode else (
                f"{actions.get(pending[0], '执行')}方案 {digit}")
            finish(event, result, pending_key[0] + str(digit), label)

    for action in actions:
        @bindings.add(action)
        @bindings.add(action.upper())
        def begin(event, action=action):
            if count == 1:
                finish(event, action + "1", event.key_sequence[-1].data, f"{actions[action]}方案 1")
                return
            invalid_prefix[0] = False
            pending[0] = action
            pending_key[0] = event.key_sequence[-1].data
            notice[0] = ""
            event.app.invalidate()

    for key in extra:
        @bindings.add(key.upper())
        @bindings.add(key)
        def shortcut(event, key=key):
            finish(event, key, event.key_sequence[-1].data, extra[key])

    @bindings.add("escape")
    def back(event):
        if pending[0] or menu_pending[0] or invalid_prefix[0]:
            pending[0] = ""
            menu_pending[0] = ""
            pending_key[0] = ""
            invalid_prefix[0] = False
            notice[0] = ""
            event.app.invalidate()
        else:
            if menu_options is not None:
                finish(event, "0", "Esc", menu_options.get("0", "不保存并返回"))
            else:
                finish(event, "q", "Esc", "返回" if option_mode else "退出")

    @bindings.add("Q")
    @bindings.add("q")
    @bindings.add("enter")
    @bindings.add("c-j")
    @bindings.add("c-d")
    def cancel(event):
        pressed = event.key_sequence[-1]
        key = {"c-m": "Enter", "c-j": "Enter", "c-d": "Ctrl-D"}.get(pressed.key, pressed.data)
        if menu_options is not None:
            result = "q" if pressed.key in ("c-d",) or pressed.data.lower() == "q" else menu_default
            finish(event, result, key, "退出设置" if result == "q" else menu_options.get(result, "返回"))
        else:
            finish(event, "q", key, "返回" if option_mode else "退出")

    @bindings.add("c-c")
    def interrupt(event):
        selection[0] = ("Ctrl-C", "取消")
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("<bracketed-paste>")
    def paste(event):
        menu_pending[0] = ""
        pending[0] = ""
        pending_key[0] = ""
        invalid_prefix[0] = True
        notice[0] = "已忽略粘贴内容；按 Esc 重选。 "
        event.app.invalidate()

    @bindings.add("<any>")
    def invalid(event):
        invalid_prefix[0] = True
        notice[0] = "无效按键，请按 Esc 重选。 "
        event.app.invalidate()

    output = create_output(stdout=writer)
    # No cursor-position query is needed for a one-line menu.
    if hasattr(output, "enable_cpr"):
        output.enable_cpr = False
    with _prompt_input(reader) as input_stream:
        app = Application(layout=Layout(Window(FormattedTextControl(text), wrap_lines=True)),
                          key_bindings=bindings, input=input_stream, output=output,
                          full_screen=False, mouse_support=False, erase_when_done=True)
        try:
            return app.run()
        finally:
            if selection[0] is not None:
                record_choice(writer, *selection[0])


def read_text(message, reader, writer):
    """Use normal editable text input; Enter submits and Ctrl-C cancels."""
    if not supports_keys(reader, writer):
        return read_line(message, reader, writer) or ""
    _require_toolkit()
    from prompt_toolkit import PromptSession
    from prompt_toolkit.output import create_output
    output = create_output(stdout=writer)
    if hasattr(output, "enable_cpr"):
        output.enable_cpr = False
    with _prompt_input(reader) as input_stream:
        try:
            style = "bold ansimagenta" if current_policy().status(writer)[0] else ""
            return PromptSession(input=input_stream, output=output, mouse_support=False).prompt([(style, message)]).strip()
        except EOFError:
            return ""


def read_secret(reader, writer, *, existing=False):
    """Read a masked credential from a real terminal without input history."""
    if not reader.isatty() or not writer.isatty():
        raise AskError("粘贴密钥需要交互终端；请在终端中运行 askai-config")
    _require_toolkit()
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import DummyHistory
    from prompt_toolkit.output import create_output
    output = create_output(stdout=writer)
    if hasattr(output, "enable_cpr"):
        output.enable_cpr = False
    with _prompt_input(reader) as input_stream:
        try:
            return PromptSession(input=input_stream, output=output, history=DummyHistory(),
                                 mouse_support=False, enable_open_in_editor=False).prompt(
                ("粘贴 API Key（输入隐藏；回车使用已保存密钥，Ctrl-C 取消）: " if existing
                 else "粘贴 API Key（输入隐藏，空白取消）: "), is_password=True).strip()
        except (EOFError, KeyboardInterrupt):
            return None
