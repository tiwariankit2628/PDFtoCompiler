/* Interaction and progressive-enhancement checks for the redesigned public UI. */
const { chromium } = require('playwright-core');
const assert = require('assert');

(async () => {
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const base = process.env.REDESIGN_TEST_URL;
  const errors = [];
  try {
    const context = await browser.newContext({viewport:{width:1440, height:900}});
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    for (const width of [320, 375, 390, 768, 1024, 1440]) {
      await page.setViewportSize({width,height:900});
      await page.goto(base + '/', {waitUntil:'networkidle'});
      const size = await page.evaluate(() => ({scroll:document.documentElement.scrollWidth, viewport:document.documentElement.clientWidth}));
      assert(size.scroll <= size.viewport + 1, `home ${width} overflows: ${size.scroll} > ${size.viewport}`);
      assert(await page.locator('[data-slide]').first().locator('img').evaluate(img => img.complete && img.naturalWidth > 0));
    }
    await page.locator('[data-demo-option]').nth(2).click();
    assert.strictEqual(await page.locator('[data-demo-option]').nth(2).getAttribute('aria-pressed'), 'true');
    assert((await page.locator('[data-demo-status]').innerText()).includes('Option C selected'));
    await page.locator('#role-student-tab').focus();
    await page.keyboard.press('ArrowRight');
    assert.strictEqual(await page.locator('#role-teacher-tab').getAttribute('aria-selected'), 'true');
    assert(await page.locator('#role-teacher').isVisible());
    assert(!(await page.locator('#role-student').isVisible()));
    assert((await page.locator('#role-teacher a').getAttribute('href')).includes('role=TEACHER'));
    await page.locator('[data-carousel-next]').click();
    assert(await page.locator('[data-slide]').nth(1).isVisible());
    await page.locator('[data-carousel-prev]').click();
    assert(await page.locator('[data-slide]').first().isVisible());
    await page.goto(base + '/auth/register/?role=TEACHER');
    assert(await page.locator('input[name=role][value=TEACHER]').isChecked());
    assert.strictEqual(await page.locator('#id_password').getAttribute('type'), 'password');
    await page.locator('[data-password-toggle="id_password"]').click();
    assert.strictEqual(await page.locator('#id_password').getAttribute('type'), 'text');
    assert.strictEqual(await page.locator('[data-password-toggle="id_password"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-password-toggle="id_password"]').click();
    assert.strictEqual(await page.locator('#id_password').getAttribute('type'), 'password');
    // Keep the first context open; some headless single-process Chromium builds
    // exit when their last context closes.

    // Exercise the automatic rotation without making the suite wait in real time.
    const autoplayContext = await browser.newContext();
    const auto = await autoplayContext.newPage();
    await auto.clock.install();
    await auto.goto(base + '/', {waitUntil:'domcontentloaded'});
    await auto.locator('[data-carousel-controls]').waitFor({state:'visible'});
    await auto.clock.fastForward(6600);
    assert(await auto.locator('[data-slide]').nth(1).isVisible(), 'carousel did not advance automatically');
    await auto.locator('[data-carousel-pause]').evaluate(button => button.click());
    await auto.clock.fastForward(15000);
    assert(await auto.locator('[data-slide]').nth(1).isVisible(), 'paused carousel continued moving');

    const reduced = await browser.newContext({reducedMotion:'reduce'});
    const motionPage = await reduced.newPage();
    await motionPage.clock.install();
    await motionPage.goto(base + '/', {waitUntil:'domcontentloaded'});
    assert.strictEqual(await motionPage.locator('[data-carousel-pause]').getAttribute('aria-label'), 'Play automatic slides');
    await motionPage.clock.fastForward(15000);
    assert(await motionPage.locator('[data-slide]').first().isVisible());

    // Without JavaScript all meaningful slide and role content remains available.
    const noJs = await browser.newContext({javaScriptEnabled:false});
    const fallback = await noJs.newPage();
    await fallback.goto(base + '/');
    assert.strictEqual(await fallback.locator('[data-slide]:visible').count(), 3);
    assert.strictEqual(await fallback.locator('[data-role-panel]:visible').count(), 2);
    assert(!(await fallback.locator('[data-carousel-controls]').isVisible()));
    assert.deepStrictEqual(errors, []);
    console.log('REDESIGN_BROWSER_OK');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
