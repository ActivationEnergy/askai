"""Built-in backend metadata, shared by selection, settings and shell aliases."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    shortcut: str
    defaults: Mapping[str, str | bool]
    menu_order: int
    auto_order: int | None = None
    models: tuple[ModelSpec, ...] = ()
    models_source: str = ""
    models_checked_on: str = ""

    def __post_init__(self):
        # Configuration merging must never mutate the built-in preset.
        models = tuple(self.models)
        defaults = dict(self.defaults)
        if models:
            defaults["model"] = models[0].id
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "defaults", MappingProxyType(defaults))


SPECS = (
    ProviderSpec("codex", "Codex", "askgpt",
                 {"type": "codex", "executable": "codex", "web_search": True}, 1, 1),
    # Claude remains explicit-only, even when its CLI is installed.
    ProviderSpec("claude", "Claude Code", "askcc",
                 {"type": "claude", "executable": "claude"}, 4),
    ProviderSpec("dsh", "DSH", "askdsh",
                 {"type": "dsh", "executable": "dsh"}, 3, 2),
    ProviderSpec("deepseek", "DeepSeek", "askds",
                 {"type": "api", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"}, 2, 3,
                 (ModelSpec("deepseek-flash", "DeepSeek Flash"), ModelSpec("deepseek-v4-pro", "DeepSeek Pro")),
                 models_source="https://api-docs.deepseek.com/quick_start/pricing/", models_checked_on="2026-11-06"),
    ProviderSpec("kimi", "Kimi", "askkimi",
                 {"type": "api", "base_url": "https://api.moonshot.cn/v1", "api_key_env": "MOONSHOT_API_KEY"}, 5, 4,
                 (ModelSpec("kimi-k3.1-pro", "Kimi K3.1 Pro"),
                  ModelSpec("kimi-k3.1-pro-highspeed", "Kimi K3.1 Pro Highspeed"),
                  ModelSpec("kimi-k3", "Kimi K3")),
                 models_source="https://platform.kimi.com/docs/models", models_checked_on="2026-11-06"),
    ProviderSpec("minimax", "MiniMax", "askmm",
                 {"type": "api", "base_url": "https://api.minimax.cn/v1", "api_key_env": "MINIMAX_API_KEY"}, 6, 5,
                 (ModelSpec("MiniMax-M3.1-Flash", "MiniMax M3.1 Flash"), ModelSpec("MiniMax-M3", "MiniMax M3")),
                 models_source="https://platform.minimax.cn/docs/guides/text-generation", models_checked_on="2026-11-06"),
    ProviderSpec("glm", "GLM", "askglm",
                 {"type": "api", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "GLM_API_KEY"}, 7, 6,
                 (ModelSpec("glm-5.3", "GLM 5.3"), ModelSpec("glm-5.3-flash", "GLM 5.3 Flash"),
                  ModelSpec("glm-5.3-flashx", "GLM 5.3 FlashX")),
                 models_source="https://docs.bigmodel.cn/cn/guide/start/model-overview", models_checked_on="2026-11-06"),
)

PROVIDER_SPECS = MappingProxyType({spec.name: spec for spec in SPECS})
DEFAULTS = MappingProxyType({spec.name: spec.defaults for spec in SPECS})
SHORTCUTS = MappingProxyType({spec.shortcut: spec.name for spec in SPECS})
MENU_ORDER = tuple(spec.name for spec in sorted(SPECS, key=lambda spec: spec.menu_order))
AUTO_ORDER = tuple(spec.name for spec in sorted(
    (spec for spec in SPECS if spec.auto_order is not None), key=lambda spec: spec.auto_order))


def display_name(name: str) -> str:
    """Custom providers keep their configured name rather than a preset label."""
    spec = PROVIDER_SPECS.get(name)
    return spec.label if spec else name


def common_models(name: str, provider: dict) -> tuple[ModelSpec, ...]:
    """Offer presets only for their original API endpoint, never a custom gateway."""
    spec = PROVIDER_SPECS.get(name)
    base = provider.get("base_url")
    if (spec is None or provider.get("type") != "api" or not isinstance(base, str)
            or base.rstrip("/") != spec.defaults.get("base_url")):
        return ()
    return spec.models
