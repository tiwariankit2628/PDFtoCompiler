/* An empty catalog must be recoverable without losing the exam draft. */
const {chromium} = require('playwright-core');
const assert = require('assert');

(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage']});
  const base = process.env.CATEGORY_TEST_URL;
  const contexts = [];
  const errors = [];
  async function login(username, password) {
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    contexts.push(context);
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${base}/auth/login/`);
    await page.locator('#id_username').fill(username);
    await page.locator('#id_password').fill(password);
    await Promise.all([
      page.waitForURL('**/dashboard/**'),
      page.locator('form').filter({has: page.locator('#id_username')}).locator('button[type=submit]').click(),
    ]);
    await page.goto(base + process.env.CATEGORY_TEST_CREATE_PATH);
    return page;
  }
  try {
    const admin = await login('category_browser_admin', 'CategoryBrowserPass987!');
    const teacher = await login('category_browser_teacher', 'CategoryBrowserPass987!');
    const adminSection = admin.locator('[data-exam-category]');
    const teacherSection = teacher.locator('[data-exam-category]');
    assert((await adminSection.textContent()).includes('No examination categories are available'));
    assert((await teacherSection.textContent()).includes('Ask an administrator'));
    assert.strictEqual(await teacherSection.locator('a').count(), 0);
    const forbidden = await teacher.request.get(base + process.env.CATEGORY_TEST_CATEGORY_CREATE_PATH);
    assert.strictEqual(forbidden.status(), 403);

    await admin.locator('#id_title').fill('Admin category draft');
    await admin.locator('#id_subject').fill('Biology');
    await teacher.locator('#id_title').fill('Teacher category draft');
    await teacher.locator('#id_subject').fill('Physics');
    await teacherSection.locator('[data-category-refresh]').click();
    await teacherSection.locator('[data-category-status]').getByText('No categories are available yet.', {exact: false}).waitFor();
    assert.strictEqual(await teacher.locator('#id_title').inputValue(), 'Teacher category draft');

    const [popup] = await Promise.all([
      admin.waitForEvent('popup'),
      adminSection.getByRole('link', {name: 'Create an active category'}).click(),
    ]);
    popup.on('pageerror', error => errors.push(error.message));
    await popup.waitForLoadState('domcontentloaded');
    await popup.locator('#id_name').fill('NEET & JEE');
    assert.strictEqual(await popup.locator('#id_slug').inputValue(), '');
    await popup.locator('#id_is_active').check();
    await Promise.all([
      popup.waitForURL('**/categories/'),
      popup.getByRole('button', {name: 'Save Category'}).click(),
    ]);
    await popup.close();

    for (const [page, section, title, subject] of [
      [admin, adminSection, 'Admin category draft', 'Biology'],
      [teacher, teacherSection, 'Teacher category draft', 'Physics'],
    ]) {
      await section.locator('[data-category-refresh]').click();
      await section.locator('[data-category-status]').getByText('Categories updated.', {exact: false}).waitFor();
      assert.strictEqual(await page.locator('#id_title').inputValue(), title);
      assert.strictEqual(await page.locator('#id_subject').inputValue(), subject);
      assert(!(await section.locator('[data-category-empty]').isVisible()));
      await page.locator('#id_category').selectOption({label: 'NEET & JEE'});
      await page.locator('#id_target_type').selectOption('jee');
      await Promise.all([
        page.waitForURL('**/questions/'),
        page.getByRole('button', {name: /Save & Build Questions/}).click(),
      ]);
      assert((await page.textContent('body')).includes(title));
    }
    assert.deepStrictEqual(errors, []);
    console.log('EXAM_CATEGORY_BROWSER_OK');
  } finally {
    for (const context of contexts) await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
