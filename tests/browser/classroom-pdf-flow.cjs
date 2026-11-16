/* End-to-end PDF review and a two-student classroom in isolated browser contexts. */
const assert = require('assert');
const fs = require('fs');
const {chromium} = require('playwright-core');

(async () => {
  const base = process.env.CLASSROOM_TEST_URL;
  const data = JSON.parse(process.env.CLASSROOM_TEST_DATA || '{}');
  if (!base || !process.env.CHROMIUM_PATH) throw new Error('Run through the Django classroom browser test.');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const teacherCtx = await browser.newContext({viewport: {width: 1280, height: 900}, acceptDownloads: true});
  const mobileCtx = await browser.newContext({viewport: {width: 390, height: 850}, isMobile: true});
  const otherCtx = await browser.newContext({viewport: {width: 1280, height: 900}});
  const teacher = await teacherCtx.newPage(), a = await mobileCtx.newPage(), b = await otherCtx.newPage();
  const errors = [];
  for (const page of [teacher, a, b]) page.on('pageerror', error => errors.push(error.message));
  const login = async (page, username) => {
    await page.goto(base + data.login);
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form button[type=submit]').click()]);
  };
  const checkNoOverflow = async page => {
    const result = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth}));
    assert(result.scroll <= result.width + 2, `Horizontal overflow on mobile: ${JSON.stringify(result)}`);
  };
  const joinStart = async (page, code) => {
    await page.goto(base + data.join);
    await page.locator('input[name=code]').fill(code);
    await Promise.all([page.waitForURL('**/classroom/take/**'), page.getByRole('button', {name:'Join test'}).click()]);
    assert((await page.locator('body').textContent()).includes('23 min'));
    await Promise.all([page.waitForURL('**/exams/session/**'), page.getByRole('button', {name:/Start test/}).click()]);
    await page.locator('#fs-continue-windowed').click();
  };
  const submit = async page => {
    await page.locator('#btn-open-submit-modal').click();
    await Promise.all([page.waitForURL('**/completed/'), page.locator('#btn-confirm-submit').click()]);
    assert((await page.locator('body').textContent()).includes('Examination Submitted Successfully'));
  };
  try {
    await login(teacher, data.teacher);
    await teacher.goto(base + data.hub);
    await teacher.getByRole('link', {name:/Create Test from PDF/i}).first().click();
    await teacher.locator('#id_title').fill('Browser classroom PDF');
    await teacher.locator('#id_subject').fill('Science');
    await teacher.locator('#id_pdf_file').setInputFiles(data.pdf);
    await teacher.locator('#id_image_first').check();
    assert.strictEqual(await teacher.locator('#id_extraction_mode').inputValue(), 'local');
    await Promise.all([teacher.waitForURL('**/classroom/papers/**/review/', {timeout: 45000}),
      teacher.getByRole('button', {name:/Upload & start extraction/i}).click()]);
    assert(await teacher.getByText('Test preview').isVisible());
    assert.strictEqual(await teacher.getByText('Original question image', {exact:false}).count() >= 1, true);
    const firstQ = teacher.locator('[id^="q-"]').first();
    await firstQ.getByRole('link', {name:'Edit & key'}).click();
    await teacher.locator('#id_correct_option').selectOption('A');
    await teacher.locator('#id_reviewed').check();
    await Promise.all([teacher.waitForURL('**/classroom/papers/**/review/**'),
      teacher.getByRole('button', {name:'Save question'}).click()]);
    assert((await teacher.locator('body').textContent()).includes('Confirmed key · A'));
    await teacher.locator('#id_duration_minutes').fill('23');
    await teacher.locator('#id_answer_key_text').fill('2: A');
    for (const selector of ['#id_acknowledge_review', '#id_acknowledge_unassigned']) {
      if (await teacher.locator(selector).count()) await teacher.locator(selector).check();
    }
    await teacher.locator('#id_confirm_review').check();
    await Promise.all([teacher.waitForURL('**/classroom/sessions/**/'),
      teacher.getByRole('button', {name:/Create test & join code/}).click()]);
    const sessionUrl = teacher.url();
    const code = (await teacher.locator('#join-code').textContent()).trim();
    assert(/^[A-Z2-9]{6}$/.test(code));

    await login(a, data.studentA);
    await joinStart(a, code);
    await checkNoOverflow(a);
    const image = a.locator('#question-crop-box img').first();
    await image.waitFor({state:'visible'});
    await image.evaluate(node => node.decode());
    assert((await image.getAttribute('src')).includes('/classroom/attempts/'));
    const before = await a.locator('#exam-timer').textContent();

    // Simulate a failed save: the UI must say unsaved and allow an explicit retry.
    let failOnce = true;
    await a.route('**/save-answer/', route => {
      if (failOnce) { failOnce = false; return route.abort('failed'); }
      return route.continue();
    });
    await a.locator('.option-card').filter({hasText:'Four'}).click();
    await a.locator('#retry-unsaved').waitFor({state:'visible'});
    assert((await a.locator('#sync-indicator').textContent()).includes('Unsaved'));
    await checkNoOverflow(a);
    await a.locator('#retry-unsaved').click();
    await a.locator('#retry-unsaved').waitFor({state:'hidden'});
    assert((await a.locator('#sync-indicator').textContent()).includes('Saved'));
    await checkNoOverflow(a);
    await a.reload();
    await a.locator('#fs-continue-windowed').click();
    assert.strictEqual(await a.locator('.option-card.selected').count(), 1, 'Selection did not survive refresh');
    assert((await a.locator('.option-card.selected').textContent()).includes('Four'));
    const after = await a.locator('#exam-timer').textContent();
    assert.notStrictEqual(after, '--:--:--');
    const seconds = str => str.split(':').reduce((x, part) => x * 60 + Number(part), 0);
    assert(seconds(after) <= seconds(before), `Timer reset on refresh: ${before} -> ${after}`);
    await a.locator('#btn-save-next').click();
    await a.locator('.option-card').filter({hasText:'Blue'}).click();
    await submit(a);

    await login(b, data.studentB);
    await joinStart(b, code);
    await b.locator('.option-card').filter({hasText:'Five'}).click();
    await b.locator('#btn-save-next').click();
    await b.locator('.option-card').filter({hasText:'Blue'}).click();
    await submit(b);

    await teacher.goto(sessionUrl);
    assert.strictEqual((await teacher.locator('[data-count="joined"]').first().textContent()).trim(), '2');
    assert.strictEqual((await teacher.locator('[data-count="submitted"]').first().textContent()).trim(), '2');
    const scoreA = await teacher.locator('#participant-body tr').filter({hasText:data.studentA}).locator('td').nth(4).textContent();
    const scoreB = await teacher.locator('#participant-body tr').filter({hasText:data.studentB}).locator('td').nth(4).textContent();
    assert(scoreA.trim().startsWith('8') && scoreB.trim().startsWith('3'), `Wrong monitor scores: ${scoreA} ${scoreB}`);
    const [download] = await Promise.all([teacher.waitForEvent('download'), teacher.getByRole('link', {name:/Export CSV/}).click()]);
    const csv = fs.readFileSync(await download.path(), 'utf8');
    assert(csv.indexOf(data.studentA) < csv.indexOf(data.studentB), 'CSV did not put the topper first');
    assert(csv.includes('Score (confirmed keys)') && csv.includes('Result completeness'));
    await teacher.getByRole('link', {name:/Leaderboard/}).click();
    const ranks = await teacher.locator('tbody tr').allTextContents();
    assert(ranks[0].includes(data.studentA) && ranks[1].includes(data.studentB), `Wrong rank order: ${ranks}`);
    await teacher.goto(sessionUrl);
    let endConfirmation = false;
    teacher.once('dialog', dialog => { endConfirmation = true; return dialog.dismiss(); });
    await teacher.getByRole('button', {name:/End test now/}).click();
    assert(endConfirmation, 'Ending the test must request confirmation');
    assert((await teacher.locator('body').textContent()).includes('Joining open'));
    teacher.once('dialog', dialog => dialog.accept());
    await Promise.all([teacher.waitForURL('**/classroom/sessions/**/'),
      teacher.getByRole('button', {name:'Release results & answers'}).click()]);
    await a.reload();
    assert((await a.locator('body').textContent()).includes('Score on confirmed keys'));
    assert.deepStrictEqual(errors, [], `Page errors: ${errors.join(', ')}`);
    console.log('CLASSROOM_PDF_FLOW_OK');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
