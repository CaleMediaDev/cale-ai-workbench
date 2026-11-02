// Validate the app-owned JavaScript tools and launch a disposable local browser.
// This never connects to the user's Chrome profile or opens a web page.
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
  await browser.close();
  console.log('Local browser and JavaScript tools passed their checks.');
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
