const {chromium} = require('playwright-core');
const fs = require('fs');
const assert = require('assert');
const {confirmVerification} = require('./verification-helper.cjs');
(async () => {
  const executablePath = process.env.CHROMIUM_PATH;
  if (!executablePath) throw new Error('Set CHROMIUM_PATH to a compatible Chromium executable.');
  const args = ['--no-sandbox', '--disable-dev-shm-usage'];
  if (process.env.CHROMIUM_SINGLE_PROCESS === '1') args.push('--single-process', '--no-zygote');
  const browser = await chromium.launch({executablePath, headless:true, args});
  const context = await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: '+e.message));
  page.on('console', msg => {if(msg.type()==='error') errors.push('console: '+msg.text());});
  page.on('response', response => {if(response.status() >= 500) errors.push('server '+response.status()+': '+response.url());});
  const base=process.env.PRACTICE_TEST_URL;
  const sourcePdf=process.env.PRACTICE_TEST_PDF;
  const reportPdf=process.env.PRACTICE_TEST_REPORT;
  if (!base || !sourcePdf || !reportPdf) throw new Error('Browser smoke test must be started through Django.');
  const tag='practice_smoke_'+Date.now();
  const step = x => console.log('PASS',x);
  try {
    await page.goto(base+'/auth/register/');
    for(const [field,value] of Object.entries({first_name:'Arena',last_name:'Browser',username:tag,email:tag+'@example.test',password:'PracticeBrowserPass987!',confirm_password:'PracticeBrowserPass987!'})) await page.locator('#id_'+field).fill(value);
    await page.locator('#id_target_exam').selectOption('JEE');
    await Promise.all([page.waitForURL('**/auth/verify-email/pending/'),page.locator('form').filter({has:page.locator('#id_username')}).locator('button[type=submit]').click()]);
    await confirmVerification(page, base, tag+'@example.test');
    assert((await page.locator('body').innerText()).toLowerCase().includes('your private workspace')); step('register, verify email and open student dashboard');

    await page.goto(base+'/questions/practice/upload/');
    await page.locator('#id_title').fill('Browser science practice');
    await page.locator('#id_target_type').selectOption('jee_main');
    await page.locator('#id_subject').fill('Physics');
    await page.locator('#id_pdf_file').setInputFiles(sourcePdf);
    await Promise.all([page.waitForURL('**/settings/'),page.getByRole('button',{name:/Upload & extract/i}).click()]);
    assert((await page.locator('body').innerText()).includes('Extracted questions')); step('PDF upload and real local extraction');

    // Complete any malformed staged items explicitly, never silently omit.
    while (await page.locator('.card').filter({has:page.getByText('Needs editing',{exact:true})}).count()) {
      const card=page.locator('.card').filter({has:page.getByText('Needs editing',{exact:true})}).first();
      await Promise.all([page.waitForLoadState('domcontentloaded'),card.getByRole('button',{name:'Exclude'}).click()]);
    }
    await page.getByRole('link',{name:'Edit',exact:true}).first().click();
    await page.locator('#id_question_text').fill('भारत में गति का प्रश्न १२३ / Browser motion question');
    await page.locator('#id_correct_option').selectOption('A');
    await page.locator('#id_confirm_answer_key').check();
    await page.locator('#id_explanation').fill('Confirmed explanation for the practice answer.');
    if(await page.locator('#id_reviewed').count()) await page.locator('#id_reviewed').check();
    await Promise.all([page.waitForURL('**/settings/'),page.getByRole('button',{name:'Save question'}).click()]);
    await page.locator('#id_duration_minutes').fill('30');
    await page.locator('#id_passing_percentage').fill('40');
    if(await page.locator('#id_acknowledge_review').count()) await page.locator('#id_acknowledge_review').check();
    if(await page.locator('#id_acknowledge_unassigned').count()) await page.locator('#id_acknowledge_unassigned').check();
    await Promise.all([page.waitForURL('**/instructions/'),page.getByRole('button',{name:'Create private test'}).click()]);
    assert((await page.locator('body').innerText()).includes('PRIVATE PRACTICE')); step('review questions, confirm key and configure duration');
    const instructionsUrl=page.url();

    await page.locator('#declaration-check').check();
    await Promise.all([page.waitForURL('**/session/**'),page.locator('#start-btn').click()]);
    const session1=page.url();
    await page.locator('#fs-continue-windowed').click();
    await Promise.all([page.waitForResponse(r => r.url().includes('/save-answer/') && r.status()===200), page.locator('.option-card').first().click()]);
    const timerBefore=await page.locator('#exam-timer').innerText();
    await page.waitForTimeout(1200); await page.reload();
    await page.locator('#fs-continue-windowed').click();
    const timerAfter=await page.locator('#exam-timer').innerText();
    assert(timerAfter<=timerBefore,`timer reset: ${timerBefore} -> ${timerAfter}`);
    assert(await page.locator('.option-card.selected').count()===1, 'selected answer was not auto-saved before refresh');
    await Promise.all([page.waitForResponse(r => r.url().includes('/save-answer/') && r.status()===200), page.locator('#btn-save-next').click()]);
    await page.locator('.option-card').first().click(); // final answer saved atomically during submit
    await page.locator('#btn-open-submit-modal').click();
    await page.locator('#btn-confirm-submit').click();
    await page.waitForURL('**/completed/');
    await page.getByRole('link',{name:'Review my answers'}).click();
    const firstResult=page.url();
    assert((await page.locator('body').innerText()).includes('Question-wise review'));
    assert((await page.locator('body').innerText()).includes('Confirmed key'));
    assert((await page.locator('body').innerText()).includes('Answered, ungraded'));
    // Image-first results keep the editable text in an accessible details
    // fallback rather than duplicating it visibly under the original crop.
    assert((await page.locator('body').textContent()).includes('भारत में गति का प्रश्न'));
    step('CBT save, atomic submit, keyed and ungraded result dashboard');

    const dlPromise=page.waitForEvent('download');
    await page.getByRole('link',{name:/Download PDF/}).click();
    const download=await dlPromise; await download.saveAs(reportPdf);
    assert(fs.statSync(reportPdf).size>10000); step('authorized PDF report download');

    assert.strictEqual(await page.getByRole('link',{name:/Reattempt|Retake/i}).count(), 0, 'a completed test offered a retake');
    await page.goto(instructionsUrl);
    assert((await page.locator('body').innerText()).toLowerCase().includes('attempt limit'), 'completed test did not show the no-retake limit');
    assert.strictEqual(await page.locator('#start-btn').count(), 0, 'completed test still has a start button');
    await page.goto(base+'/questions/practice/history/');
    assert((await page.locator('table tbody tr').count())>=1); step('no retake, saved result and practice history');

    await page.goto(base+'/questions/practice/bank/');
    assert((await page.locator('body').textContent()).includes('Browser motion question'));
    assert(await page.locator('img[alt^="Original question image"]').first().isVisible());
    const keyedBankCard = page.locator('.card').filter({hasText:'Confirmed key A'}).first();
    await keyedBankCard.getByRole('link',{name:'Images / recrop'}).click();
    const oldImagesUrl = page.url();
    await page.locator('#crop-replace').selectOption({index:1});
    for (const [id, value] of Object.entries({x0:'0.05',y0:'0.05',x1:'0.9',y1:'0.5'})) await page.locator('#'+id).fill(value);
    await Promise.all([page.waitForURL(url => url.toString() !== oldImagesUrl), page.getByRole('button',{name:'Save original crop'}).click()]);
    assert((await page.locator('body').innerText()).includes('Check the answer again'));
    await page.getByRole('link',{name:'Edit question and key'}).click();
    assert((await page.locator('body').innerText()).includes('unconfirmed suggestion'));
    await page.locator('#id_confirm_answer_key').check();
    await page.locator('#id_topic').fill('Mechanics'); await page.locator('#id_chapter').fill('Motion');
    await page.locator('#id_difficulty').selectOption('hard');
    await page.getByRole('button',{name:'Save changes'}).click();
    await page.getByRole('link',{name:'← My question bank'}).click();
    await page.locator('select[name=topic]').selectOption({label:'Mechanics'});
    await page.getByRole('button',{name:'Apply filters'}).click();
    assert((await page.locator('body').innerText()).includes('Mechanics'));
    await page.locator('input[name=question_ids]').first().check();
    await page.locator('#test-title').fill('Bank revision'); await page.locator('#test-duration').fill('20');
    await Promise.all([page.waitForURL('**/instructions/'),page.getByRole('button',{name:'Create test',exact:true}).click()]);
    assert((await page.locator('body').innerText()).includes('Bank revision'));
    await page.goto(base+'/questions/practice/');
    assert((await page.title()).includes('My PDF Practice') && !(await page.title()).includes('<div'), 'practice page title is invalid');
    assert((await page.locator('body').innerText()).includes('Bank revision'), 'bank test missing from practice list');
    step('bank edit, filtering and test creation');

    await page.goto(base+'/payments/plans/');
    const billing=await page.locator('body').innerText();
    assert(billing.includes('Razorpay test checkout is not configured'));
    assert(billing.includes('PDF practice pack') && billing.includes('20 additional PDF extractions'));
    assert(!billing.includes('safe_unit_secret')); step('usage dashboard and fail-closed unconfigured payment');
    await page.setViewportSize({width:390,height:844});
    await page.goto(firstResult);
    const widthInfo=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth, viewport:document.documentElement.clientWidth}));
    assert(widthInfo.scroll <= widthInfo.viewport, 'mobile layout overflows the viewport');
    if(errors.length) throw new Error('Browser console/page errors: '+JSON.stringify(errors));
    console.log('BROWSER_WORKFLOW_OK',JSON.stringify({firstResult,session1,tag,errors}));
  } finally { await context.close(); await browser.close(); }
})().catch(e=>{console.error('BROWSER_WORKFLOW_FAILED',e);process.exit(1)});
