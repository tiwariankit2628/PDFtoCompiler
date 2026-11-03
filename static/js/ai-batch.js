/* Bounded, explicitly consented client-side queue. Each POST is individually
   authorized, quota-checked and deduplicated by the existing server endpoint. */
(function () {
  const panel = document.getElementById('ai-batch-panel');
  const form = document.getElementById('ai-batch-form');
  if (!panel || !form || !window.fetch) return;
  const selections = [...document.querySelectorAll('.ai-batch-select')];
  if (!selections.length) return;
  panel.hidden = false;
  selections.forEach(node => { node.hidden = false; });
  const status = document.getElementById('ai-batch-status');
  const button = form.querySelector('button[type=submit]');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const boxes = selections.map(node => node.querySelector('input')).filter(input => input && input.checked);
    if (!form.querySelector('#batch-consent').checked) { status.textContent = 'Consent is required.'; return; }
    if (!boxes.length || boxes.length > 5) { status.textContent = 'Select between one and five questions.'; return; }
    button.disabled = true;
    selections.forEach(node => { node.querySelector('input').disabled = true; });
    form.querySelectorAll('input').forEach(input => { input.disabled = true; });
    let finished = 0, stopped = false;
    try {
      for (const input of boxes) {
        const url = new URL(input.dataset.requestUrl, window.location.origin);
        if (url.origin !== window.location.origin) throw new Error('Invalid request address.');
        status.textContent = `Requesting ${finished + 1} of ${boxes.length}…`;
        const data = new URLSearchParams({consent:'yes'});
        if (form.querySelector('#batch-vision').checked) data.set('vision','yes');
        const response = await fetch(url.href, {method:'POST', credentials:'same-origin',
          headers:{'X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value}, body:data});
        const destination = new URL(response.url);
        if (!response.ok || destination.origin !== window.location.origin || !destination.pathname.endsWith('/ai/')) {
          stopped = true; status.textContent = `Batch stopped after ${finished} of ${boxes.length}. Check your active test, plan quota or session before retrying.`; break;
        }
        const documentResult = new DOMParser().parseFromString(await response.text(), 'text/html');
        const item = documentResult.querySelector('[data-ai-status]');
        input.checked = false;
        const label = input.closest('.ai-batch-select').querySelector('.ai-batch-outcome');
        if (label) label.textContent = item ? ` · ${item.dataset.aiStatus}` : ' · check AI review';
        finished++;
      }
      if (!stopped) status.textContent = `Finished ${finished} request${finished === 1 ? '' : 's'}. Open each AI answer review to check or confirm its result.`;
    } catch (_) {
      status.textContent = `Batch interrupted after ${finished} of ${boxes.length}. An in-flight request may still be billed; check its AI review before retrying.`;
    } finally {
      button.disabled = false;
      selections.forEach(node => { node.querySelector('input').disabled = false; });
      form.querySelectorAll('input').forEach(input => { input.disabled = false; });
    }
  });
})();
