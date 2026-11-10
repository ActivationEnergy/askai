"""Review and retry answers in memory; never save defaults or execute commands."""

from dataclasses import dataclass, replace
import os

from .credentials import api_key
import subprocess

from .core import AskError, api_key_env_name
from .interaction import clear_pending_input, read_choice, read_text, record_choice, supports_keys
from .providers import available, list_api_models, model_id_allowed, providers
from .registry import DEFAULTS, MENU_ORDER, common_models, display_name, reasoning_levels, implicit_model_label
from .terminal import Terminal


EFFORT_LABELS = {None: "沿用 Codex 配置", "off": "关闭", "low": "低", "medium": "中",
                 "high": "高", "xhigh": "更高（xhigh）", "max": "最高（max）", "ultra": "超高（ultra）"}


@dataclass(frozen=True)
class AnswerState:
    name: str
    provider: dict
    model: str | None
    question: str
    answer: dict
    version: int = 1


def select_option(title, labels, reader, writer):
    """Select a paged list by one digit, or return None on cancellation."""
    ui = Terminal(writer)
    page = 0
    while True:
        start = page * 9
        items = labels[start:start + 9]
        ui.line(title, "title")
        for i, label in enumerate(items, 1):
            ui.line(f"{i} {label}")
        keys = {"0": "返回"}
        if start:
            keys["p"] = "上一页"
        if start + 9 < len(labels):
            keys["n"] = "下一页"
        ui.line("  ".join(f"{key} {label}" for key, label in keys.items()))
        single_keys = supports_keys(reader, writer)
        if single_keys:
            value = read_choice(reader, writer, len(items), extra=keys, option_mode=True, labels=items)
        else:
            clear_pending_input(reader)
            ui.prompt("请选择 [0]: ")
            line = reader.readline()
            entered = line.strip() or ("Enter" if line else "EOF")
            value = line.strip().lower()
        if value in ("", "0", "q"):
            if not single_keys:
                record_choice(writer, entered, "返回")
            return None
        if value == "n" and "n" in keys:
            if not single_keys:
                record_choice(writer, entered, keys[value])
            page += 1
        elif value == "p" and "p" in keys:
            if not single_keys:
                record_choice(writer, entered, keys[value])
            page -= 1
        elif len(value) == 1 and value.isascii() and value.isdigit() and 1 <= int(value) <= len(items):
            if not single_keys:
                record_choice(writer, entered, items[int(value) - 1])
            return start + int(value) - 1


def select_model(state, config, reader, writer):
    """Offer configured CLI choices and API catalogs without guessing CLI IDs."""
    ui = Terminal(writer)
    items = providers(config)
    names = [name for name in (*MENU_ORDER, *items) if name in items and available(items[name])]
    names = list(dict.fromkeys(names))
    number = select_option("换模型 · 仅用于本次问题", [display_name(n) for n in names], reader, writer)
    if number is None:
        return None
    name = names[number]
    provider = dict(state.provider if name == state.name else items[name])
    api = provider.get("type") == "api"
    if provider.get("type") == "command":
        return replace(state, name=name, provider=provider, model=None)
    key = api_key(provider) if api else ""
    choices = [(m.label, m.id) for m in common_models(name, provider)]
    current = state.model if name == state.name and state.model else provider.get("model")
    if current and model_id_allowed(current, key) and all(value != current for _, value in choices):
        choices.insert(0, (f"当前配置：{current}", current))
    if not api:
        choices.append((implicit_model_label(provider), None))
        ui.line("Claude 使用隔离设置；可选择 AskAI 中已配置的型号。" if provider.get("type") == "claude" else "CLI 原生型号请在其自身设置中选择；此处可沿用配置。")
        if len(choices) == 1:
            provider.pop("model", None)
            return replace(state, name=name, provider=provider, model=None)
    while True:
        labels = [label for label, _ in choices]
        if api:
            labels.append("从服务商获取模型列表")
        number = select_option(f"{display_name(name)} · 选择具体模型", labels, reader, writer)
        if number is None:
            return None
        if number < len(choices):
            model = choices[number][1]
            if model is None:
                provider.pop("model", None)
            return replace(state, name=name, provider=provider, model=model)
        try:
            ids = list_api_models(provider)
            choices = [(value, value) for value in ids if model_id_allowed(value, key)]
            if not choices:
                ui.notice("没有可选模型", "可返回并保留当前答案。")
        except AskError as exc:
            ui.notice("获取模型列表失败", str(exc), "error")


