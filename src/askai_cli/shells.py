"""Generate opt-in shell functions without editing startup files."""

from pathlib import Path
import shlex
import sys

from .registry import SHORTCUTS
from .execution import PROTOCOL, SELECTED, EXECUTED, WRAPPER_FLAG


def _hook(shell: str, runner: str) -> str:
    if shell == "fish":
        return '''functions -q fish_command_not_found; or functions fish_command_not_found >/dev/null 2>&1
set -l _askai_current (functions fish_command_not_found | string collect)
if not set -q __askai_handler_definition; and begin; set -q __askai_hook_installed; or functions -q __askai_original_not_found; end
    printf '%s\\n' 'askai: 检测到旧版或未确认的 handler 备份，保留当前处理；请重开终端加载新配置。' >&2
else if set -q __askai_handler_definition; and test "$_askai_current" != "$__askai_handler_definition"
    printf '%s\\n' 'askai: fish handler 已被其他插件修改，保留当前 handler；显式 askai 仍可使用。' >&2
else
    if not set -q __askai_handler_definition; and functions -q fish_command_not_found
        functions -c fish_command_not_found __askai_original_not_found
    end
    function fish_command_not_found
        if status is-interactive
            if test (count $argv) -ge 2; or string match --quiet --regex '(?s)\\p{Han}.*\\p{Han}' -- "$argv[1]"
                set -lx ASKAI_HOOK_INVOCATION 1
                askai -- $argv
                return $status
            end
        end
        if functions -q __askai_original_not_found
            __askai_original_not_found $argv
        else
            printf 'fish: Unknown command: %s\\n' "$argv[1]" >&2
        end
        return 127
    end
    set -gu __askai_handler_definition (functions fish_command_not_found | string collect)
end
'''
    name = "command_not_found_handler" if shell == "zsh" else "command_not_found_handle"
    check = f"(( $+functions[{name}] ))" if shell == "zsh" else f"declare -F {name} >/dev/null"
    unexport = "typeset -g +x __askai_handler_definition" if shell == "zsh" else "export -n __askai_handler_definition"
    han = f"{runner} __askai_han_probe --"
    return f'''if {"(( BASH_VERSINFO[0] < 4 ))" if shell == "bash" else "false"}; then
    printf '%s\\n' 'askai: Bash 4+ 才支持未知命令接管；当前版本仅启用显式集成。' >&2
elif {check} && [ "$(typeset -f {name})" != "${{__askai_handler_definition-}}" ]; then
    printf '%s\\n' 'askai: 保留已有 {name}；显式 askai 仍可使用。' >&2
else
    {name}() {{
        if [[ $- == *i* ]]; then
            if [ "$#" -ge 2 ]; then
                ASKAI_HOOK_INVOCATION=1 askai -- "$@"
                return $?
            fi
            case "$1" in
                *[!a-zA-Z0-9_./-]*)
                    if command {han} "$1"; then
                        ASKAI_HOOK_INVOCATION=1 askai -- "$@"
                        return $?
                    fi ;;
            esac
        fi
        printf '%s: command not found: %s\\n' '{shell}' "$1" >&2
        return 127
    }}
    __askai_handler_definition=$(typeset -f {name})
    {unexport}
fi
'''


def shortcuts(shell: str) -> str:
    if shell == "fish":
        return "".join(f"function {name} --wraps askai\n    askai --provider {provider} $argv\nend\n"
                       for name, provider in SHORTCUTS.items())
    return "".join(f'{name}() {{ askai --provider {provider} "$@"; }}\n'
                   for name, provider in SHORTCUTS.items())


