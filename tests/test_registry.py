"""Registry integration and static distribution entry points must stay aligned."""

import os
from datetime import date
from pathlib import Path
import re
import sys
import tomllib
import unittest
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from askai_cli import providers, registry, setup, shells
from askai_cli.core import AskError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="askai-unit-credentials-")
        self.addCleanup(temporary.cleanup)
        store = patch("askai_cli.credentials.store_path", return_value=Path(temporary.name) / "credentials.json")
        store.start()
        self.addCleanup(store.stop)

    def test_shortcuts_are_fixed_and_every_distributed_entry_point_exists(self):
        expected = {"askgpt": "codex", "askds": "deepseek", "askdsh": "dsh", "askcc": "claude",
                    "askkimi": "kimi", "askmm": "minimax", "askglm": "glm"}
        self.assertEqual(dict(shells.SHORTCUTS), expected)
        scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertEqual(set(scripts), {"askai", "askai-config", *registry.SHORTCUTS})
        for name in scripts:
            self.assertEqual(scripts[name], "askai_cli.cli:main")
            self.assertTrue((ROOT / "bin" / name).is_file(), name)
        formula = (ROOT / "packaging/homebrew/askai.rb").read_text()
        shortcut_lists = re.findall(r"%w\[([^\]]+)\]", formula)
        self.assertTrue(shortcut_lists)
        for value in shortcut_lists:
            # The formula installs and smoke-tests each fixed shortcut.
            if "askgpt" in value:
                self.assertEqual(set(value.split()), set(registry.SHORTCUTS))

    def test_auto_preference_and_explicit_only_claude_survive_registry_merge(self):
        self.assertEqual(registry.AUTO_ORDER, ("codex", "dsh", "deepseek", "kimi", "minimax", "glm"))
        with patch.dict(os.environ, {}, clear=True), patch.object(providers, "available", return_value=True):
            self.assertEqual(providers.select_provider({}, None)[0], "codex")
            self.assertEqual(providers.select_provider({}, "claude")[0], "claude")
        with patch.dict(os.environ, {}, clear=True), patch.object(providers, "available", side_effect=lambda p: p["type"] == "claude"):
            with self.assertRaises(AskError):
                providers.select_provider({}, None)

    def test_overrides_are_isolated_and_custom_providers_keep_their_identity(self):
        first = providers.providers({"providers": {"deepseek": {"type": "command", "executable": "fixture"},
                                                     "private": {"type": "command", "executable": "private"}}})
        self.assertEqual(first["deepseek"], {"type": "command", "executable": "fixture"})
        self.assertEqual(setup.Wizard.backend_text("private", first["private"]), "private · 自定义命令")
        self.assertEqual(setup.Wizard.backend_text("deepseek", first["deepseek"]), "DeepSeek · 自定义命令")
        first["codex"]["web_search"] = False
        self.assertTrue(providers.providers({})["codex"]["web_search"])
        with self.assertRaises(TypeError):
            registry.DEFAULTS["codex"]["web_search"] = False

    def test_registry_has_unique_names_shortcuts_and_order_positions(self):
        specs = registry.SPECS
        for attr in ("name", "shortcut", "menu_order"):
            self.assertEqual(len({getattr(spec, attr) for spec in specs}), len(specs))
        self.assertEqual(registry.MENU_ORDER, ("codex", "deepseek", "dsh", "claude", "kimi", "minimax", "glm"))
        positions = [spec.auto_order for spec in specs if spec.auto_order is not None]
        self.assertEqual(len(set(positions)), len(positions))
        self.assertEqual(set(registry.DEFAULTS), set(registry.MENU_ORDER))

    def test_common_model_metadata_drives_defaults_and_is_endpoint_scoped(self):
        for spec in registry.SPECS:
            if spec.models:
                self.assertEqual(spec.defaults["model"], spec.models[0].id)
                self.assertEqual(registry.common_models(spec.name, dict(spec.defaults)), spec.models)
                changed = {**spec.defaults, "base_url": "https://gateway.example.test/v1"}
                self.assertEqual(registry.common_models(spec.name, changed), ())
        self.assertEqual([item.id for item in registry.PROVIDER_SPECS["deepseek"].models],
                         ["deepseek-flash", "deepseek-v4-pro"])

    def test_custom_endpoint_does_not_inherit_the_official_default_model(self):
        for name, preset in registry.DEFAULTS.items():
            if preset.get("type") != "api":
                continue
            with self.subTest(name=name):
                value = providers.providers({"providers": {name: {"base_url": "https://gateway.example.test/v1"}}})[name]
                self.assertNotIn("model", value)
                explicit = providers.providers({"providers": {name: {"base_url": "https://gateway.example.test/v1", "model": "gateway-model"}}})[name]
                self.assertEqual(explicit["model"], "gateway-model")
                unchanged = providers.providers({"providers": {name: {"base_url": preset["base_url"] + "/"}}})[name]
                self.assertEqual(unchanged["model"], preset["model"])

    def test_current_presets_match_examples_and_record_official_provenance(self):
        expected = {"deepseek": "deepseek-flash", "kimi": "kimi-k3.1-pro",
                    "minimax": "MiniMax-M3.1-Flash", "glm": "glm-5.3"}
        example = tomllib.loads((ROOT / "examples/config.toml").read_text())
        references = (ROOT / "docs/references.md").read_text()
        for name, model in expected.items():
            spec = registry.PROVIDER_SPECS[name]
            self.assertEqual(spec.defaults["model"], model)
            self.assertEqual(example["providers"][name]["model"], model)
            self.assertTrue(spec.models_source.startswith("https://"))
            date.fromisoformat(spec.models_checked_on)
            self.assertIn(spec.models_source, references)
            self.assertIn(spec.models_checked_on, references)
            self.assertIn(f"| {spec.label} | `{model}` |", references)
        self.assertNotIn("kimi-k2.5", [model.id for model in registry.PROVIDER_SPECS["kimi"].models])

    def test_new_defaults_do_not_overwrite_explicitly_saved_models(self):
        config = {"providers": {"kimi": {"model": "kimi-k2.5"}, "glm": {"model": "custom-model"}}}
        current = providers.providers(config)
        self.assertEqual(current["kimi"]["model"], "kimi-k2.5")
        self.assertEqual(current["glm"]["model"], "custom-model")


if __name__ == "__main__":
    unittest.main()
