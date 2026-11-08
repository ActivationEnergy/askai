// Override one headless call's selection without replacing its stored config.
export const name = "askai-call-settings";
export const inject = ["agentDefaultModel"];

export function apply(ctx, config) {
  const selection = ctx.get("agentDefaultModel");
  if (typeof selection?.currentSelection !== "function" || typeof ctx.effect !== "function") {
    throw new Error("askai: this DSH version cannot apply per-call model settings");
  }
  if (!["off", "low", "high", "max"].includes(config?.reasoningEffort) ||
      (config.model !== undefined && typeof config.model !== "string")) {
    throw new Error("askai: invalid per-call model settings");
  }
  const overrides = { reasoningEffort: config.reasoningEffort };
  if (config.model !== undefined) overrides.model = config.model;
  ctx.effect(() => {
    const original = selection.currentSelection;
    selection.currentSelection = function () {
      return { ...original.call(this), ...overrides };
    };
    return () => { selection.currentSelection = original; };
  });
  // The headless runner must not start before the override has been installed.
  ctx.provide("askaiCallSettings", Object.freeze({ ready: true }));
}
