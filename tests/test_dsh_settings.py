"""Use DSH's real patch composer and native service, without model/network calls."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from askai_cli.dsh_policy import dsh_patch

ROOT = Path(__file__).resolve().parents[1]


class DshSettingsTests(unittest.TestCase):
    def test_native_composition_activation_and_selection_preservation(self):
        dsh, node = shutil.which('dsh'), shutil.which('node')
        if not dsh or not node:
            self.skipTest('requires installed DSH and Node for native integration')
        modules = Path(dsh).resolve().parent.parent / 'node_modules/@deepseek-ai'
        libraries = [modules / name / 'lib/index.js' for name in
                     ('cordis', 'dsh-app-boot', 'dsh-agent-default-model')]
        if not all(p.is_file() for p in libraries):
            self.skipTest('installed DSH does not expose native test libraries')
        script = '''import assert from 'node:assert/strict';
const {Context} = await import(process.argv[2]);
const {composeEntries,loadOverlayPatches} = await import(process.argv[3]);
const {default:DefaultModel} = await import(process.argv[4]);
const settings = await import(process.argv[5]);
const patch = loadOverlayPatches('askai-test',process.argv[6]);
const expected = JSON.parse(process.argv[7]);
for (const provider of ['deepseek-official','deepseek-account','custom-provider']) {
  const original = {provider,model:'existing-model',reasoningEffort:'max'};
  const base = [{insert:[
    {id:'agent-default-model',config:original},
    {id:'headless-runner'}
  ]}];
  // Reproduce the bug with the actual composer and native config validator.
  const broken = composeEntries([base,[{id:'agent-default-model',config:{reasoningEffort:'low'}}]]);
  assert(DefaultModel.Config['~standard'].validate(broken[0].config).issues?.length);
  const rows = composeEntries([base,patch]);
  const native = rows.find(x=>x.id==='agent-default-model');
  assert.deepEqual(native.config,original);
  const override = rows.find(x=>x.id==='askai-call-settings');
  const runner = rows.find(x=>x.id==='headless-runner');
  assert(runner.inject.includes('askaiCallSettings'));
  assert(runner.inject.includes('askaiToolGuard'));
  const ctx = new Context();
  const nativeScope = ctx.plugin(DefaultModel,native.config);
  await new Promise(r=>setTimeout(r,15));
  let observed;
  const probe = ctx.plugin({inject:['agentDefaultModel','askaiCallSettings'],apply(c){
    observed=c.get('agentDefaultModel').currentSelection();
  }});
  await new Promise(r=>setTimeout(r,15));
  assert.equal(observed,undefined);
  const overrideScope = ctx.plugin(settings,override.config);
  await new Promise(r=>setTimeout(r,15));
  assert.deepEqual(observed,{...original,...expected});
  await probe.dispose();
  await overrideScope.dispose();
  assert.deepEqual(ctx.get('agentDefaultModel').currentSelection(),original);
  await nativeScope.dispose();
}
console.log('PASS: native patch semantics, schema, activation ordering and restoration');
'''
        plugin = ROOT / 'src/askai_cli/prompts/dsh-call-settings.mjs'
        for effort, model in (('low', None), ('high', None), ('off', 'selected-model'), ('max', 'another-model')):
            with self.subTest(effort=effort, model=model), tempfile.TemporaryDirectory() as tmp:
                patch = Path(tmp) / 'policy.yml'
                patch.write_text(dsh_patch(model=model, reasoning_effort=effort))
                expected = {'reasoningEffort': effort}
                if model:
                    expected['model'] = model
                result = subprocess.run([node, '--input-type=module', '-',
                    *(p.as_uri() for p in libraries), plugin.as_uri(), str(patch), json.dumps(expected)],
                    input=script, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('PASS:', result.stdout)


if __name__ == '__main__':
    unittest.main()
