/* Join a real classroom from a second browser context and watch the teacher monitor update. */
const assert = require('assert');
const {chromium} = require('playwright-core');

(async () => {
  const base = process.env.CLASSROOM_TEST_URL;
  const data = JSON.parse(process.env.CLASSROOM_TEST_DATA || '{}');
  if (!base || !process.env.CHROMIUM_PATH) throw new Error('Run through the Django classroom browser test.');
  // Separate, concurrent logins need real browser-process isolation. Chromium's
  // single-process mode can mix request cookies between browser contexts.
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const teacherContext = await browser.newContext({viewport: {width: 1280, height: 900}});
  const studentContext = await browser.newContext({viewport: {width: 390, height: 850}});
  const teacher = await teacherContext.newPage();
  const student = await studentContext.newPage();
  const errors = [];
  teacher.on('pageerror', e => errors.push(e.message));
  student.on('pageerror', e => errors.push(e.message));
  async function login(page, username) {
    await page.goto(base + data.login);
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill(data.password);
    await Promise.all([page.waitForURL('**/dashboard/**'), page.locator('form button[type=submit]').click()]);
  }
  try {
    await login(teacher, data.teacher);
    await teacher.goto(base + data.session);
    assert.strictEqual((await teacher.locator('[data-count="joined"]').first().textContent()).trim(), '25');
    assert(!(await teacher.locator('#participant-pagination').isVisible()));

    await login(student, data.student);
    await student.goto(base + data.join);
    await student.locator('input[name=code]').fill(data.code);
    await Promise.all([student.waitForURL('**/classroom/take/**'), student.locator('form button').click()]);
    await teacher.waitForFunction(() => document.querySelector('[data-count="joined"]').textContent.trim() === '26', {timeout: 15000});
    await teacher.locator('[data-page-next] a').waitFor({state:'visible', timeout: 15000});
    await Promise.all([teacher.waitForURL('**?page=2'), teacher.locator('[data-page-next] a').click()]);
    const row = teacher.locator('#participant-body tr').filter({hasText: data.student});
    await row.waitFor();
    assert((await row.textContent()).includes('<img src=x onerror=alert(1)>'), 'Participant name should be shown as text');
    assert.strictEqual(await row.locator('img').count(), 0, 'Participant input was inserted as HTML');
    await teacher.locator('#participant-filter').fill(data.student);

    await Promise.all([student.waitForURL('**/exams/session/**'), student.getByRole('button', {name:/Start test/}).click()]);
    await row.getByText('In Progress', {exact:true}).waitFor({timeout:15000});
    assert.strictEqual(await row.locator('a').count(), 0, 'A live attempt must not link to a final result');
    assert.strictEqual(await teacher.locator('#participant-filter').inputValue(), data.student);

    await student.locator('#fs-continue-windowed').click();
    await student.locator('.option-card').filter({hasText:'Four'}).click();
    const [saveResponse] = await Promise.all([
      student.waitForResponse(r => r.url().includes('/save-answer/')),
      student.locator('#btn-save-next').click(),
    ]);
    assert.strictEqual(saveResponse.status(), 200, `Save failed: ${saveResponse.status()} ${await saveResponse.text()}`);
    await student.locator('#btn-open-submit-modal').click();
    await Promise.all([student.waitForURL('**/completed/'), student.locator('#btn-confirm-submit').click()]);
    await row.getByText('Submitted', {exact:true}).waitFor({timeout:15000});
    assert.strictEqual((await row.locator('td').nth(4).textContent()).trim(), '4Complete');
    await Promise.all([teacher.waitForURL('**/results/**'), row.getByRole('link', {name:'View'}).click()]);
    assert(await teacher.getByRole('heading', {name:'Classroom result', exact:true}).isVisible());
    assert.deepStrictEqual(errors, [], `Page errors: ${errors.join(', ')}`);
    console.log('CLASSROOM_BROWSER_OK');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
