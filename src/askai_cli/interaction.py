"""Lazy, non-full-screen keyboard interaction for real terminals."""

from contextlib import closing
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


def supports_keys(reader, writer):
    return reader.isatty() and writer.isatty() and os.environ.get("TERM", "") not in ("", "dumb")


def read_choice(reader, writer, count, scripts=False, *, extra=None, option_mode=False):
    """Read menu keys; bracketed paste is never interpreted as menu actions."""
    _require_toolkit()
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import create_output

    pending = [""]
    notice = [""]
    invalid_prefix = [False]
    bindings = KeyBindings()
    extra = extra or {}
    actions = {} if option_mode else {"e": "解释", "p": "打印", "c": "复制"}
    if scripts:
        actions.update(v="复制源码", s="保存脚本")
        if "r" not in extra:
            actions["r"] = "复制源码"

    def text():
        if pending[0]:
            value = f"{actions[pending[0]]}哪个方案？按 1–{count}，Esc 返回："
        elif option_mode:
            value = notice[0] + "按编号选择，Esc 返回: "
        else:
            value = notice[0] + "按键选择 [默认 q]: "
        enabled = current_policy().status(writer)[0]
        style = ("bold ansiyellow" if invalid_prefix[0] else "bold ansimagenta") if enabled else ""
        return [(style, value)]

    for digit in range(1, count + 1):
        @bindings.add(str(digit))
        def select(event, digit=digit):
            if invalid_prefix[0]:
                return
            event.app.exit(result=pending[0] + str(digit))

    for action in actions:
        @bindings.add(action)
        @bindings.add(action.upper())
        def begin(event, action=action):
            invalid_prefix[0] = False
            pending[0] = action
            notice[0] = ""
            event.app.invalidate()

    for key in extra:
        @bindings.add(key.upper())
        @bindings.add(key)
        def shortcut(event, key=key):
            event.app.exit(result=key)

    @bindings.add("escape")
    def back(event):
        if pending[0] or invalid_prefix[0]:
            pending[0] = ""
            invalid_prefix[0] = False
            notice[0] = ""
            event.app.invalidate()
        else:
            event.app.exit(result="q")

    @bindings.add("Q")
    @bindings.add("q")
    @bindings.add("enter")
    @bindings.add("c-j")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(result="q")

    @bindings.add("c-c")
    def interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("<bracketed-paste>")
    def paste(event):
        pending[0] = ""
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
    # A key typed while the model was running must not select an unseen answer.
    # prompt_toolkit owns raw-mode restoration; only discard old POSIX input here.
    clear_pending_input(reader)
    with closing(create_input(stdin=reader)) as input_stream:
        app = Application(layout=Layout(Window(FormattedTextControl(text), wrap_lines=True)),
                          key_bindings=bindings, input=input_stream, output=output,
                          full_screen=False, mouse_support=False, erase_when_done=True)
        return app.run()


def read_text(message, reader, writer):
    """Use normal editable text input; Enter submits and Ctrl-C cancels."""
    if not supports_keys(reader, writer):
        Terminal(writer).prompt(message)
        return reader.readline().strip()
    _require_toolkit()
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_input
    from prompt_toolkit.output import create_output
    output = create_output(stdout=writer)
    if hasattr(output, "enable_cpr"):
        output.enable_cpr = False
    with closing(create_input(stdin=reader)) as input_stream:
        try:
            style = "bold ansimagenta" if current_policy().status(writer)[0] else ""
            return PromptSession(input=input_stream, output=output, mouse_support=False).prompt([(style, message)]).strip()
        except EOFError:
            return ""
