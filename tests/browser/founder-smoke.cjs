/* Real Chromium against a disposable local HTTPS proxy. No external requests. */
const assert = require('assert');
const crypto = require('crypto');
const { chromium } = require('playwright-core');
const data = JSON.parse(process.env.FOUNDER_BROWSER_DATA);

function totp(secret) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const char of secret) bits += alphabet.indexOf(char).toString(2).padStart(5, '0');
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const digest = crypto.createHmac('sha1', Buffer.from(bytes)).update(counter).digest();
  const offset = digest[19] & 15;
  const binary = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(binary % 1000000).padStart(6, '0');
}

(async () => {
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const errors = [];
  async function newPage() {
    const context = await browser.newContext({ignoreHTTPSErrors: true, viewport: {width:1440, height:900}});
    await context.route('**/*', async (route, request) => {
      if (new URL(request.url()).origin === data.base) return route.fallback();
      errors.push('Unexpected external request: ' + new URL(request.url()).origin);
      return route.abort();
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error' && !message.text().includes('403')) errors.push(message.text());
    });
    return page;
  }
  async function checkWidth(page, width) {
    await page.setViewportSize({width, height:900});
    const size = await page.evaluate(() => ({scroll:document.documentElement.scrollWidth,
      viewport:document.documentElement.clientWidth}));
    assert(size.scroll <= size.viewport + 1, `${page.url()} overflows at ${width}: ${JSON.stringify(size)}`);
  }
  async function login(page, username) {
    await page.goto(data.base + data.login);
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([
      page.waitForURL(url => url.pathname.startsWith('/founder/') || url.pathname.startsWith('/dashboard/')),
      page.locator(`form[action^="${data.login}"] button[type="submit"]`).click(),
    ]);
  }
  try {
    const page = await newPage();
    await login(page, data.owner);
    await page.waitForURL(url => url.pathname === data.verify);
    await checkWidth(page, 320);
    assert((await page.locator('body').innerText()).includes('Verify it’s you'));
    await page.locator('#founder-password').fill('WrongPassword');
    await page.locator('#founder-code').fill(totp(data.secret));
    await page.getByRole('button', {name:'Unlock workspace'}).click();
    assert((await page.locator('[role=alert]').innerText()).includes('not accepted'));
    assert(!(await page.locator('body').innerText()).includes('WrongPassword'));
    await page.locator('#founder-password').fill(data.password);
    await page.locator('#founder-code').fill(totp(data.secret));
    await Promise.all([page.waitForURL(url => url.pathname === data.overview),
      page.getByRole('button', {name:'Unlock workspace'}).click()]);
    assert((await page.locator('body').innerText()).includes('Welcome back, Anika'));
    assert((await page.locator('body').innerText()).includes('not live revenue'));
    assert((await page.locator('body').innerText()).includes('Earlier history is not reconstructed'));
    await page.getByText('View exact daily values').click();
    assert((await page.locator('.chart-details').innerText()).includes('Not tracked'));
    assert((await page.locator('.chart-details').innerText()).includes('partial day'));
    await page.getByText('View exact daily values').click();
    const hero = page.locator('.overview-hero');
    assert((await hero.evaluate(el => getComputedStyle(el).backgroundImage)).includes('founder-command-hero'));
    if (process.env.FOUNDER_SCREENSHOT_DIR) {
      await page.setViewportSize({width:1440,height:1000});
      await page.evaluate(() => { document.activeElement.blur(); window.scrollTo({top:0, behavior:'instant'}); });
      await page.waitForFunction(() => window.scrollY === 0);
      await page.screenshot({path:process.env.FOUNDER_SCREENSHOT_DIR + '/founder-desktop.png', fullPage:true});
      await page.setViewportSize({width:390,height:844});
      await page.evaluate(() => window.scrollTo({top:0, behavior:'instant'}));
      await page.waitForFunction(() => window.scrollY === 0);
      await page.screenshot({path:process.env.FOUNDER_SCREENSHOT_DIR + '/founder-mobile.png', fullPage:true});
    }
    for (const path of [data.overview, data.accounts, data.series, data.payments, data.activity]) {
      await page.goto(data.base + path);
      for (const width of [320, 375, 390, 768, 1024, 1440]) await checkWidth(page, width);
      assert.strictEqual(await page.locator('.founder-nav [aria-current="page"]').count(), 1);
    }
    await page.goto(data.base + data.accounts);
    await page.locator('#people-query').fill(data.student_email);
    await page.getByRole('button', {name:'Search'}).click();
    assert.strictEqual(new URL(page.url()).search, '');
    assert((await page.locator('body').innerText()).includes('Synthetic Student'));
    await page.getByRole('link', {name:'View account'}).click();
    assert(new URL(page.url()).pathname.endsWith(`/accounts/${data.student_pk}/`));
    assert((await page.locator('body').innerText()).includes('Paper processing'));
    for (const width of [320,390,768,1440]) await checkWidth(page, width);
    await page.getByRole('link', {name:'Payment & refund ledger'}).click();
    assert((await page.locator('body').innerText()).includes('Test-only gateway'));
    await page.getByRole('button', {name:'Lock'}).click();
    await page.waitForURL(url => url.pathname === data.verify);
    await page.goto(data.base + data.overview);
    await page.waitForURL(url => url.pathname === data.verify);
    // A different superuser is not automatically a founder.
    const other = await newPage();
    await login(other, data.other);
    await other.waitForURL(url => url.pathname.startsWith('/dashboard/'));
    const forbidden = await other.goto(data.base + data.overview);
    assert.strictEqual(forbidden.status(), 403);
    assert(!(await other.locator('body').innerText()).includes('THE BIG PICTURE'));
    assert.deepStrictEqual(errors, [], 'Browser errors: ' + errors.join('; '));
    console.log('FOUNDER_BROWSER_OK');
  } finally {
    // Closing the browser closes its contexts; closing both contexts in
    // parallel can terminate single-process Chromium before the second closes.
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