def _launcher(launcher: tuple[str, ...] | None) -> tuple[str, ...]:
    """Bind generated functions to the installation, without resolving opt links.

    Entry scripts may supply their trusted stable path. Normal virtualenvs use
    their unversioned interpreter; other library callers retain the isolated
    bootstrap, whose lifetime is limited to that interpreter and package root.
    """
    if launcher is not None:
        return launcher
    prefix = Path(sys.prefix)
    if (prefix / "pyvenv.cfg").is_file() and Path(__file__).resolve().is_relative_to(prefix.resolve()):
        return (str(prefix / "bin/python"), "-I", "-m", "askai_cli")
    code = (f"import sys; sys.path.insert(0, {str(Path(__file__).absolute().parents[1])!r}); "
            "from askai_cli.cli import main; main()")
    return (sys.executable, "-I", "-c", code)


def integration(shell: str, hook: bool = False, aliases: bool = False,
                *, launcher: tuple[str, ...] | None = None) -> str:
    runner = " ".join(shlex.quote(s) for s in _launcher(launcher))
    if shell == "fish":
        script = f'''function askai --description 'Ask AI for a shell command'
    set -l _askai_tmp /tmp
    if set -q TMPDIR; and test -n "$TMPDIR"
        set _askai_tmp "$TMPDIR"
    end
    set -l _askai_file (command mktemp "$_askai_tmp/askai.XXXXXXXXXX")
    or return 1
    command env ASKAI_SHELL=fish ASKAI_WRAPPER_PROTOCOL={PROTOCOL} ASKAI_COMMAND_FILE="$_askai_file" {runner} {WRAPPER_FLAG} $argv
    set -l _askai_rc $status
    set -l _askai_payload
    if test $_askai_rc -eq {SELECTED}; or test $_askai_rc -eq {EXECUTED}
        set _askai_payload (string collect < "$_askai_file")
    end
    command rm -f -- "$_askai_file"
    set -e _askai_file
    if test $_askai_rc -eq {SELECTED}
        if not test -n "$_askai_payload"
            return 1
        end
        set -lu ASKAI_COMMAND_FILE
        set -lu ASKAI_WRAPPER_PROTOCOL
        set -lu ASKAI_HOOK_INVOCATION
        builtin eval "$_askai_payload
"
        return $status
    else if test $_askai_rc -eq {EXECUTED}
        if string match -rq '^[0-9]{{1,3}}$' -- "$_askai_payload"; and test "$_askai_payload" -le 255
            return $_askai_payload
        end
        return 1
    end
    return $_askai_rc
end
'''
    else:
        script = f'''askai() {{
    local _askai_file _askai_rc _askai_payload
    _askai_file=$(command mktemp "${{TMPDIR:-/tmp}}/askai.XXXXXXXXXX") || return 1
    if command env ASKAI_SHELL={shell} ASKAI_WRAPPER_PROTOCOL={PROTOCOL} ASKAI_COMMAND_FILE="$_askai_file" {runner} {WRAPPER_FLAG} "$@"; then
        _askai_rc=0
    else
        _askai_rc=$?
    fi
    if [ "$_askai_rc" -eq {SELECTED} ] || [ "$_askai_rc" -eq {EXECUTED} ]; then
        _askai_payload=$(command cat -- "$_askai_file") || _askai_rc=1
    fi
    command rm -f -- "$_askai_file"
    unset _askai_file
    if [ "$_askai_rc" -eq {SELECTED} ]; then
        [ -n "$_askai_payload" ] || return 1
        unset ASKAI_COMMAND_FILE ASKAI_WRAPPER_PROTOCOL ASKAI_HOOK_INVOCATION
        builtin eval -- "$_askai_payload
"
        return $?
    elif [ "$_askai_rc" -eq {EXECUTED} ]; then
        case "$_askai_payload" in
            ''|*[!0-9]*) return 1 ;;
        esac
        [ "${{#_askai_payload}}" -le 3 ] && [ "$_askai_payload" -le 255 ] || return 1
        return "$_askai_payload"
    fi
    return "$_askai_rc"
}}
'''
    if hook:
        script += _hook(shell, runner)
    return script + (shortcuts(shell) if aliases else "")
