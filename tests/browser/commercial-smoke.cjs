/* Real-browser smoke test: synthetic free series, pricing and answer release. */
const assert = require('assert');
const {chromium} = require('playwright-core');

(async () => {
  const base = process.env.COMMERCIAL_TEST_URL;
  const data = JSON.parse(process.env.COMMERCIAL_TEST_DATA);
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const mobile = {viewport: {width: 390, height: 844}, isMobile: true};
  const contexts = await Promise.all([browser.newContext(mobile), browser.newContext(mobile),
    browser.newContext(mobile), browser.newContext({viewport: {width: 1280, height: 900}})]);
  const [student, neet, teacher, admin] = await Promise.all(contexts.map(c => c.newPage()));
  const errors = [];
  for (const page of [student, neet, teacher, admin]) page.on('pageerror', error => errors.push(error.message));
  const noOverflow = async page => {
    const sizes = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth}));
    assert(sizes.scroll <= sizes.width + 2, `Horizontal overflow at ${page.url()}: ${JSON.stringify(sizes)}`);
  };
  const text = page => page.locator('body').innerText();
  const login = async (page, name) => {
    await page.goto(base + data.login);
    await page.locator('#id_username').fill(name);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form button[type=submit]').click()]);
  };
  const seconds = value => value.split(':').reduce((sum, part) => sum * 60 + Number(part), 0);
  try {
    for (const page of [student, admin]) {
      await page.goto(base + '/');
      const home = await text(page);
      assert(home.includes('₹299') && home.includes('₹99') && home.includes('20 more PDFs'));
      await noOverflow(page);
    }

    await login(student, data.jee);
    await student.goto(base + data.plans);
    const studentPlans = await text(student);
    assert(studentPlans.includes('20 additional PDF extractions'));
    assert(studentPlans.includes('20 additional classroom test starts'));
    assert(studentPlans.includes('JEE test series') && !studentPlans.includes('NEET test series'));
    assert(studentPlans.includes('sandbox payments are disabled'));
    assert(await student.getByRole('button', {name: 'Continue in test mode →'}).first().isDisabled());
    await noOverflow(student);
    await student.goto(base + data.series);
    assert.strictEqual(await student.locator('.series-card').count(), 16);
    assert((await text(student)).includes('Full series not on sale yet'));
    const first = student.locator('.series-card').first();
    assert((await first.innerText()).includes('Synthetic browser JEE test'));
    await noOverflow(student);
    await Promise.all([student.waitForURL('**/instructions/'), first.getByRole('link', {name: /View test/}).click()]);
    assert((await text(student)).includes('0/1 used'));
    await student.locator('#declaration-check').check();
    await Promise.all([student.waitForURL('**/session/**'), student.locator('#start-btn').click()]);
    const session = student.url();
    await student.locator('#fs-continue-windowed').click();
    const before = await student.locator('#exam-timer').innerText();
    const payload = await student.locator('#questions-data').textContent();
    assert(!payload.includes('Reviewed browser explanation') && !payload.includes('is_correct'));
    const image = student.locator('#question-crop-box img');
    await image.waitFor({state:'visible'});
    await image.evaluate(el => el.decode());
    assert((await image.getAttribute('src')).includes('/series/attempts/'));
    await student.locator('.option-card').filter({hasText: 'Four'}).click();
    await student.locator('#sync-indicator').getByText('Saved', {exact:false}).waitFor({timeout:10000});
    await student.reload();
    assert.strictEqual(student.url(), session);
    await student.locator('#fs-continue-windowed').click();
    assert.strictEqual(await student.locator('.option-card.selected').count(), 1);
    assert(seconds(await student.locator('#exam-timer').innerText()) <= seconds(before) + 1);
    await noOverflow(student);
    await student.locator('#btn-open-submit-modal').click();
    await Promise.all([student.waitForURL('**/completed/'), student.locator('#btn-confirm-submit').click()]);
    assert((await text(student)).includes('1 correct'));
    await student.getByRole('link', {name:'View result & review'}).click();
    assert((await text(student)).toLowerCase().includes('score on confirmed keys'));
    await student.getByRole('link', {name:'Review answers'}).click();
    const ownReview = await text(student);
    assert(ownReview.includes('Your answer review') && ownReview.includes('Your choice'));
    assert(!ownReview.includes('Reviewed browser explanation'));
    await noOverflow(student);
    const reviewUrl = student.url();
    await student.goto(base + data.instructions);
    assert((await text(student)).includes('Retakes are not available'));

    await login(neet, data.neet);
    await neet.goto(base + data.series);
    assert((await text(neet)).includes('NEET') && !(await text(neet)).includes('Synthetic browser JEE test'));
    assert.strictEqual((await neet.request.get(base + data.instructions)).status(), 404);
    await neet.goto(base + data.plans);
    assert((await text(neet)).includes('NEET test series') && !(await text(neet)).includes('JEE test series'));
    await noOverflow(neet);

    await login(teacher, data.teacher);
    await teacher.goto(base + data.classroom);
    assert((await text(teacher)).includes('24-HOUR ACTIVATIONS'));
    await teacher.goto(base + data.plans);
    const teacherPlans = await text(teacher);
    assert(teacherPlans.includes('20 additional 24-hour test activations') && teacherPlans.includes('₹299.00'));
    assert(!teacherPlans.includes('PDF practice pack'));
    await noOverflow(teacher);

    await login(admin, data.admin);
    await admin.goto(base + data.operator);
    const firstRow = admin.locator('tr').filter({hasText:'Synthetic browser JEE test'});
    await firstRow.getByRole('button', {name:'Release keys'}).click();
    assert((await text(admin)).includes('Answer keys and explanations are released'));
    await student.goto(reviewUrl);
    assert((await text(student)).includes('Reviewed browser explanation'));
    assert.deepStrictEqual(errors, [], `Browser page errors: ${errors.join('; ')}`);
    console.log('COMMERCIAL_BROWSER_OK');
  } finally {
    await Promise.all(contexts.map(c => c.close()));
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
