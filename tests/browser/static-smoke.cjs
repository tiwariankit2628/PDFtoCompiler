/* Real Chromium against collected, fingerprinted assets served by WhiteNoise. */
const assert = require('assert');
const {chromium} = require('playwright-core');

(async () => {
  const base = process.env.STATIC_TEST_URL;
  if (!base || !process.env.CHROMIUM_PATH || !process.env.STATIC_TEST_DATA) {
    throw new Error('Run this browser smoke test via Django with CHROMIUM_PATH set.');
  }
  const data = JSON.parse(process.env.STATIC_TEST_DATA);
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await context.newPage();
  const errors = [];
  const failedStatic = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', msg => { if (msg.type() === 'error') errors.push(`console: ${msg.text()}`); });
  page.on('requestfailed', req => {
    if (req.url().includes('/static/') && !req.failure()?.errorText?.includes('ERR_ABORTED')) {
      failedStatic.push(`${req.url()}: ${req.failure()?.errorText}`);
    }
  });
  page.on('response', res => {
    if (res.url().includes('/static/') && res.status() >= 400) failedStatic.push(`${res.status()} ${res.url()}`);
  });
  async function visit(path, label, checkOverflow = true) {
    const response = await page.goto(base + path, {waitUntil: 'networkidle'});
    assert(response && response.status() === 200, `${label}: expected HTTP 200, got ${response?.status()} at ${page.url()}`);
    assert.strictEqual(typeof await page.evaluate(() => window.bootstrap), 'object', `${label}: Bootstrap JS did not load`);
    assert(await page.evaluate(() => [...document.styleSheets].some(s => s.href && s.href.includes('/bootstrap.min.'))),
      `${label}: fingerprinted Bootstrap CSS did not load`);
    if (checkOverflow) await noOverflow(label);
  }
  async function noOverflow(label) {
    const size = await page.evaluate(() => ({width: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth}));
    assert(size.width <= size.viewport + 1, `${label}: horizontal overflow ${size.width}px > ${size.viewport}px`);
  }
  async function login(role) {
    await page.goto(base + data.paths.logout);
    await page.goto(base + data.paths.login);
    await page.locator('#id_username').fill(data.roles[role].username);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([
      page.waitForURL('**/dashboard/**'),
      page.locator('form button[type=submit]').click(),
    ]);
  }
  try {
    for (const width of [320, 390, 768, 1024, 1440]) {
      await page.setViewportSize({width, height: 900});
      await visit(data.paths.home, `home ${width}`);
      assert(await page.evaluate(() => getComputedStyle(document.querySelector('.navbar')).display === 'flex'));
      assert(await page.evaluate(() => document.fonts.check('12px bootstrap-icons')), 'Bootstrap icon font failed');
      if (width < 992) {
        await page.locator('.navbar-toggler').click();
        await page.locator('#cbtNavbar.show').waitFor();
        await noOverflow(`expanded public menu ${width}`);
        if (width === 390) {
          await page.locator('#cbtNavbar .nav-link[href$="#features"]').click();
          await page.waitForURL('**/#features');
          await page.locator('#cbtNavbar:not(.show)').waitFor();
          const anchor = await page.evaluate(() => ({
            target: document.getElementById('features').getBoundingClientRect().top,
            nav: document.querySelector('.navbar').getBoundingClientRect().bottom,
          }));
          assert(anchor.target >= anchor.nav - 1, 'Mobile navigation covers the selected section');
        } else {
          await page.locator('.navbar-toggler').click();
          await page.locator('#cbtNavbar:not(.show)').waitFor();
        }
      }
    }
    for (const width of [390, 1440]) {
      await page.setViewportSize({width, height: 900});
      for (const name of ['about', 'contact', 'login', 'register', 'catalog']) {
        await visit(data.paths[name], `${name} ${width}`);
      }
    }
    console.log('PASS public pages, icons and responsive global navigation');

    for (const role of ['student', 'teacher', 'admin', 'super_admin']) {
      await page.setViewportSize({width: 1440, height: 900});
      await login(role);
      for (const width of [320, 390, 1440]) {
        await page.setViewportSize({width, height: 900});
        await visit(data.roles[role].dashboard, `${role} dashboard ${width}`);
        if (width < 992) {
          await page.locator('.navbar-toggler').click();
          await page.locator('#cbtNavbar.show').waitFor();
          await noOverflow(`${role} global menu ${width}`);
          await page.locator('.navbar-account-toggle').click();
          assert(await page.locator('.navbar-account .dropdown-menu a').filter({hasText: 'My Profile'}).isVisible(), `${role} account dropdown`);
          await noOverflow(`${role} account menu ${width}`);
          await page.locator('.navbar-toggler').click();
          await page.locator('#cbtNavbar:not(.show)').waitFor();
          await page.locator('.dashboard-menu-toggle').click();
          await page.locator('#dashboardSidebar.show').waitFor();
          assert(await page.locator('#dashboardSidebar a').filter({hasText: data.roles[role].nav}).isVisible(),
            `${role} sidebar navigation missing on mobile`);
          await noOverflow(`${role} dashboard menu ${width}`);
          if (width === 390) {
            await Promise.all([
              page.waitForURL(base + data.roles[role].target),
              page.locator('#dashboardSidebar a').filter({hasText: data.roles[role].nav}).click(),
            ]);
          }
        } else {
          assert(await page.locator('#dashboardSidebar a').filter({hasText: data.roles[role].nav}).isVisible());
        }
      }
      const paths = {
        student: ['profile', 'practice', 'bank', 'history', 'plans', 'catalog'],
        teacher: ['profile', 'papers', 'questions', 'exams', 'question_preview', 'analytics'],
        admin: ['profile', 'teachers', 'students', 'categories', 'exams', 'papers', 'questions'],
        super_admin: ['profile', 'admins', 'teachers', 'students', 'categories', 'audit'],
      }[role];
      for (const width of [390, 1440]) {
        await page.setViewportSize({width, height: 900});
        for (const name of paths) {
          await visit(data.paths[name], `${role} ${name} ${width}`);
          if (name === 'question_preview') {
            assert(await page.locator('.katex').count(), 'KaTeX did not render a formula');
            assert.strictEqual(typeof await page.evaluate(() => window.katex), 'object');
          }
          if (name === 'analytics') {
            assert.strictEqual(await page.evaluate(() => typeof window.Chart), 'function', 'Chart.js failed');
            assert(await page.evaluate(() => !!window.Chart.getChart('scoreDistChart')), 'Chart was not initialized');
          }
        }
      }
      if (role === 'student') {
        for (const width of [320, 1440, 390]) {
          await page.setViewportSize({width, height: 900});
          await visit(data.paths.session, `student exam session ${width}`);
          assert(await page.locator('.katex').count(), 'KaTeX did not render in the exam session');
          const payload = await page.locator('#questions-data').textContent();
          assert(!payload.includes('is_correct') && !payload.includes('explanation'), 'Answer key leaked to browser');
          if (width === 390) {
            assert(await page.evaluate(() => [...document.querySelectorAll('.option-card.has-math .option-text')].some(el =>
              parseFloat(getComputedStyle(el).lineHeight) >= 32)),
            'Long mobile equations need enough line height to avoid overlapping');
            await page.locator('.option-card').first().click();
            await Promise.all([
              page.waitForResponse(r => r.url().includes('/save-answer/') && r.status() === 200),
              page.locator('#btn-save-next').click(),
            ]);
            await page.reload({waitUntil: 'networkidle'});
            assert.strictEqual(await page.locator('.option-card.selected').count(), 1, 'Mobile answer was not saved');
            await page.locator('#btn-open-submit-modal').click();
            await page.locator('#submitModal.show').waitFor();
            assert(await page.locator('#btn-confirm-submit').isVisible(), 'Mobile submit control is hidden');
            await page.locator('#submitModal button').filter({hasText: 'Return to Exam'}).click();
            if (await page.evaluate(() => !!document.fullscreenElement)) {
              await page.evaluate(() => document.exitFullscreen());
            }
          }
        }
      }
      console.log(`PASS ${role} dashboard and role-specific pages`);
    }
    assert.deepStrictEqual(failedStatic, [], `Static requests failed: ${failedStatic.join(', ')}`);
    assert.deepStrictEqual(errors, [], `Browser errors: ${errors.join(', ')}`);
    console.log('BROWSER_STATIC_OK');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
