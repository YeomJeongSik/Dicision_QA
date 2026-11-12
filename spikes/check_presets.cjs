const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const { isAbsolute, join, relative } = require('node:path');
const { createInterface } = require('node:readline');
const { chromium } = require(process.env.QA_PLAYWRIGHT_PATH || 'playwright');

async function main() {
  const [program, browserPath, script] = process.argv.slice(2);
  const args = [...(script ? ['-B', script] : []), 'panel', '--port', '0'];
  const panel = spawn(program, args, { windowsHide: true });
  let browser;
  let fixture;
  try {
    const lines = createInterface({ input: panel.stdout });
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Panel did not start')), 8000);
      panel.once('error', reject);
      lines.once('line', line => {
        clearTimeout(timer);
        const match = /^MOCK panel: (http:\/\/127\.0\.0\.1:\d+)$/.exec(line);
        if (match) resolve(match[1]);
        else reject(new Error('Unexpected panel startup output'));
      });
    });
    browser = await chromium.launch({ executablePath: browserPath, headless: true });
    const page = await browser.newPage({ acceptDownloads: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    const step = page.locator('.step').first();
    await step.locator('.reference').selectOption('JUMP');
    await step.locator('[data-range="duration_ms"] [data-field="value"]').fill('120');
    await step.locator('[data-range="repeat"] [data-field="value"]').fill('2');
    await page.locator('#expect').selectOption('IA_Jump');
    const downloadPromise = page.waitForEvent('download');
    await page.locator('#save-preset').click();
    const download = await downloadPromise;
    const path = await download.path();
    const preset = JSON.parse(await fs.readFile(path, 'utf8'));
    assert.equal(preset.steps[0].duration_ms.value, 120);
    assert.equal(preset.steps[0].repeat.value, 2);
    await step.locator('.reference').selectOption('MOVE_FORWARD');
    await page.locator('#preset-file').setInputFiles(path);
    await page.waitForFunction(() => document.querySelector('.reference').value === 'JUMP');
    const executed = page.waitForResponse(r => r.url() === url + '/run');
    await page.locator('#run').click();
    const report = await (await executed).json();
    assert.equal(report.verdict, 'PASS');
    assert.equal(report.after.jumps, 2);
    assert.equal(report.input_plan.duration_ms, 290);
    fixture = await fs.mkdtemp(join(os.tmpdir(), 'qa-project-ui-'));
    const project = join(fixture, 'Sample.uproject');
    const plugin = join(fixture, 'fixture-plugin');
    await fs.writeFile(project, '{"EngineAssociation":"5.9"}');
    await fs.mkdir(plugin);
    await fs.writeFile(join(plugin, 'GameQABridge.uplugin'), '{"Version":1}');
    await page.locator('details summary').click();
    await page.locator('#project-path').fill(project);
    await page.locator('#plugin-source').fill(plugin);
    async function projectRequest(button, route) {
      const response = page.waitForResponse(r => r.url() === url + '/project/' + route);
      await page.locator(button).click();
      const result = await response;
      assert.equal(result.status(), 200, JSON.stringify(await result.json()));
      return result.json();
    }
    const doctor = await projectRequest('#project-doctor', 'doctor');
    assert.equal(doctor.project_type, 'Blueprint');
    assert.equal(doctor.engine_association, '5.9');
    assert.ok(Array.isArray(doctor.diagnostics.missing));
    const preview = await projectRequest('#plugin-preview', 'preview');
    assert.deepEqual(preview.approved_operations, ['install_new_plugin']);
    const installed = await projectRequest('#plugin-apply', 'apply');
    assert.equal(await fs.readFile(join(installed.target, 'GameQABridge.uplugin'), 'utf8'), '{"Version":1}');
    await page.locator('#plugin-mode').selectOption('remove');
    const remove = await projectRequest('#plugin-preview', 'preview');
    assert.deepEqual(remove.approved_operations, ['remove_plugin']);
    await projectRequest('#plugin-apply', 'apply');
    await assert.rejects(fs.access(installed.target), { code: 'ENOENT' });
    assert.equal(await fs.readFile(project, 'utf8'), '{"EngineAssociation":"5.9"}');
    assert.deepEqual(errors, []);
    console.log('PASS: Chrome preset round trip and project diagnosis, preview, install and remove');
  } finally {
    if (browser) await browser.close();
    panel.kill();
    if (fixture) {
      const resolved = await fs.realpath(fixture);
      const within = relative(await fs.realpath(os.tmpdir()), resolved);
      assert.match(within, /^qa-project-ui-[^\\/]+$/);
      assert.equal(isAbsolute(within), false);
      await fs.rm(resolved, { recursive: true, force: true });
    }
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
