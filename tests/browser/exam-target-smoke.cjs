/* Mobile registration, track-aware catalog, direct access and active timer. */
const {chromium} = require('playwright-core');
const assert = require('assert');

(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = process.env.TARGET_TEST_URL;
  const data = JSON.parse(process.env.TARGET_TEST_DATA);
  try {
    await page.goto(base + data.register);
    for (const [name, value] of Object.entries({first_name: 'Target', last_name: 'Student',
      username: 'browser_target_student', email: 'browser-target@example.test',
      password: 'TargetBrowserPass987!', confirm_password: 'TargetBrowserPass987!'})) {
      await page.locator('#id_' + name).fill(value);
    }
    await page.getByRole('button', {name: /Create account/}).click();
    assert((await page.locator('body').innerText()).includes('Students must choose JEE or NEET.'));
    // Password fields are deliberately cleared after a failed registration.
    await page.locator('#id_password').fill('TargetBrowserPass987!');
    await page.locator('#id_confirm_password').fill('TargetBrowserPass987!');
    await page.locator('#id_target_exam').selectOption('NEET');
    await Promise.all([page.waitForURL('**/dashboard/**'), page.getByRole('button', {name: /Create account/}).click()]);
    await page.goto(base + data.catalog);
    let text = await page.locator('body').innerText();
    assert(text.includes('Browser NEET paper'));
    assert(!text.includes('Browser JEE Main paper'));
    assert(!text.includes('Browser JEE Advanced paper'));
    assert.strictEqual((await page.request.get(base + data.main)).status(), 404);
    assert.strictEqual((await page.request.get(base + data.advanced)).status(), 404);

    await page.goto(base + data.profile);
    await page.locator('#id_target_exam').selectOption('JEE');
    await Promise.all([page.waitForURL('**/profile/'), page.getByRole('button', {name: /Save Changes/}).click()]);
    await page.goto(base + data.catalog);
    text = await page.locator('body').innerText();
    assert(text.includes('Browser JEE Main paper'));
    assert(text.includes('Browser JEE Advanced paper'));
    assert(!text.includes('Browser NEET paper'));
    assert.strictEqual((await page.request.get(base + data.neet)).status(), 404);

    await page.goto(base + data.main);
    await page.locator('#declaration-check').check();
    await Promise.all([page.waitForURL('**/session/**'), page.locator('#start-btn').click()]);
    const sessionUrl = page.url();
    const second = await context.newPage();
    await second.goto(base + data.profile);
    await second.locator('#id_target_exam').selectOption('NEET');
    await second.getByRole('button', {name: /Save Changes/}).click();
    assert((await second.locator('body').innerText()).includes('Finish your active test before changing your target exam'));
    await second.close();
    await page.reload();
    assert.strictEqual(page.url(), sessionUrl);
    assert.deepStrictEqual(errors, []);
    console.log('EXAM_TARGET_BROWSER_OK');
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
