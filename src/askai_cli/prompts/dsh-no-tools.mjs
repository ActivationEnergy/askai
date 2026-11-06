// Process-scoped Cordis plugin for DSH 0.2.1-rc.5's monotonic tool guards.
// No imports from cwd, user modules, model output, or external packages.
export const name = "askai-no-tools";
export const inject = ["tools"];

export function apply(ctx) {
  if (typeof ctx.tools?.guard !== "function" || typeof ctx.on !== "function" || typeof ctx.provide !== "function") {
    throw new Error("askai: this DSH version cannot enforce no-tools mode");
  }

  // A global guard also covers scoped tools and tools registered later. Other
  // hooks cannot force an allowed execution past this monotonic denial.
  ctx.tools.guard(() => "askai only generates suggestions; tool execution is disabled");

  // Agent creation waits for this serial event before queued turns may run.
  // Hide inherited tool schemas as well as denying their actual execution.
  ctx.on("agent/created", ({ agent }) => {
    // `get()` preserves the agent scope without assuming the headless driver's
    // static dependency list exposes a direct agent.ctx.tools property.
    const tools = agent?.ctx?.get?.("tools");
    if (typeof tools?.restrict !== "function") {
      throw new Error("askai: this DSH version cannot hide agent tools");
    }
    tools.restrict({ allow: [] });
  });

  // The driver declares this as a required dependency. Publish only after the
  // guard and serial initialization listener are both successfully installed.
  ctx.provide("askaiToolGuard", Object.freeze({ noTools: true }));
}
