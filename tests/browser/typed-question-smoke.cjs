/* Real mobile/desktop workflow for checked typed questions and safe resumption. */
const assert = require('assert');
const {chromium} = require('playwright-core');

(async () => {
  const base = process.env.TYPED_TEST_URL;
  const data = JSON.parse(process.env.TYPED_TEST_DATA || '{}');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const mobile = await browser.newContext({viewport: {width: 390, height: 844}, isMobile: true});
  const desktop = await browser.newContext({viewport: {width: 1280, height: 900}});
  const student = await mobile.newPage(), teacher = await desktop.newPage();
  const errors = [];
  for (const page of [student, teacher]) page.on('pageerror', err => errors.push(err.message));
  const login = async (page, username) => {
    await page.goto(base + data.login);
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form button[type=submit]').click()]);
  };
  const noOverflow = async page => {
    const result = await page.evaluate(() => ({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth}));
    assert(result.scroll <= result.width + 2, `Horizontal overflow: ${JSON.stringify(result)}`);
  };
  const saveMultiEditor = async (page, path, expectedUrl) => {
    await page.goto(base + path);
    assert.strictEqual(await page.locator('#id_detected_type').inputValue(), 'mcq_multiple');
    assert(await page.locator('#id_typed_key').isVisible());
    assert(!(await page.locator('#id_correct_option').isVisible()));
    await page.locator('#id_typed_key').fill('[1,3]');
    await page.locator('#id_multi_scoring_mode').selectOption('per_correct');
    await page.locator('#id_partial_credit_per_choice').fill('1');
    await page.locator('#id_confirm_answer_key').check();
    await Promise.all([page.waitForURL(expectedUrl), page.getByRole('button', {name:'Save question'}).click()]);
  };
  const seconds = text => text.split(':').reduce((sum, part) => sum * 60 + Number(part), 0);
  const saved = async page => {
    await page.locator('#sync-indicator').getByText('Saved', {exact:false}).waitFor({timeout:10000});
    await page.locator('#retry-unsaved').waitFor({state:'hidden'});
  };
  const choose = (page, key) => page.locator('.option-card').filter({hasText:`Choice ${key}`});
  const enterSession = async page => {
    await page.locator('#fs-continue-windowed').click();
    const payload = await page.locator('#questions-data').textContent();
    assert(!payload.includes('numeric_key') && !payload.includes('is_correct'), 'Correct answers leaked to candidate');
    assert((await page.locator('#question-answer-instruction').textContent()).includes('Select all correct choices'));
    await noOverflow(page);
  };
  const take = async (page, numericAnswer, exerciseRetry) => {
    await enterSession(page);
    const before = await page.locator('#exam-timer').textContent();
    if (exerciseRetry) {
      let failOnce = true;
      await page.route('**/save-answer/', route => {
        if (failOnce) { failOnce = false; return route.abort('failed'); }
        return route.continue();
      });
      await choose(page, 'A').click();
      await page.locator('#retry-unsaved').waitFor({state:'visible'});
      await page.locator('#retry-unsaved').click();
      await saved(page);
      await page.unroute('**/save-answer/');
      await choose(page, 'C').focus();
      await page.keyboard.press('Space');
      await saved(page);
      assert.strictEqual(await page.locator('.option-card.selected').count(), 2);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      assert.strictEqual(await page.locator('.option-card.selected').count(), 2, 'Multiple selections did not survive refresh');
      const after = await page.locator('#exam-timer').textContent();
      assert(seconds(after) <= seconds(before) + 1, `Timer reset on refresh: ${before} -> ${after}`);
      await choose(page, 'C').focus(); await page.keyboard.press('Space');
      await saved(page);
    } else {
      await choose(page, 'A').click();
      await saved(page);
    }
    assert.strictEqual(await page.locator('.option-card.selected').count(), 1);
    await page.locator('#btn-save-next').click();
    const input = page.locator('#numerical-answer');
    await input.waitFor({state:'visible'});
    await input.fill(numericAnswer);
    await saved(page);
    if (exerciseRetry) {
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      assert.strictEqual(Number(await page.locator('#numerical-answer').inputValue()), Number(numericAnswer));
      await page.locator('#numerical-answer').fill('1e2');
      let alerted = false;
      page.once('dialog', async dialog => { alerted = dialog.message().includes('numerical answer'); await dialog.accept(); });
      await page.locator('#btn-open-submit-modal').click();
      await page.locator('#btn-confirm-submit').click();
      assert(alerted, 'Invalid numerical answer should prevent submission');
      await page.locator('#numerical-answer').fill(numericAnswer);
      await saved(page);
    }
    await noOverflow(page);
    await page.locator('#btn-save-next').click();
    await choose(page, 'B').click();
    await saved(page);
    await page.locator('#btn-open-submit-modal').click();
    await Promise.all([page.waitForURL('**/completed/'), page.locator('#btn-confirm-submit').click()]);
    assert((await page.locator('body').textContent()).includes('Examination Submitted Successfully'));
  };
  try {
    await login(student, data.student);
    await student.goto(base + data.legacySession);
    await student.locator('#fs-continue-windowed').click();
    assert((await student.locator('#question-answer-instruction').textContent()).includes('older single-choice response'));
    await student.locator('#btn-save-next').click(); await saved(student);
    await student.reload(); await student.locator('#fs-continue-windowed').click();
    assert((await student.locator('#question-answer-instruction').textContent()).includes('older single-choice response'),
      'Navigation erased the old response');
    await student.locator('#numerical-answer').fill('3.5'); await saved(student);
    await student.reload(); await student.locator('#fs-continue-windowed').click();
    assert.strictEqual(Number(await student.locator('#numerical-answer').inputValue()), 3.5);
    assert(!(await student.locator('#question-answer-instruction').textContent()).includes('older single-choice response'));
    await student.locator('#btn-open-submit-modal').click();
    await Promise.all([student.waitForURL('**/completed/'), student.locator('#btn-confirm-submit').click()]);
    await saveMultiEditor(student, data.practiceMulti, '**/practice/**/settings/**');
    await student.goto(base + data.practiceNumber);
    assert.strictEqual(await student.locator('#id_detected_type').inputValue(), 'numerical');
    assert(await student.locator('#id_numeric_tolerance').isVisible());
    assert(!(await student.locator('#id_multi_scoring_mode').isVisible()));
    await student.locator('#id_typed_key').fill('-2.5');
    await student.locator('#id_numeric_tolerance').fill('0.01');
    await student.locator('#id_confirm_answer_key').check();
    await Promise.all([student.waitForURL('**/practice/**/settings/**'), student.getByRole('button', {name:'Save question'}).click()]);
    await student.locator('[data-question-filter="missing-key"]').click();
    assert.strictEqual(await student.locator('[data-review-question]:visible').count(), 1);
    assert.strictEqual(await student.locator('[data-review-question]:visible').getAttribute('data-number'), '3');
    await student.locator('[data-question-filter="all"]').click();
    await student.locator('#id_duration_minutes').fill('17');
    await student.locator('#id_answer_key_text').fill('3:2');
    assert((await student.locator('[data-manual-key-preview]').textContent()).includes('Q3: B'));
    assert((await student.locator('[data-manual-key-preview]').textContent()).includes('2 saved confirmed'));
    await student.locator('#id_confirm_answer_key').check();
    await Promise.all([student.waitForURL('**/exams/**'), student.getByRole('button', {name:'Create private test'}).click()]);
    await student.locator('#declaration-check').check();
    await Promise.all([student.waitForURL('**/exams/session/**'), student.locator('#start-btn').click()]);
    await take(student, '-2.49', true);
    await student.getByRole('link', {name:'Review my answers'}).click();
    assert((await student.locator('body').textContent()).includes('Partial credit'));
    assert((await student.locator('body').textContent()).includes('Checked explanation 1'));

    await login(teacher, data.teacher);
    await saveMultiEditor(teacher, data.classroomMulti, '**/classroom/papers/**/review/**');
    await teacher.locator('#id_duration_minutes').fill('23');
    await teacher.locator('#id_answer_key_text').fill('2:0, 3:2');
    const preview = await teacher.locator('[data-manual-key-preview]').textContent();
    assert(preview.includes('Q2: 0') && preview.includes('Q3: B'), preview);
    await teacher.locator('#id_show_results').check();
    await teacher.locator('#id_confirm_review').check();
    await Promise.all([teacher.waitForURL('**/classroom/sessions/**/'), teacher.getByRole('button', {name:'Create test & join code'}).click()]);
    const code = (await teacher.locator('#join-code').textContent()).trim();
    await student.goto(base + data.join);
    await student.locator('input[name=code]').fill(code);
    await Promise.all([student.waitForURL('**/classroom/take/**'), student.getByRole('button', {name:'Join test'}).click()]);
    await Promise.all([student.waitForURL('**/exams/session/**'), student.getByRole('button', {name:/Start test/}).click()]);
    await take(student, '0', false);
    await student.getByRole('link', {name:'View result & review'}).click();
    await student.getByRole('link', {name:'Review answers'}).click();
    assert((await student.locator('body').textContent()).includes('Partial credit'));
    assert((await student.locator('body').textContent()).includes('Checked explanation 1'));
    await student.locator('.filter-btn[data-filter="partial"]').click();
    assert.strictEqual(await student.locator('.review-card:visible').count(), 1);
    assert.strictEqual(await student.locator('.review-card:visible').getAttribute('data-status'), 'partial');
    await student.locator('.filter-btn[data-filter="correct"]').click();
    assert.strictEqual(await student.locator('.review-card:visible').count(), 2);
    assert.deepStrictEqual(errors, [], `Browser page errors: ${errors.join('; ')}`);
    console.log('TYPED_BROWSER_OK');
  } finally { await mobile.close(); await desktop.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
