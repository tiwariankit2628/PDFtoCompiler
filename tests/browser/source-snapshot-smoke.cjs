/* Local synthetic PDF; verify original-source crops render in the mobile CBT. */
const {chromium} = require('playwright-core');
const assert = require('assert');

(async () => {
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(process.env.SOURCE_TEST_URL + '/auth/login/');
    await page.locator('#id_username').fill('source_browser_student');
    await page.locator('#id_password').fill('SourceBrowserPass987!');
    await Promise.all([
      page.waitForURL('**/dashboard/**'),
      page.locator('form').filter({has: page.locator('#id_username')}).locator('button[type=submit]').click(),
    ]);
    await page.goto(process.env.SOURCE_TEST_URL + process.env.SOURCE_TEST_SESSION_PATH);
    const parallel = process.env.SOURCE_TEST_PARALLEL === '1';
    const caption = parallel ? 'Other original-language region' : 'Original answer choices';
    await page.getByText(caption, {exact: false}).waitFor({state: 'visible'});
    if (!parallel) await page.getByText('Original question region', {exact: false}).waitFor({state: 'visible'});
    const choice = page.locator(`img[alt^="${caption}"]`);
    await choice.waitFor({state: 'visible'});
    await page.waitForFunction((label) => {
      const image = [...document.querySelectorAll('img')].find(item => item.alt.startsWith(label));
      return image && image.complete && image.naturalWidth > 0;
    }, caption);
    assert(await page.getByText('1=A, 2=B', {exact: false}).isVisible());
    assert.strictEqual(await page.locator('.option-card').count(), 4);
    if (parallel) {
      await page.locator('.option-card').filter({hasText: 'English 1-2'}).click();
      const [saved] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('#btn-save-next').click(),
      ]);
      assert.strictEqual(saved.status(), 200);
    }
    await page.reload();
    if (parallel) await page.locator('#palette-btn-0').click();
    await page.getByText(caption, {exact: false}).waitFor({state: 'visible'});
    if (parallel) assert(await page.locator('.option-card.selected').filter({hasText: 'English 1-2'}).isVisible());
    await page.waitForLoadState('networkidle');
    assert.strictEqual(errors.length, 0, errors.join('\n'));
    console.log('SOURCE_SNAPSHOT_BROWSER_OK');
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
