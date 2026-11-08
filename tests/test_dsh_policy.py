"""Process policy tests; no real model task or user DSH settings are used."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from askai_cli.core import AskError
from askai_cli.dsh_policy import dsh_patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src/askai_cli/prompts/dsh-no-tools.mjs"


class DshPolicyTests(unittest.TestCase):
    def test_patch_keeps_sandbox_credentials_and_requires_packaged_guard(self):
        model = 'model/"quoted"\n- id: unwanted'
        value = dsh_patch(model)
        for expected in ("mode: read-only", "policy: never", "defaultPreset: read-only",
                         "- id: credentials\n  disabled: false", "- id: askai-no-tools",
                         "inject: [headlessStartup, askaiToolGuard, askaiCallSettings]", "mode: native",
                         json.dumps(str(PLUGIN)), json.dumps(model)):
            self.assertIn(expected, value)
        self.assertNotIn("--permission-mode", value)
        with patch("askai_cli.dsh_policy.Path.is_file", return_value=False), self.assertRaises(AskError):
            dsh_patch()

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the optional DSH plugin runtime tests")
    def test_guard_order_and_unsupported_api_fail_before_marker(self):
        script = '''
import assert from 'node:assert/strict';
const guard = await import(process.argv[2]);
const events = [];
let agentListener;
guard.apply({
  tools: {guard(fn) {assert.equal(typeof fn({}), 'string');events.push('guard');return ()=>{};}},
  on(event,fn) {assert.equal(event,'agent/created');agentListener=fn;events.push('listener');},
  provide(name,value) {assert.equal(name,'askaiToolGuard');assert(value.noTools);events.push('marker');}
});
assert.deepEqual(events,['guard','listener','marker']);
let filter;
agentListener({agent:{ctx:{get(name){assert.equal(name,'tools');return {restrict(value){filter=value;}};}}}});
assert.deepEqual(filter,{allow:[]});
assert.throws(()=>agentListener({agent:{ctx:{get(){return {};}}}}),/cannot hide/);
let published=false;
assert.throws(()=>guard.apply({tools:{},on(){},provide(){published=true;}}),/cannot enforce/);
assert.equal(published,false);
console.log('PASS: guard and serial callback installed before driver marker; unknown APIs fail closed');
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-", PLUGIN.as_uri()],
                                input=script, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_dsh_registry_denies_global_and_scoped_tools(self):
        executable = shutil.which("dsh")
        node = shutil.which("node")
        if not executable or not node:
            self.skipTest("DSH and Node are not installed; no model invocation is substituted")
        package = Path(executable).resolve().parent.parent
        modules = package / "node_modules/@deepseek-ai"
        libraries = [modules / name / "lib/index.js" for name in ("cordis", "dsh-tools", "dsh-scope")]
        if not all(path.is_file() for path in libraries):
            self.skipTest("Installed DSH does not expose testable local runtime modules")
        script = '''
import assert from 'node:assert/strict';
const {Context}=await import(process.argv[2]);
const {default:ToolRuntime}=await import(process.argv[3]);
const {createScope,scopeTarget}=await import(process.argv[4]);
const guard=await import(process.argv[5]);
const ctx=new Context();
ctx.provide('systemPrompt',{tools(){},section(){},getSectionOrder(){return 0;}});
ctx.plugin(ToolRuntime);
await new Promise(resolve=>setTimeout(resolve,0));
let calls=0;
function fixture(name){return {
  name,description:'harmless fixture',parameters:{type:'object',properties:{},additionalProperties:false},
  output:{schema:{type:'string'},render(args,value){return [{type:'text',text:value}];}},
  async execute(){calls++;return 'ran';}
};}
const input=(name,agent)=>({callId:name,name,arguments:{},signal:new AbortController().signal,...(agent?{agent}:{})});
try {
  ctx.tools.register(fixture('global_fixture'));
  const before=await ctx.tools.execute(input('global_fixture'));
  assert.equal(before.isError,false);assert.equal(calls,1);
  let driverReady=false;
  ctx.plugin({name:'fake-headless-driver',inject:['askaiToolGuard'],apply(c){
    assert(c.get('askaiToolGuard').noTools);
    assert.equal(typeof ctx.tools.guardReason({}),'string');driverReady=true;
  }});
  await new Promise(resolve=>setTimeout(resolve,0));assert.equal(driverReady,false);
  ctx.plugin(guard);
  await new Promise(resolve=>setTimeout(resolve,0));assert(driverReady);
  // Another pre-execute handler cannot force allow past a monotonic guard.
  ctx.on('tools/pre-execute',()=>({kind:'allow'}));
  const denied=await ctx.tools.execute(input('global_fixture'));
  assert.equal(denied.isError,true);assert.equal(calls,1);
  // Future global registrations are also covered by the existing guard.
  ctx.tools.register(fixture('late_fixture'));
  assert.equal((await ctx.tools.execute(input('late_fixture'))).isError,true);
  assert.equal(calls,1);
  const agent={};agent.ctx=createScope(ctx,agent).ctx;
  await ctx.serial(scopeTarget(agent,agent),'agent/created',{agent,source:'fresh'});
  assert.equal(ctx.tools.wireSchemas(agent).schemas.length,0);
  // A scope-local tool is exempt from inherited schema masks, but never from
  // the global execution guard.
  agent.ctx.get('tools').register(fixture('scoped_fixture'));
  assert.equal((await ctx.tools.execute(input('scoped_fixture',agent))).isError,true);
  assert.equal(calls,1);
  console.log('PASS: real DSH registry denies existing/late/scoped tool bodies and hides inherited schemas');
} finally { await ctx.fiber.dispose(); }
'''
        result = subprocess.run([node, "--input-type=module", "-", *[p.as_uri() for p in libraries], PLUGIN.as_uri()],
                                input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_dsh_loader_enforces_driver_dependency_without_model(self):
        executable = shutil.which("dsh")
        if not executable or not (Path(executable).resolve().parent.parent / "node_modules/@deepseek-ai/dsh-tools").is_dir():
            self.skipTest("No locally testable DSH installation")
        with tempfile.TemporaryDirectory(prefix="askai-dsh-loader-", dir="/private/tmp") as folder:
            root = Path(folder)
            driver = root / "driver.mjs"
            driver.write_text('''export const name="fixture-driver";
export const inject=["tools","askaiToolGuard"];
export function apply(ctx){
  if(!ctx.get("askaiToolGuard")?.noTools || typeof ctx.tools.guardReason({})!=="string") throw Error("guard absent");
  process.stdout.write("ASKAI_GUARD_READY\\n");
  process.exit(0);
}
''')
            # Disable the real driver before startup. This fixture cannot create
            # an agent or request a model, even if policy composition regresses.
            policy = dsh_patch(system='ASKAI_SYSTEM_POLICY_SENTINEL\n策略与 "quoted" 协议').replace("- id: headless-runner\n  inject:",
                                         "- id: headless-runner\n  disabled: true\n  inject:")
            policy += "\n- insert:\n    - id: fixture-driver\n      name: " + json.dumps(str(driver)) + "\n      inject: [askaiToolGuard, tools]\n"
            overlay = root / "policy.yml"
            overlay.write_text(policy)
            # Deliberately do not inherit API keys, DSH settings, or proxy env.
            env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
            env.update(HOME=str(root / "home"), DSH_HOME=str(root / "dsh"),
                       DSH_TELEMETRY_DISABLED="1", TMPDIR=str(root))
            base = [executable, "--profile", "headless", "--patch", str(overlay)]
            composition = subprocess.run([*base, "--dump-config"], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(composition.returncode, 0, composition.stderr)
            headless = composition.stdout.split("- id: headless-runner", 1)[1].split("\n- id:", 1)[0]
            self.assertIn("askaiToolGuard", headless)
            self.assertIn("disabled: true", headless)
            self.assertIn("ASKAI_SYSTEM_POLICY_SENTINEL", composition.stdout)
            result = subprocess.run([*base, "fixture-task-never-sent-to-model"], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ASKAI_GUARD_READY", result.stdout)


if __name__ == "__main__":
    unittest.main()
