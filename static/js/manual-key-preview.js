/* Helpful, non-authoritative preview of a typed/pasted key. All values are
   validated again on the server before they can be used for grading. */
(() => {
  const preview = document.querySelector('[data-manual-key-preview]');
  const data = document.getElementById('answer-key-preview-data');
  if (!preview || !data) return;
  const form = preview.closest('form');
  const input = form && form.querySelector('[name="answer_key_text"]');
  if (!input) return;
  let questions;
  try { questions = JSON.parse(data.textContent); } catch (_) { return; }
  const byNumber = new Map();
  questions.forEach(q => {
    const number = String(q.number);
    if (!byNumber.has(number)) byNumber.set(number, []);
    byNumber.get(number).push(q);
  });
  const summary = preview.querySelector('[data-manual-key-summary]');
  const details = preview.querySelector('[data-manual-key-details]');
  const confirmation = form.querySelector('[name="confirm_answer_key"], [name="confirm_review"], [name="confirm_checked"]');
  const pair = /(?<!\w)(\d+)\s*(?:[:.\-=]\s*|\s+)(\[[A-E1-5\s,;/|]+\]|\(\s*[A-E1-5]\s*\)|[A-E]|[+-]?(?:\d+(?:\.\d+)?|\.\d+))(?![\w.])/gi;
  const numeric = {'1':'A', '2':'B', '3':'C', '4':'D', '5':'E'};
  function describe(label, values) {
    if (!values.length) return;
    const li = document.createElement('li');
    li.textContent = `${label}: ${values.slice(0, 30).join(', ')}${values.length > 30 ? '…' : ''}`;
    details.appendChild(li);
  }
  function update() {
    const entries = new Map();
    const remainder = input.value.replace(pair, (_, rawNumber, rawAnswer) => {
      const number = String(Number(rawNumber));
      if (!entries.has(number)) entries.set(number, []);
      entries.get(number).push(rawAnswer.toUpperCase());
      return '';
    });
    const valid = [], invalid = [], missing = [], saved = [];
    entries.forEach((answers, number) => {
      const matches = byNumber.get(number) || [];
      let reason = '';
      if (answers.length > 1) reason = 'repeated';
      else if (!matches.length) reason = 'not in this paper';
      else if (matches.length > 1) reason = 'question number repeats';
      else if (matches[0].approved) reason = 'already in bank';
      let shown = answers[0];
      if (!reason) {
        const question = matches[0], kind = question.type || (question.single ? 'mcq_single' : 'unsupported');
        const token = shown.startsWith('(') && shown.endsWith(')') ? shown.slice(1, -1).trim() : shown;
        if (kind === 'numerical') {
          shown = token;
          if (!/^[+-]?(?:\d{1,12}(?:\.\d{1,6})?|\.\d{1,6})$/.test(token)) reason = 'invalid number';
        } else if (kind === 'mcq_single') {
          shown = numeric[token] || token;
          if (!/^[A-E]$/.test(shown) || question.options.filter(key => key === shown).length !== 1) reason = 'choice unavailable or repeated';
        } else if (kind === 'mcq_multiple') {
          if (!shown.startsWith('[') || !shown.endsWith(']')) reason = 'use brackets, e.g. [A,C]';
          else {
            const raw = shown.slice(1, -1).trim().split(/[\s,;/|]+/).filter(Boolean);
            const keys = raw.map(value => numeric[value] || value);
            if (!keys.length || keys.some(key => !/^[A-E]$/.test(key)) || new Set(keys).size !== keys.length
                || keys.some(key => question.options.filter(option => option === key).length !== 1)) reason = 'choice unavailable or repeated';
            else shown = `[${keys.sort().join(',')}]`;
          }
        } else reason = 'unsupported question type';
      }
      if (reason) invalid.push(`Q${number} (${reason})`);
      else valid.push(`Q${number}: ${shown}`);
    });
    byNumber.forEach((matches, number) => {
      if (matches.every(q => q.approved) || entries.has(number)) return;
      if (matches.length === 1 && matches[0].saved_confirmed) saved.push(`Q${number}`);
      else missing.push(`Q${number}`);
    });
    const malformed = remainder.replace(/[\s,;]+/g, '');
    summary.textContent = `${valid.length} entered · ${saved.length} saved confirmed · ${missing.length} missing · ${invalid.length + (malformed ? 1 : 0)} invalid or unclear`;
    details.replaceChildren();
    describe('Entered', valid);
    describe('Saved confirmed', saved);
    describe('Missing / unconfirmed', missing);
    describe('Invalid / ambiguous', invalid);
    if (malformed) describe('Unrecognized text — correct the key before continuing', [malformed.slice(0, 60)]);
  }
  input.addEventListener('input', () => {
    // A changed key needs a fresh confirmation, including image suggestions
    // copied into the text box. This is advisory; server checks remain decisive.
    if (confirmation) confirmation.checked = false;
    update();
  });
  preview.querySelector('[data-manual-key-clear]').addEventListener('click', () => {
    input.value = '';
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.focus();
  });
  preview.hidden = false;
  update();
})();
