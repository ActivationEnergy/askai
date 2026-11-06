"""Build a process-scoped DSH policy with an enforced no-tools prerequisite."""

import json
from pathlib import Path

from .core import AskError


def dsh_patch(model: str | None = None, system: str | None = None) -> str:
    """Return the patch used only by this invocation, not saved DSH settings.

    The headless driver cannot mount until the packaged guard has mounted and
    published its capability marker. Unknown DSH APIs fail before that marker is
    provided instead of silently running without the tool execution guard.
    """
    guard = Path(__file__).with_name("prompts") / "dsh-no-tools.mjs"
    if not guard.is_file():
        raise AskError("缺少 DSH 工具隔离模块；未启动 DSH，请重新安装 askai")
    persona = system if system is not None else (
        "You propose terminal commands as JSON for the user to review. "
        "No tools are available. Do not attempt to read files or run commands.")
    patch = '''- id: sandbox-policy
  config:
    mode: read-only
- id: approval
  config:
    policy: never
- id: permission
  config:
    defaultPreset: read-only
    presets:
      read-only:
        sandbox: read-only
        approval: never
- id: credentials
  disabled: false
- id: authorization
  disabled: false
- id: deepseek-account
  disabled: false
- id: tools
  config:
    mode: native
- id: system-prompt
  config:
    personaPrefix: ''' + json.dumps(persona, ensure_ascii=True) + '''
- insert:
    - id: askai-no-tools
      name: ''' + json.dumps(str(guard), ensure_ascii=False) + '''
- id: headless-runner
  inject: [headlessStartup, askaiToolGuard]
'''
    if model:
        patch += "- id: agent-default-model\n  config:\n    model: " + json.dumps(model, ensure_ascii=False) + "\n"
    return patch
