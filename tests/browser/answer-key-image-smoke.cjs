/* End-to-end key review on a local raster image. Only the deliberately delayed
   first response is mocked to exercise stale-response handling; the succeeding
   image read goes through the real local OCR endpoint. */
const {chromium} = require('playwright-core');
const assert = require('assert');

(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(process.env.KEY_TEST_URL + '/auth/login/');
    await page.locator('#id_username').fill('key_browser_user');
    await page.locator('#id_password').fill('KeyBrowserPass987!');
    await Promise.all([
      page.waitForURL('**/dashboard/**'),
      page.locator('form').filter({has: page.locator('#id_username')}).locator('button[type=submit]').click(),
    ]);
    await page.goto(process.env.KEY_TEST_URL + process.env.KEY_TEST_PATH);
    const mode = process.env.KEY_TEST_MODE;
    if (mode === 'teacher') await page.getByRole('button', {name: /Import Answer Key/i}).click();
    const widget = page.locator('[data-answer-key-image]');
    const key = page.locator('#id_answer_key_text');
    const manual = page.locator('[data-manual-key-preview]');
    const files = widget.locator('[data-key-files]');
    const read = widget.locator('[data-key-read]');
    const review = widget.locator('[data-key-review]');
    const status = widget.locator('[data-key-status]');
    assert(await widget.isVisible());
    assert(await manual.isVisible());
    assert(await widget.getByText('Image reading can make mistakes even when an answer looks clear.', {exact: false}).count());
    const overflow = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
      elements: [...document.querySelectorAll('body *')].map(el => ({tag: el.tagName, cls: String(el.className), text: el.textContent.trim().slice(0, 45), right: Math.round(el.getBoundingClientRect().right)}))
        .filter(item => item.right > document.documentElement.clientWidth + 1).slice(0, 15)}));
    assert(overflow.scroll <= overflow.width + 1, JSON.stringify(overflow));

    // Typed keys show useful non-authoritative feedback, including malformed,
    // duplicate, unmatched and unsupported choices. Nothing is saved yet.
    await key.fill('1:A, 1:B, 99:C, 2:E, 7:A/C');
    assert((await manual.textContent()).includes('Q1 (repeated)'));
    assert((await manual.textContent()).includes('Q99 (not in this paper)'));
    assert((await manual.textContent()).includes('Q2 (choice unavailable or repeated)'));
    assert((await manual.textContent()).includes('Unrecognized text'));
    await manual.locator('[data-manual-key-clear]').click();
    assert.strictEqual(await key.inputValue(), '');
    assert((await manual.textContent()).includes('3 missing'));

    // Filters/jump affect the actual visible question cards on a phone.
    await page.locator('[data-question-filter="missing-key"]').click();
    await page.locator('[data-question-search]').fill('7');
    assert.strictEqual(await page.locator('[data-review-question]:visible').count(), 1);
    assert.strictEqual(await page.locator('[data-review-question]:visible').getAttribute('data-number'), '7');
    await page.locator('[data-question-search]').fill('');
    await page.locator('[data-question-filter="all"]').click();
    assert.strictEqual(await page.locator('[data-review-question]:visible').count(), 3);

    // Delay a fake old response, then change the file before it can arrive.
    // It must never be shown alongside the newer image or copied into the key.
    const endpoint = new URL(await widget.getAttribute('data-preview-url'), process.env.KEY_TEST_URL).href;
    let unblock, seen, done;
    const held = new Promise(resolve => { unblock = resolve; });
    const requested = new Promise(resolve => { seen = resolve; });
    const finished = new Promise(resolve => { done = resolve; });
    await page.route(endpoint, async route => {
      seen(); await held;
      try { await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
        detected_count: 1, rows: [{number: 1, options: ['A','B'], answer: 'B', status: 'detected'}],
      })}); } catch (_) { /* aborted requests are expected */ } finally { done(); }
    });
    await files.setInputFiles(process.env.KEY_TEST_IMAGE);
    await read.click();
    await requested;
    await files.setInputFiles(process.env.KEY_TEST_SECOND_IMAGE);
    unblock(); await finished;
    await page.unroute(endpoint);
    assert(!(await review.isVisible()));
    assert.strictEqual(await key.inputValue(), '');
    assert(!(await status.textContent()).includes('suggested'));

    // Real local OCR (no mocked OCR or remote service) reads the second raster.
    const [response] = await Promise.all([
      page.waitForResponse(res => res.url() === endpoint && res.request().method() === 'POST'),
      read.click(),
    ]);
    assert.strictEqual(response.status(), 200);
    await review.waitFor({state: 'visible'});
    assert((await status.textContent()).includes('2 suggested answer(s)'));
    assert((await status.textContent()).includes('4 number(s) did not match'));
    assert.strictEqual(await widget.locator('[data-key-image-previews] img').count(), 1);
    await widget.locator('[data-key-zoom]').evaluate(el => { el.value = '200'; el.dispatchEvent(new Event('input', {bubbles:true})); });
    assert.strictEqual(await widget.locator('[data-key-zoom-value]').textContent(), '200%');
    assert.strictEqual(await widget.locator('[data-key-image-previews] img').evaluate(el => el.style.width), '200%');
    const row = number => widget.locator(`[data-key-rows] tr[data-number="${number}"]`);
    assert.strictEqual(await row(1).locator('select').inputValue(), 'A');
    assert.strictEqual(await row(2).locator('select').inputValue(), 'B');
    assert.strictEqual(await row(7).locator('select').inputValue(), '');
    assert((await row(7).textContent()).includes('Not detected'));
    await widget.locator('[data-key-filter]').selectOption('attention');
    assert.strictEqual(await widget.locator('[data-key-rows] tr:visible').count(), 1);
    await widget.locator('[data-key-filter]').selectOption('all');
    await row(1).locator('select').selectOption('D');
    await row(2).locator('select').selectOption('');
    await row(7).locator('select').selectOption('C');
    const apply = widget.locator('[data-key-apply]');
    assert(await apply.isDisabled());
    await widget.locator('[data-key-confirm]').check();
    assert(await apply.isEnabled());

    // Never overwrite a typed key without approval. Then explicitly accept it.
    await key.fill('1: A');
    page.once('dialog', dialog => dialog.dismiss());
    await apply.click();
    assert.strictEqual(await key.inputValue(), '1: A');
    page.once('dialog', dialog => dialog.accept());
    await apply.click();
    assert.strictEqual(await key.inputValue(), '1: D, 7: C');
    assert((await manual.textContent()).includes('2 entered'));
    assert((await manual.textContent()).includes('1 missing'));

    // Changes after confirmation invalidate the form-level confirmation too.
    if (mode === 'student') await page.locator('#id_duration_minutes').fill('20');
    const confirmSelector = mode === 'teacher' ? '#id_confirm_checked' : mode === 'classroom' ? '#id_confirm_review' : '#id_confirm_answer_key';
    await page.locator(confirmSelector).check();
    await key.fill('1:D, 7:C');
    assert(!(await page.locator(confirmSelector).isChecked()));
    await page.locator(confirmSelector).check();
    const submit = mode === 'teacher' ? page.getByRole('button', {name: 'Apply checked keys to staging'})
      : mode === 'classroom' ? page.getByRole('button', {name: 'Create test & join code'})
      : page.getByRole('button', {name: 'Create private test'});
    await Promise.all([page.waitForNavigation({waitUntil: 'domcontentloaded'}), submit.click()]);
    assert.strictEqual(errors.length, 0, errors.join('\n'));
    console.log('ANSWER_KEY_BROWSER_OK');
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
