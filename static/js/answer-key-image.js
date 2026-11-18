/* Answer images are suggestions only. Nothing is submitted until a human
   compares the preview and explicitly copies selected values into the key. */
(() => {
  document.querySelectorAll('[data-answer-key-image]').forEach(widget => {
    const form = widget.closest('form');
    const keyText = form && form.querySelector('[name="answer_key_text"]');
    const files = widget.querySelector('[data-key-files]');
    const read = widget.querySelector('[data-key-read]');
    const status = widget.querySelector('[data-key-status]');
    const review = widget.querySelector('[data-key-review]');
    const imagePreviews = widget.querySelector('[data-key-image-previews]');
    const zoom = widget.querySelector('[data-key-zoom]');
    const zoomValue = widget.querySelector('[data-key-zoom-value]');
    const table = widget.querySelector('[data-key-rows]');
    const count = widget.querySelector('[data-key-count]');
    const filter = widget.querySelector('[data-key-filter]');
    const search = widget.querySelector('[data-key-search]');
    const confirmed = widget.querySelector('[data-key-confirm]');
    const apply = widget.querySelector('[data-key-apply]');
    if (!form || !keyText || !files || !read) return;
    let objectUrls = [];
    let rows = [];
    let waiting = false;
    let unapplied = false;
    let requestVersion = 0;
    let controller = null;

    const statuses = {
      detected: 'Detected — check against image',
      missing: 'Not detected — enter if known',
      duplicate: 'Repeated in image — choose after checking',
      unclear: 'Unclear or multiple-answer reading — check the original',
      ambiguous_question: 'Question number repeats — edit that question individually',
      invalid_option: 'Does not match available choices — check',
      invalid_choices: 'Choices are incomplete or repeated — edit this question',
      unsupported_type: 'Not a single-correct MCQ — review its type',
      already_approved: 'Already in the bank — edit the bank question separately',
    };
    function releasePreviews() {
      objectUrls.forEach(url => URL.revokeObjectURL(url));
      objectUrls = [];
      imagePreviews.replaceChildren();
    }
    function resetPreview() {
      releasePreviews();
      rows = [];
      table.replaceChildren();
      review.hidden = true;
      confirmed.checked = false;
      apply.disabled = true;
      unapplied = false;
    }
    function updateFilter() {
      const query = search.value.trim();
      table.querySelectorAll('tr').forEach(tr => {
        const matchesFilter = filter.value !== 'attention' || tr.dataset.attention === 'true';
        tr.hidden = !(matchesFilter && (!query || tr.dataset.number === query));
      });
    }
    function updateCount() {
      const chosen = rows.filter(row => row.select && row.select.value).length;
      count.textContent = `${chosen} selected / ${rows.length} questions — all need checking`;
      updateFilter();
    }
    function displayRows(data) {
      rows = data.rows.map(item => {
        const tr = document.createElement('tr');
        tr.dataset.number = String(item.number);
        tr.dataset.attention = String(item.status !== 'detected');
        const num = document.createElement('th');
        num.scope = 'row'; num.textContent = `Q${item.number}`;
        const cell = document.createElement('td');
        const select = document.createElement('select');
        select.className = 'form-select form-select-sm';
        select.setAttribute('aria-label', `Answer for question ${item.number}`);
        const blank = document.createElement('option');
        blank.value = ''; blank.textContent = 'Not entered';
        select.appendChild(blank);
        (item.options || []).forEach(key => {
          const option = document.createElement('option');
          option.value = key; option.textContent = key;
          select.appendChild(option);
        });
        select.value = item.answer || '';
        if (['ambiguous_question', 'unsupported_type', 'already_approved', 'invalid_choices'].includes(item.status)) select.disabled = true;
        select.addEventListener('change', () => {
          tr.dataset.attention = String(!select.value);
          confirmed.checked = false; apply.disabled = true;
          unapplied = true;
          updateCount();
        });
        cell.appendChild(select);
        const reason = document.createElement('td');
        reason.className = 'small text-muted';
        reason.textContent = statuses[item.status] || 'Review against image';
        if (item.existing_key) reason.textContent += ` · ${item.existing_confirmed ? 'confirmed' : 'unconfirmed'} saved key: ${item.existing_key}`;
        tr.append(num, cell, reason); table.appendChild(tr);
        return {item, select};
      });
      updateCount();
    }
    files.addEventListener('change', () => {
      // A result for a previous selection must never appear beside a new photo.
      ++requestVersion;
      if (controller) controller.abort();
      controller = null; waiting = false; read.disabled = false;
      resetPreview(); status.textContent = '';
    });
    zoom.addEventListener('input', () => {
      zoomValue.textContent = `${zoom.value}%`;
      imagePreviews.querySelectorAll('img').forEach(image => { image.style.width = `${zoom.value}%`; });
    });
    filter.addEventListener('change', updateFilter);
    search.addEventListener('input', updateFilter);
    confirmed.addEventListener('change', () => { apply.disabled = !confirmed.checked; });
    read.addEventListener('click', async () => {
      if (waiting) return;
      resetPreview();
      const selected = [...files.files];
      if (!selected.length || selected.length > 3) {
        status.textContent = 'Choose one to three JPEG or PNG images.'; return;
      }
      if (selected.some(file => file.size > 8 * 1024 * 1024) || selected.reduce((n, file) => n + file.size, 0) > 20 * 1024 * 1024) {
        status.textContent = 'Images must be 8 MB or less each and 20 MB or less combined.'; return;
      }
      zoom.value = '100'; zoomValue.textContent = '100%';
      selected.forEach((file, index) => {
        const url = URL.createObjectURL(file); objectUrls.push(url);
        const container = document.createElement('div');
        container.style.cssText = 'width:min(100%,24rem);max-height:28rem;overflow:auto';
        const link = document.createElement('a'); link.href = url; link.target = '_blank'; link.rel = 'noopener';
        const image = document.createElement('img'); image.src = url; image.alt = `Answer-key image ${index + 1}; open larger`;
        image.className = 'rounded border'; image.style.width = '100%'; image.style.maxWidth = 'none';
        link.appendChild(image); container.appendChild(link); imagePreviews.appendChild(container);
      });
      const body = new FormData();
      selected.forEach(file => body.append('images', file));
      const csrf = form.querySelector('[name="csrfmiddlewaretoken"]');
      const version = ++requestVersion;
      controller = new AbortController();
      waiting = true; read.disabled = true;
      status.textContent = 'Reading your image on this server. This may take a moment…';
      try {
        const response = await fetch(widget.dataset.previewUrl, {
          method: 'POST', body, credentials: 'same-origin', signal: controller.signal,
          headers: {'X-CSRFToken': csrf ? csrf.value : ''},
        });
        if (version !== requestVersion) return;
        if (response.redirected) throw new Error('Please finish your active test or sign in again before reading a key.');
        let data;
        try { data = await response.json(); } catch (_) { throw new Error('Could not read the server response. Try again or type the key.'); }
        if (version !== requestVersion) return;
        if (!response.ok) throw new Error(data.error || 'Could not read the image. Try again or type the key.');
        displayRows(data);
        review.hidden = false; unapplied = true;
        const unmatched = data.unmatched_count ? ` ${data.unmatched_count} number(s) did not match included questions.` : '';
        const unclear = data.unreadable_rows ? ` ${data.unreadable_rows} row(s) could not be reliably read.` : '';
        status.textContent = `${data.detected_count} suggested answer(s). Review all of them, including any missing entries.${unmatched}${unclear}`;
        if (data.unmatched_numbers && data.unmatched_numbers.length) {
          status.textContent += ` Unmatched: ${data.unmatched_numbers.join(', ')}${data.unmatched_count > data.unmatched_numbers.length ? '…' : ''}.`;
        }
      } catch (error) {
        if (version === requestVersion && error.name !== 'AbortError') {
          status.textContent = error.message || 'Could not read the image. Try again or type the key.';
          releasePreviews();
        }
      } finally {
        if (version === requestVersion) {
          controller = null; waiting = false; read.disabled = false;
        }
      }
    });
    apply.addEventListener('click', () => {
      if (!confirmed.checked) return;
      const selected = rows.filter(row => row.select.value && !row.select.disabled);
      if (!selected.length) { status.textContent = 'Select at least one answer, or continue without a key.'; return; }
      if (keyText.value.trim() && !window.confirm('Replace the answers currently typed or pasted in the box with these checked selections?')) return;
      const conflicts = selected.filter(row => row.item.existing_key && row.item.existing_key !== row.select.value).length;
      if (conflicts && !window.confirm(`${conflicts} selected answer(s) differ from keys saved on individual questions. Use the new selections for this test?`)) return;
      keyText.value = selected.map(row => `${row.item.number}: ${row.select.value}`).join(', ');
      keyText.dispatchEvent(new Event('input', {bubbles: true}));
      unapplied = false;
      status.textContent = widget.dataset.keyAction === 'import'
        ? `${selected.length} checked answer(s) added below. Apply them to staging when ready.`
        : `${selected.length} checked answer(s) added below. They will be used only when you create the test.`;
      keyText.focus();
    });
    form.addEventListener('submit', event => {
      if (waiting) {
        event.preventDefault(); status.textContent = 'Wait for image reading to finish, or type the key instead.';
      } else if (unapplied && !window.confirm('The image suggestions have not been applied. Continue without using them?')) {
        event.preventDefault();
      }
    });
    window.addEventListener('pagehide', releasePreviews);
  });
})();
