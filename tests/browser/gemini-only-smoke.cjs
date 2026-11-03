/* Browser-only lower-cost extraction smoke test: synthetic PDF, mocked Google, no paid calls. */
const { chromium } = require('playwright-core');
const assert = require('assert');

(async () => {
  const base = process.env.GEMINI_ONLY_TEST_URL, source = process.env.GEMINI_ONLY_TEST_PDF;
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    const tag = 'lowcost_' + Date.now();
    await page.goto(base + '/auth/register/');
    for (const [field, value] of Object.entries({first_name:'Low', last_name:'Cost', username:tag,
      email:tag+'@example.test', password:'PracticeBrowserPass987!', confirm_password:'PracticeBrowserPass987!'})) {
      await page.locator('#id_' + field).fill(value);
    }
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form').filter({has:page.locator('#id_username')}).locator('button[type=submit]').click()]);
    await page.goto(base + '/questions/practice/upload/');
    assert(await page.locator('#id_extraction_mode option[value="gemini"]').count(), 'Gemini-only choice missing');
    await page.locator('#id_title').fill('Lower cost browser paper');
    await page.locator('#id_extraction_mode').selectOption('gemini');
    await page.locator('#id_pdf_file').setInputFiles(source);
    await Promise.all([
      page.waitForResponse(response => response.url().includes('/questions/practice/upload/') && response.request().method() === 'POST'),
      page.getByRole('button', {name:/Upload & extract/i}).click(),
    ]);
    assert(await page.getByText('Explicit consent is required before sending a PDF to external providers.').isVisible());
    assert(await page.locator('#id_cloud_consent').isChecked() === false);
    // The browser clears the file input on a validation response.
    await page.locator('#id_pdf_file').setInputFiles(source);
    await page.locator('#id_cloud_consent').check();
    await Promise.all([page.waitForURL('**/settings/'), page.getByRole('button', {name:/Upload & extract/i}).click()]);
    assert.strictEqual((await page.locator('#preview-questions').textContent()).trim(), '2');
    await page.getByText('Cloud extraction usage').click();
    assert(await page.getByText('Gemini reported 1200 input, 500 response and 50 thinking tokens', {exact:false}).isVisible());
    assert(await page.getByText('Mathpix is not contacted', {exact:false}).count() === 0, 'Only warnings and usage should be public');
    assert(await page.locator('img[alt="Question image"]').count() > 0, 'Original image crops missing');
    const size = await page.evaluate(() => ({scroll:document.documentElement.scrollWidth, width:document.documentElement.clientWidth}));
    assert(size.scroll <= size.width, `Mobile review overflow: ${size.scroll} > ${size.width}`);
    await page.locator('#id_duration_minutes').fill('30');
    await page.locator('#id_acknowledge_review').check();
    await page.locator('#id_acknowledge_unassigned').check();
    await Promise.all([page.waitForURL('**/instructions/'), page.getByRole('button', {name:'Create private test'}).click()]);
    assert.strictEqual(errors.length, 0, errors.join('\n'));
    console.log('GEMINI_ONLY_BROWSER_OK');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
