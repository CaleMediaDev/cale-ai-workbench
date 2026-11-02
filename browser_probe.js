// Validate the app-owned JavaScript tools and exercise a disposable browser.
// The test page is a data URL; no network or user's Chrome profile is used.
const fs = require('fs');
const path = require('path');

async function main() {
  const root = path.resolve(process.argv[2]);
  const toolbox = path.resolve(process.argv[3]);
  const expected = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).dependencies;
  for (const [name, version] of Object.entries(expected)) {
    const file = path.join(toolbox, 'node_modules', name, 'package.json');
    const installed = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (installed.version !== version) throw new Error(`${name} version mismatch`);
  }
  const {chromium} = require(path.join(toolbox, 'node_modules', 'playwright'));
  const browser = await chromium.launch({headless: true, timeout: 15000});
  try {
    const page = await browser.newPage();
    await page.goto('data:text/html,<button id="check" onclick="this.textContent=\'clicked\'">ready</button>');
    await page.locator('#check').click();
    if (await page.locator('#check').textContent() !== 'clicked') {
      throw new Error('Local browser could not interact with a page');
    }
    const screenshot = await page.screenshot();
    if (!screenshot.length) throw new Error('Local browser could not capture a page');
  } finally {
    await browser.close();
  }
  console.log('Local browser and JavaScript tools passed navigation, interaction and capture checks.');
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
