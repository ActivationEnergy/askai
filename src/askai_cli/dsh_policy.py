"""Build a process-scoped DSH policy with an enforced no-tools prerequisite."""

import json
from pathlib import Path

from .core import AskError
from .registry import DEFAULTS, PROVIDER_SPECS


def dsh_patch(model: str | None = None, system: str | None = None, reasoning_effort: str = DEFAULTS["dsh"]["reasoning_effort"]) -> str:
    """Return the patch used only by this invocation, not saved DSH settings.

    The headless driver cannot mount until the packaged guard has mounted and
    published its capability marker. Unknown DSH APIs fail before that marker is
    provided instead of silently running without the tool execution guard.
    """
    if reasoning_effort not in PROVIDER_SPECS["dsh"].reasoning_levels:
        raise AskError("DSH 推理强度须为 off、low、high 或 max")
    guard = Path(__file__).with_name("prompts") / "dsh-no-tools.mjs"
    settings = guard.with_name("dsh-call-settings.mjs")
    if not guard.is_file() or not settings.is_file():
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
  config: {}
- id: system-prompt
  config:
    personaPrefix: ''' + json.dumps(persona, ensure_ascii=True) + '''
- insert:
    - id: askai-no-tools
      name: ''' + json.dumps(str(guard), ensure_ascii=False) + '''
- id: headless-runner
  inject: [headlessStartup, askaiToolGuard, askaiCallSettings]
'''
    # Reset legacy tools config: <=0.2.1 defaults to native; >=0.2.2 rejects
    # tools.mode and mounts the native executor separately in the base profile.
    # DSH id-targeted config patches replace the whole config, not individual
    # fields. Keep the native provider/model config and override its getter only
    # within this process, after the original service has activated.
    patch += "- insert:\n    - id: askai-call-settings\n      name: " + json.dumps(str(settings)) + "\n      config:\n        reasoningEffort: " + json.dumps(reasoning_effort) + "\n"
    if model:
        patch += "        model: " + json.dumps(model, ensure_ascii=False) + "\n"
    return patch