def review_answers(data, name, provider, model, question, config, reader, writer,
                   shell, request, select_action, perform=None):
    """Retry transactionally: commit answer/settings only after a valid response.

    A cancelled or failed attempt leaves the current answer and its question
    untouched. History contains successful answers only and stays in memory.
    """
    ui = Terminal(writer)
    state = AnswerState(name, dict(provider), model, question, data)
    history = [state]
    position = 0
    next_version = 2
    while True:
        levels = reasoning_levels(state.provider)
        if state.provider.get("type") == "codex":
            levels = (None, *levels)
        effort = state.provider.get("reasoning_effort", DEFAULTS.get(state.provider.get("type"), {}).get("reasoning_effort")) if levels else None
        chosen = state.model or state.provider.get("model") or implicit_model_label(state.provider)
        if state.provider.get("type") == "api":
            secret = api_key(state.provider)
            if secret and secret in str(chosen):
                chosen = "型号设置已隐藏（疑似凭据）"
        label = f"回答 {state.version} · 本次使用：{display_name(state.name)} · {chosen}"
        if levels:
            label += f" · 推理：{EFFORT_LABELS.get(effort, effort)}（请求设置）"
        ui.line(label)
        actions = {"m": "换模型", "a": "补充要求"}
        if levels:
            actions = {"r": "推理强度", **actions}
        if position > 0:
            actions["b"] = "上一版"
        if position + 1 < len(history):
            actions["f"] = "下一版"
        action, index, destination = select_action(state.answer, reader, writer, shell, options=actions)
        if action == "retry-menu":
            continue
        if action not in actions:
            if perform is None:
                return state.answer, action, index, destination
            try:
                return perform(state.answer, action, index, destination)
            except (AskError, OSError, subprocess.SubprocessError) as exc:
                ui.notice("操作未完成", str(exc), "error")
                ui.line("答案已保留，可选择其他操作或退出。")
                continue
        if action in ("b", "f"):
            position += -1 if action == "b" else 1
            state = history[position]
            continue
        proposed = None
        try:
            if action == "r":
                if state.provider.get("type") == "codex":
                    ui.line("可用档位取决于 Codex 版本与模型；请求失败时保留当前答案。")
                labels = [EFFORT_LABELS[value] + ("（当前）" if value == effort else "") for value in levels]
                selected = select_option("推理强度 · 选择后重新回答", labels, reader, writer)
                if selected is not None and levels[selected] != effort:
                    next_provider = dict(state.provider)
                    if levels[selected] is None:
                        next_provider.pop("reasoning_effort", None)
                    else:
                        next_provider["reasoning_effort"] = levels[selected]
                    proposed = replace(state, provider=next_provider)
            elif action == "m":
                proposed = select_model(state, config, reader, writer)
                if proposed and (proposed.name, proposed.provider, proposed.model or proposed.provider.get("model")) == (state.name, state.provider, state.model or state.provider.get("model")):
                    proposed = None
            elif action == "a":
                text = read_text("补充要求（回车提交，空白返回）: ", reader, writer)
                if text:
                    proposed = replace(state, question=state.question + "\n补充要求：" + text)
            if proposed is None:
                continue
            answer = request(proposed)
            if not answer["commands"]:
                ui.notice("需要补充信息", answer["explanation"])
                ui.line("保留当前答案；可再次补充要求。")
                continue
        except KeyboardInterrupt:
            ui.line("已取消重新回答，保留当前答案。")
            continue
        except (AskError, OSError, subprocess.SubprocessError) as exc:
            ui.notice("重新回答失败", str(exc), "error")
            ui.line("已保留当前答案和设置。")
            continue
        state = replace(proposed, answer=answer, version=next_version)
        history = history[:position + 1] + [state]
        position += 1
        next_version += 1
