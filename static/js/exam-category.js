/* Refresh the server-scoped category choices without discarding an exam draft. */
(() => {
  const section = document.querySelector('[data-exam-category]');
  const button = section && section.querySelector('[data-category-refresh]');
  const select = section && section.querySelector('select');
  const notice = section && section.querySelector('[data-category-empty]');
  const status = section && section.querySelector('[data-category-status]');
  if (!button || !select || !notice || !status) return;

  button.hidden = false;
  select.addEventListener('change', () => {
    const error = section.querySelector('[data-category-error]');
    if (error && select.value) error.hidden = true;
  });
  button.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Checking available categories…';
    try {
      // This is the same authenticated form GET. No category or permission is
      // created by refreshing; the POST will validate the selected value again.
      const response = await fetch(window.location.pathname, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.redirected) throw new Error('Could not refresh. Please sign in again or reload the page.');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const fresh = doc.querySelector('[data-exam-category] select');
      if (!fresh) throw new Error('Could not refresh the list. Please reload the page.');
      const previous = select.value;
      select.replaceChildren(...Array.from(fresh.options, option => option.cloneNode(true)));
      if (Array.from(select.options).some(option => option.value === previous)) select.value = previous;
      if (Array.from(select.options).some(option => option.value)) {
        notice.hidden = true;
        status.textContent = 'Categories updated. Select a category above; your other entries are unchanged.';
        select.focus();
      } else {
        status.textContent = 'No categories are available yet. Ask an administrator to check the catalog and your access.';
      }
    } catch (error) {
      status.textContent = error.message || 'Could not refresh the list. Please reload the page.';
    } finally {
      button.disabled = false;
    }
  });
})();
