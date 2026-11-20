/* HTTPS real-browser UX/CSRF smoke. Google responses are fully synthetic. */
const assert = require('assert');
const {chromium} = require('playwright-core');

(async () => {
  const data = JSON.parse(process.env.GOOGLE_BROWSER_DATA);
  const base = data.base;
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const contexts = [];
  const errors = [];
  const makePage = async (code, mobile = true) => {
    const context = await browser.newContext({ignoreHTTPSErrors: true,
      extraHTTPHeaders: {'X-Synthetic-Identity': code},
      viewport: mobile ? {width: 390, height: 844} : {width: 1280, height: 900}, isMobile: mobile});
    contexts.push(context);
    // The local TLS proxy replaces the Google redirect with a synthetic
    // provider on localhost and asserts state, nonce, PKCE and callback.
    await context.route('**/*', async (route, request) => {
      const url = new URL(request.url());
      if (url.origin === base || (url.hostname === 'localhost' && url.port === new URL(base).port)) await route.fallback();
      else { errors.push(`Unexpected external request: ${url.origin}`); await route.abort(); }
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      const url = new URL(response.url());
      if (url.pathname.startsWith('/auth/google/') || url.hostname === 'accounts.google.com')
        console.log(`AUTH_RESPONSE ${response.status()} ${url.hostname}${url.pathname}`);
    });
    page.on('requestfailed', request => {
      const url = new URL(request.url());
      if (url.pathname.startsWith('/auth/google/') || url.hostname === 'accounts.google.com')
        console.log(`AUTH_REQUEST_FAILED ${url.hostname}${url.pathname} ${request.failure()?.errorText}`);
    });
    return page;
  };
  const noOverflow = async page => {
    const size = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth}));
    assert(size.scroll <= size.width + 2, `Horizontal overflow at ${page.url()}: ${JSON.stringify(size)}`);
  };
  const clickGoogle = async (page, destination) => {
    const callback = page.waitForResponse(response => response.url().includes(data.callback) && response.status() === 302, {timeout: 10000});
    await page.getByRole('button', {name: 'Continue with Google'}).click();
    try { await callback; }
    catch (error) { console.error(`Google flow stopped at ${new URL(page.url()).pathname}: ${(await page.locator('body').innerText()).slice(0, 1200)}`); throw error; }
    await page.waitForURL(url => url.pathname === destination || (destination === '/dashboard/' && url.pathname.startsWith('/dashboard/')));
  };
  const login = async (page, username, destination = '/dashboard/') => {
    await page.goto(base + data.login);
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([
      page.waitForURL(url => url.pathname === destination || (destination === '/dashboard/' && url.pathname.startsWith('/dashboard/'))),
      page.locator(`form[action^="${data.login}"] button[type="submit"]`).click(),
    ]);
  };
  const postAndWait = async (page, path, buttonName) => {
    const responsePromise = page.waitForResponse(response => response.url().includes(path) && response.request().method() === 'POST');
    await page.getByRole('button', {name: buttonName}).click();
    const response = await responsePromise;
    if (response.status() >= 400) throw new Error(`POST ${path} failed: ${response.status()} ${(await response.text()).slice(0, 1000)}`);
    return response;
  };
  try {
    // Existing password account: email matching does not silently sign in or merge.
    const existing = await makePage('existing', false);
    await existing.goto(base + data.login);
    await noOverflow(existing);
    assert(await existing.locator('#id_password').isVisible());
    const logo = existing.locator('.google-signin-button img');
    assert(await logo.evaluate(img => img.complete && img.naturalWidth > 0), 'Google button logo did not load');
    await clickGoogle(existing, data.login);
    assert((await existing.locator('body').innerText()).includes('An account may already exist'));
    await login(existing, data.existing);
    await existing.goto(base + data.profile);
    assert((await existing.locator('body').innerText()).includes('Connect the Google account'));
    await clickGoogle(existing, '/dashboard/');
    await existing.goto(base + data.logout);
    await existing.goto(base + data.login);
    await clickGoogle(existing, '/dashboard/');
    await existing.goto(base + data.logout);
    await login(existing, data.existing);

    // A pending password user can connect their matching Gmail without email.
    const pending = await makePage('pending');
    await login(pending, data.pending, '/auth/verify-email/pending/');
    assert((await pending.locator('body').innerText()).includes('Have a Google account for this address?'));
    await noOverflow(pending);
    await clickGoogle(pending, '/dashboard/');
    await pending.goto(base + data.profile);
    assert((await pending.locator('body').innerText()).includes('Google sign-in is connected'));

    // New mobile student chooses NEET, then adds a password on HTTPS.
    const student = await makePage('new-student');
    await student.goto(base + data.register);
    await noOverflow(student);
    await clickGoogle(student, data.complete);
    assert((await student.locator('body').innerText()).includes('newstudent@gmail.com'));
    await noOverflow(student);
    await student.locator('#id_target_exam').selectOption('NEET');
    await postAndWait(student, data.complete, 'Create my account');
    await student.waitForURL('**/dashboard/**');
    await student.goto(base + data.profile);
    await noOverflow(student);
    await student.getByRole('link', {name: 'Add a password'}).click();
    await student.locator('#id_new_password1').fill('NewBrowserPassword987!');
    await student.locator('#id_new_password2').fill('NewBrowserPassword987!');
    await postAndWait(student, data.add_password, 'Add password');
    await student.waitForURL('**/auth/profile/');
    await student.goto(base + data.logout);
    await student.goto(base + data.login);
    await student.locator('#id_username').fill('newstudent@gmail.com');
    await student.locator('#id_password').fill('NewBrowserPassword987!');
    await Promise.all([student.waitForURL('**/dashboard/**'),
      student.locator(`form[action^="${data.login}"] button[type="submit"]`).click()]);

    // New teacher is never silently made a student or assigned an exam track.
    const teacher = await makePage('new-teacher');
    await teacher.goto(base + data.register);
    await clickGoogle(teacher, data.complete);
    await teacher.locator('#id_role').selectOption('TEACHER');
    await postAndWait(teacher, data.complete, 'Create my account');
    await teacher.waitForURL('**/dashboard/teacher/');
    await noOverflow(teacher);
    assert.deepStrictEqual(errors, [], `Browser errors: ${errors.join('; ')}`);
    console.log('GOOGLE_BROWSER_OK');
  } finally {
    await Promise.all(contexts.map(context => context.close()));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
