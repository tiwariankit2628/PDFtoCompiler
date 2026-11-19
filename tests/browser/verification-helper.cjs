/* Read disposable test-backend mail and use the real signed, POST-confirmed link. */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

async function confirmVerification(page, base, email) {
  await page.waitForURL('**/auth/verify-email/pending/');
  assert((await page.locator('body').innerText()).includes('Verify'));
  const folder = process.env.BROWSER_TEST_EMAIL_DIR;
  if (!folder) throw new Error('Disposable browser-test email directory not configured.');
  const messages = fs.readdirSync(folder).map(name => fs.readFileSync(path.join(folder, name), 'utf8'));
  const message = messages.find(text => text.includes(email));
  assert(message, 'Synthetic verification email was not written.');
  const match = message.match(/https?:\/\/[^\s]+\/auth\/verify-email\/[^\s/]+\//);
  assert(match && match[0].startsWith(process.env.BROWSER_TEST_EMAIL_BASE + '/auth/verify-email/'), 'Signed verification URL missing or has an unexpected configured origin.');
  // Mail uses a configured HTTPS origin; the disposable live server is HTTP.
  await page.goto(base + new URL(match[0]).pathname);
  assert(await page.getByRole('heading', {name: 'Confirm your email.'}).isVisible());
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && r.url().includes('/auth/verify-email/')),
    page.getByRole('button', {name: 'Verify email'}).click(),
  ]);
  if (response.status() >= 400) {
    const body = await response.text();
    const reason = body.match(/<pre>\s*([^<]+)\s*<\/pre>/);
    throw new Error(`Verification confirmation failed: HTTP ${response.status()} ${reason ? reason[1] : body.slice(-1000)}`);
  }
  await page.waitForURL('**/dashboard/**');
}
module.exports = {confirmVerification};
