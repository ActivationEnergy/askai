"""Generate opt-in shell functions without editing startup files."""

from pathlib import Path
import shlex
import sys

from .registry import SHORTCUTS


def _hook(shell: str) -> str:
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
    probe = ("import sys,unicodedata;sys.exit(0 if sum(unicodedata.name(c,'').startswith("
             "('CJK UNIFIED IDEOGRAPH-','CJK COMPATIBILITY IDEOGRAPH-')) for c in sys.argv[1])>=2 else 1)")
    han = " ".join(shlex.quote(s) for s in (sys.executable, "-I", "-c", probe))
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


def integration(shell: str, hook: bool = False, aliases: bool = False) -> str:
    # Keep stable installation symlinks intact: resolving Homebrew's opt link
    # would freeze this long-lived shell function to a removable Cellar keg.
    code = (f"import sys; sys.path.insert(0, {str(Path(__file__).absolute().parents[1])!r}); "
            "from askai_cli.cli import main; main()")
    runner = " ".join(shlex.quote(s) for s in (sys.executable, "-I", "-c", code))
    if shell == "fish":
        script = f'''function askai --description 'Ask AI for a shell command'
    set -l _askai_file (command mktemp "${{TMPDIR:-/tmp}}/askai.XXXXXXXXXX")
    if test $status -ne 0
        return 1
    end
    command env ASKAI_SHELL=fish ASKAI_COMMAND_FILE="$_askai_file" {runner} $argv
    set -l _askai_rc $status
    if test $_askai_rc -eq 0; and test -s "$_askai_file"
        source "$_askai_file"
        set _askai_rc $status
    end
    command rm -f "$_askai_file"
    return $_askai_rc
end
'''
        # fish does not implement POSIX parameter expansion.
        script = script.replace('    set -l _askai_file (command mktemp "${TMPDIR:-/tmp}/askai.XXXXXXXXXX")',
                                '    set -l _askai_tmp /tmp\n'
                                '    if set -q TMPDIR; and test -n "$TMPDIR"\n'
                                '        set _askai_tmp "$TMPDIR"\n    end\n'
                                '    set -l _askai_file (command mktemp "$_askai_tmp/askai.XXXXXXXXXX")')
        if hook:
            script += _hook(shell)
        return script + (shortcuts(shell) if aliases else "")
    script = f'''askai() {{
    local _askai_file _askai_rc
    _askai_file=$(command mktemp "${{TMPDIR:-/tmp}}/askai.XXXXXXXXXX") || return 1
    if command env ASKAI_SHELL={shell} ASKAI_COMMAND_FILE="$_askai_file" {runner} "$@"; then
        _askai_rc=0
        if [ -s "$_askai_file" ]; then
            . "$_askai_file"
            _askai_rc=$?
        fi
    else
        _askai_rc=$?
    fi
    command rm -f "$_askai_file"
    return "$_askai_rc"
}}
'''
    if hook:
        script += _hook(shell)
    return script + (shortcuts(shell) if aliases else "")
