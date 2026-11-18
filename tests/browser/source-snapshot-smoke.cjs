/* Local synthetic PDFs: clean image-first CBT, source fallbacks, and answer persistence. */
const {chromium} = require('playwright-core');
const assert = require('assert');

(async () => {
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args});
  const context = await browser.newContext({viewport: {width: 390, height: 844}});
  const page = await context.newPage();
  const errors = [];
  const requestedUrls = new Set();
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requestedUrls.add(request.url()));
  try {
    await page.goto(process.env.SOURCE_TEST_URL + '/auth/login/');
    await page.locator('#id_username').fill('source_browser_student');
    await page.locator('#id_password').fill('SourceBrowserPass987!');
    await Promise.all([
      page.waitForURL('**/dashboard/**'),
      page.locator('form').filter({has: page.locator('#id_username')}).locator('button[type=submit]').click(),
    ]);
    await page.goto(process.env.SOURCE_TEST_URL + process.env.SOURCE_TEST_SESSION_PATH);
    await page.locator('#fs-continue-windowed').click();
    const parallel = process.env.SOURCE_TEST_PARALLEL === '1';
    const parallelImage = process.env.SOURCE_TEST_PARALLEL_IMAGE === '1';
    const missingCrop = process.env.SOURCE_TEST_NO_CROP === '1';
    const continuation = process.env.SOURCE_TEST_CONTINUATION === '1';
    const partial = process.env.SOURCE_TEST_PARTIAL === '1';
    const publicExam = process.env.SOURCE_TEST_PUBLIC === '1';
    const payload = JSON.parse(await page.locator('#questions-data').textContent());
    const absoluteUrl = url => new URL(url, process.env.SOURCE_TEST_URL).href;
    const assertNoDefaultSources = async () => {
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
      assert(!(await page.locator('#question-diagram-box').isVisible()));
      for (const source of payload[0].source_images) {
        assert(!requestedUrls.has(absoluteUrl(source.url)), 'A hidden source snapshot was requested');
      }
    };
    const waitForCrops = async count => page.waitForFunction(expected => {
      const images = [...document.querySelectorAll('#question-crop-box img')];
      return images.length === expected && images.every(image => image.complete && image.naturalWidth > 0);
    }, count);
    assert.strictEqual(await page.locator('.option-card').count(), 4);
    const statement = page.locator('#question-statement-box');
    const assertAccessibleTextOnly = async () => {
      assert.strictEqual(await page.locator('#question-accessibility, #question-accessibility-summary').count(), 0,
        'A normal image-first test must not show a text/source toggle');
      assert(await statement.evaluate(el => el.classList.contains('visually-hidden') && !el.hidden && !el.closest('[aria-hidden="true"]')),
        'Extracted text must remain available to assistive technology');
      assert((await statement.ariaSnapshot()).trim(), 'Extracted text must be present in the accessibility tree');
    };
    const assertVisibleFallback = async () => {
      assert(await statement.isVisible());
      assert(!(await statement.evaluate(el => el.classList.contains('visually-hidden'))));
    };

    if (publicExam) {
      // A teacher-created exam uses public image storage and explicit saving,
      // but should have the same clean rendering and fixed printed order.
      assert(payload[0].image_first);
      assert.strictEqual(payload[0].source_images.length, 2);
      await waitForCrops(1);
      await assertNoDefaultSources();
      assert.deepStrictEqual(await page.locator('.option-card .option-badge').allTextContents(), ['A', 'B', 'C', 'D']);
      await assertAccessibleTextOnly();
      await page.locator('.option-card').nth(2).click();
      assert(![...requestedUrls].some(url => url.includes('/save-answer/')), 'Teacher exam changed to automatic saving');
      const [saved] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('#btn-save-next').click(),
      ]);
      assert.strictEqual(saved.status(), 200);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      await waitForCrops(1);
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'C');
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
      await page.locator('#btn-open-submit-modal').click();
      await Promise.all([
        page.waitForURL('**/completed/'),
        page.locator('#btn-confirm-submit').click(),
      ]);
    } else if (parallelImage) {
      // A separately captured language is not duplicated by default. Its
      // saved source remains available automatically if the original fails.
      assert(payload[0].image_first && payload[0].crop_urls.length > 0);
      assert(payload[0].source_images.some(source => source.kind === 'parallel-source'));
      await waitForCrops(payload[0].crop_urls.length);
      await assertNoDefaultSources();
      await assertAccessibleTextOnly();
      assert.deepStrictEqual(await page.locator('.option-card .option-badge').allTextContents(), ['A', 'B', 'C', 'D']);
      const [saved] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('.option-card').filter({hasText: 'English 1-2'}).click(),
      ]);
      assert.strictEqual(saved.status(), 200);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'B');
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
    } else if (continuation) {
      // Both original crops of a multi-page question must remain visible and
      // ordered; only the additional source snapshots are hidden by default.
      assert(payload[0].image_first && payload[1].image_first);
      assert.strictEqual(payload[0].crop_urls.length, 2);
      assert.strictEqual(payload[0].source_images.filter(source => source.kind === 'option-source').length, 2);
      await waitForCrops(2);
      assert.deepStrictEqual(await page.locator('#question-crop-box img').evaluateAll(images =>
        images.map(image => image.getAttribute('src'))), payload[0].crop_urls);
      await assertNoDefaultSources();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
      assert.deepStrictEqual(await page.locator('.option-card .option-badge').allTextContents(), ['A', 'B', 'C', 'D']);
      assert(/^\d{2}:\d{2}:\d{2}$/.test((await page.locator('#exam-timer').textContent()).trim()));

      // The continuation can be zoomed independently by keyboard; source
      // snapshots remain stored and will appear automatically on failure.
      await page.locator('.crop-zoom-trigger').nth(1).focus();
      await page.keyboard.press('Enter');
      assert(await page.locator('#crop-zoom-overlay').isVisible());
      assert.strictEqual(await page.locator('#crop-zoom-img').getAttribute('src'), payload[0].crop_urls[1]);
      await page.keyboard.press('Escape');
      assert(await page.locator('.crop-zoom-trigger').nth(1).evaluate(el => el === document.activeElement));
      await assertAccessibleTextOnly();
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);

      const [saved] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('.option-card').nth(3).click(),
      ]);
      assert.strictEqual(saved.status(), 200);
      assert.strictEqual((await page.locator('#count-answered').textContent()).trim(), '1');
      assert.strictEqual(await page.locator('#palette-btn-0').getAttribute('aria-current'), 'true');

      // Simulate a late error event from a detached first-question image after
      // navigation. It must not reveal the first question's text or sources
      // over the second question.
      await page.evaluate(() => { window.oldQuestionCropForTest = document.querySelector('#question-crop-box img'); });
      const [navigated] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('#palette-btn-1').click(),
      ]);
      assert.strictEqual(navigated.status(), 200);
      assert.strictEqual((await page.locator('#current-q-num').textContent()).trim(), '2');
      await waitForCrops(payload[1].crop_urls.length);
      await page.evaluate(() => {
        window.oldQuestionCropForTest.dispatchEvent(new Event('error'));
        delete window.oldQuestionCropForTest;
      });
      assert.strictEqual((await page.locator('#crop-instruction').textContent()).trim(), 'Tap the image to zoom. Choose your answer below.');
      await assertAccessibleTextOnly();
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
      assert.strictEqual(await page.locator('#palette-btn-1').getAttribute('aria-current'), 'true');

      // A question without any saved source regions must still report a failed
      // original honestly and display whatever extracted text it has.
      assert.strictEqual(payload[1].source_images.length + payload[1].image_urls.length, 0);
      const secondQuestionCrop = absoluteUrl(payload[1].crop_urls[0]);
      await page.route(secondQuestionCrop, route => route.abort());
      const [sameQuestion] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('#palette-btn-1').click(),
      ]);
      assert.strictEqual(sameQuestion.status(), 200);
      await page.getByText('An original image could not be loaded', {exact: false}).waitFor({state: 'visible'});
      assert.strictEqual((await page.locator('#crop-instruction').textContent()).trim(), 'An original image could not be loaded. Showing available text.');
      await assertVisibleFallback();
      assert(!(await page.locator('#question-crop-box').isVisible()));
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
      assert.strictEqual(await page.locator('.option-card').count(), 4);
      await page.unroute(secondQuestionCrop);
      const [returned] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('#palette-btn-0').click(),
      ]);
      assert.strictEqual(returned.status(), 200);
      await waitForCrops(2);
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'D');
      // Save the current question as the server's resume point before reload.
      const [resaved] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('.option-card').nth(3).click(),
      ]);
      assert.strictEqual(resaved.status(), 200);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      await waitForCrops(2);
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'D');
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);

      // If only the continuation fails to load, keep the first crop and reveal
      // the source images and text for the whole question automatically.
      const continuationUrl = absoluteUrl(payload[0].crop_urls[1]);
      await page.route(continuationUrl, route => route.abort());
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      await page.getByText('An original image could not be loaded', {exact: false}).waitFor({state: 'visible'});
      await waitForCrops(1);
      assert.strictEqual(await page.locator('#question-crop-box img').first().getAttribute('src'), payload[0].crop_urls[0]);
      await assertVisibleFallback();
      assert.strictEqual(await page.locator('#question-diagram-box figcaption').filter({hasText: 'Original answer choices'}).count(), 2);
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'D');
      await page.unroute(continuationUrl);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      await waitForCrops(2);
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
    } else if (partial) {
      // A valid but incomplete crop cannot be detected from image loading.
      // Its second page is shown automatically via the fallback.
      assert(payload[0].image_first);
      assert.strictEqual(payload[0].crop_urls.length, 1);
      assert.strictEqual(payload[0].source_images.filter(source => source.kind === 'option-source').length, 2);
      await waitForCrops(1);
      assert(payload[0].missing_crop_pages.length > 0);
      await assertVisibleFallback();
      assert((await page.locator('#crop-instruction').textContent()).includes('missing a continuation'));
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'D');
      assert.strictEqual(await page.locator('#question-diagram-box figcaption').filter({hasText: 'Original answer choices'}).count(), 2);
      await page.waitForFunction(() => [...document.querySelectorAll('#question-diagram-box img')].every(image => image.complete && image.naturalWidth > 0));
    } else if (parallel || missingCrop) {
      // Text-mode questions (including an image-first item with no saved crop)
      // still show the source images automatically, without a hidden fallback.
      assert.strictEqual(payload[0].image_first, false);
      await assertVisibleFallback();
      assert.strictEqual(await page.locator('#question-accessibility-summary').count(), 0);
      const caption = parallel ? 'Other original-language region' : 'Original answer choices';
      await page.getByText(caption, {exact: false}).waitFor({state: 'visible'});
      await page.waitForFunction((label) => {
        const image = [...document.querySelectorAll('#question-diagram-box img')].find(item => item.alt.startsWith(label));
        return image && image.complete && image.naturalWidth > 0;
      }, caption);
      assert(await page.getByText('1=A, 2=B', {exact: false}).isVisible());
      if (parallel) {
        const [saved] = await Promise.all([
          page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
          page.locator('.option-card').filter({hasText: 'English 1-2'}).click(),
        ]);
        assert.strictEqual(saved.status(), 200);
        await page.reload();
        await page.locator('#fs-continue-windowed').click();
        await page.getByText(caption, {exact: false}).waitFor({state: 'visible'});
        assert(await page.locator('.option-card.selected').filter({hasText: 'English 1-2'}).isVisible());
      }
    } else {
      assert(payload[0].image_first && payload[0].crop_urls.length > 0, 'Expected an original question crop');
      assert(payload[0].source_images.length > 0, 'Fixture must include source snapshots');
      const main = page.locator('#question-crop-box img').first();
      await main.waitFor({state: 'visible'});
      await page.waitForFunction(() => {
        const image = document.querySelector('#question-crop-box img');
        return image && image.complete && image.naturalWidth > 0;
      });
      // The main crop is the only default question image; duplicate source
      // images are not inserted, displayed, or fetched until requested.
      await assertNoDefaultSources();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
      assert.deepStrictEqual(await page.locator('.option-card .option-badge').allTextContents(), ['A', 'B', 'C', 'D']);
      await assertAccessibleTextOnly();
      await main.click();
      assert(await page.locator('#crop-zoom-overlay').isVisible());
      await page.keyboard.press('Escape');
      assert(!(await page.locator('#crop-zoom-overlay').isVisible()));
      assert(await page.locator('.crop-zoom-trigger').first().evaluate(el => el === document.activeElement));

      // No extra controls or duplicate sources clutter a healthy image.
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);

      // The answer, timer and clean layout survive resizing and reload.
      await page.setViewportSize({width: 1440, height: 900});
      assert(await main.isVisible());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1));
      const [saved] = await Promise.all([
        page.waitForResponse(response => response.url().includes('/save-answer/') && response.request().method() === 'POST'),
        page.locator('.option-card').nth(1).click(),
      ]);
      assert.strictEqual(saved.status(), 200);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'B');
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);

      // A broken primary image must reveal available source material and text
      // rather than leaving an empty, unanswerable question.
      const primaryUrl = new URL(payload[0].crop_urls[0], process.env.SOURCE_TEST_URL).href;
      await page.route(primaryUrl, route => route.abort());
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      await page.getByText('An original image could not be loaded', {exact: false}).waitFor({state: 'visible'});
      await assertVisibleFallback();
      assert(await page.getByText('Original answer choices', {exact: false}).isVisible());
      assert.strictEqual(await page.locator('.option-card.selected .option-badge').textContent(), 'B');
      await page.unroute(primaryUrl);
      await page.reload();
      await page.locator('#fs-continue-windowed').click();
      assert.strictEqual(await page.locator('#question-diagram-box img').count(), 0);
      assert(await page.locator('#question-crop-box img').first().isVisible());
    }
    await page.waitForLoadState('networkidle');
    assert.strictEqual(errors.length, 0, errors.join('\n'));
    console.log('SOURCE_SNAPSHOT_BROWSER_OK');
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
