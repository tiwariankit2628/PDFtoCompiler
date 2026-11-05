/* Real Chromium; synthetic PDF and synthetic loopback model responses only. */
const {chromium} = require('playwright-core');
const assert = require('assert');

(async () => {
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const context = await browser.newContext({viewport:{width:390,height:844}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = process.env.LOCAL_IMPROVE_URL;
  try {
    await page.goto(base + '/auth/login/');
    await page.locator('#id_username').fill('local_browser_student');
    await page.locator('#id_password').fill('BrowserPracticePass987!');
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form').filter({has:page.locator('#id_username')}).locator('button[type=submit]').click()]);
    await page.goto(base + '/questions/practice/upload/');
    await page.locator('#id_title').fill('Local browser PDF');
    await page.locator('#id_pdf_file').setInputFiles(process.env.LOCAL_IMPROVE_PDF);
    await page.locator('#id_extraction_mode').selectOption('gemini');
    await Promise.all([
      page.waitForResponse(response => response.url().includes('/practice/upload/') && response.request().method() === 'POST'),
      page.getByRole('button', {name:/Upload & extract/i}).click(),
    ]);
    const cloudError = page.locator('[data-mode-error]');
    assert(await cloudError.isVisible(), 'unconfigured cloud error missing');
    assert(await page.locator('[data-consent-error]').isVisible(), 'cloud consent error missing');
    await page.locator('#id_extraction_mode').selectOption('local');
    assert(!(await cloudError.isVisible()), 'stale cloud configuration error stayed visible for local mode');
    assert(!(await page.locator('#cloud-consent-field').isVisible()), 'cloud consent remained visible in local mode');
    assert(await page.locator('#method-changed-note').isVisible(), 'method change guidance missing');
    await page.locator('#id_extraction_mode').selectOption('gemini');
    assert(await cloudError.isVisible(), 'switching back to the rejected cloud mode should restore its error');
    await page.locator('#id_extraction_mode').selectOption('local');
    await page.locator('#id_pdf_file').setInputFiles(process.env.LOCAL_IMPROVE_PDF);
    await Promise.all([page.waitForURL('**/settings/'),page.getByRole('button',{name:/Upload & extract/i}).click()]);
    assert(await page.getByText('Extracted questions', {exact:false}).first().isVisible());

    const resultResponse = await page.goto(base + process.env.LOCAL_IMPROVE_RESULT_PATH);
    assert(await page.getByText('Optional local AI comparison').isVisible(),
      `result panel missing: ${resultResponse.status()} ${page.url()} ${(await page.locator('body').innerText()).slice(0, 500)}`);
    assert(await page.getByText('not a verified answer or official score', {exact:false}).isVisible());
    assert(!(await page.locator('#estimate-pause').isVisible()));
    await page.locator('#estimate-understand').check();
    await page.getByRole('button', {name:'Start local comparison'}).click();
    await page.waitForLoadState('domcontentloaded');
    await page.getByText('1 estimated correct', {exact:false}).waitFor({state:'visible',timeout:30000});
    assert(await page.getByText('suggests B', {exact:false}).first().isVisible());
    assert(await page.getByText('No answer key — ungraded', {exact:false}).first().isVisible());
    await page.waitForLoadState('networkidle');
    assert.strictEqual(errors.length, 0, errors.join('\n'));
    console.log('LOCAL_IMPROVEMENTS_BROWSER_OK');
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
