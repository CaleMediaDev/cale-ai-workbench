// Render a local project page with the app's installed Playwright and Chrome.
// Arguments are passed as process values; project text never becomes shell code.
const path = require('path');
const { chromium } = require(path.join(__dirname, 'Toolbox', 'node_modules', 'playwright-core'));

const [url, output] = process.argv.slice(2);
if (!url || !output) throw new Error('Expected a local URL and output PNG path.');
const target = new URL(url);
if (target.protocol !== 'file:' && !['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname)) {
  throw new Error('Preview URL must be local.');
}

(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 960, height: 720 }, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 12000 });
    const final = new URL(page.url());
    if (final.protocol !== target.protocol ||
        (final.protocol !== 'file:' && !['127.0.0.1', 'localhost', '[::1]'].includes(final.hostname))) {
      throw new Error('Preview redirected away from the local project.');
    }
    await page.screenshot({ path: output, fullPage: false, timeout: 12000 });
  } finally {
    await browser.close();
  }
})().catch(error => {
  process.stderr.write(String(error.message || error) + '\n');
  process.exitCode = 1;
});
