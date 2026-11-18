/* Local-only upload boundary in a real browser. The PDF and provider stub are synthetic. */
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
    const tag = 'local_only_' + Date.now();
    await page.goto(base + '/auth/register/');
    for (const [field, value] of Object.entries({first_name:'Local', last_name:'Only', username:tag,
      email:tag+'@example.test', password:'PracticeBrowserPass987!', confirm_password:'PracticeBrowserPass987!'})) {
      await page.locator('#id_' + field).fill(value);
    }
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form').filter({has:page.locator('#id_username')}).locator('button[type=submit]').click()]);
    await page.goto(base + '/questions/practice/upload/');
    assert.strictEqual(await page.locator('#id_extraction_mode').count(), 0);
    assert(await page.getByText('Your PDF is processed on the server and is not sent to an external provider.', {exact:true}).isVisible());
    assert(!(await page.getByText('Tesseract', {exact:false}).count()));

    // Forge a hidden field to prove the server rejects a cloud mode.
    await page.evaluate(() => {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'extraction_mode'; input.value = 'gemini';
      document.querySelector('form[data-upload-form]').appendChild(input);
    });
    await page.locator('#id_title').fill('Tampered browser paper');
    await page.locator('#id_pdf_file').setInputFiles(source);
    const [rejected] = await Promise.all([
      page.waitForResponse(response => response.url().includes('/questions/practice/upload/') && response.request().method() === 'POST'),
      page.getByRole('button', {name:/Upload & extract/i}).click(),
    ]);
    assert.strictEqual(rejected.status(), 200);
    assert(await page.getByText('This upload option is not available', {exact:false}).isVisible());

    // The normal path extracts the same PDF locally and reaches human review.
    await page.locator('#id_title').fill('Local-only browser paper');
    await page.locator('#id_pdf_file').setInputFiles(source);
    await Promise.all([page.waitForURL('**/settings/'), page.getByRole('button', {name:/Upload & extract/i}).click()]);
    assert(Number((await page.locator('#preview-questions').textContent()).trim()) > 0);
    assert(await page.getByText('Extracted questions', {exact:false}).first().isVisible());
    const size = await page.evaluate(() => ({scroll:document.documentElement.scrollWidth, width:document.documentElement.clientWidth}));
    assert(size.scroll <= size.width, `Mobile review overflow: ${size.scroll} > ${size.width}`);
    assert.strictEqual(errors.length, 0, errors.join('\n'));
    console.log('LOCAL_ONLY_BROWSER_OK');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
