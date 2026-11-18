(() => {
  const controls = document.querySelector('[data-question-filters]');
  if (!controls) return;
  const cards = [...document.querySelectorAll('[data-review-question]')];
  const buttons = [...controls.querySelectorAll('[data-question-filter]')];
  const search = controls.querySelector('[data-question-search]');
  const count = controls.querySelector('[data-question-count]');
  let active = 'all';
  function update() {
    const value = search.value.trim();
    let shown = 0;
    cards.forEach(card => {
      const match = (active === 'all' || (active === 'review' && card.dataset.needsReview === '1') ||
        (active === 'missing-key' && card.dataset.missingKey === '1')) && (!value || card.dataset.number === value);
      card.hidden = !match;
      if (match) shown++;
    });
    count.textContent = `${shown} of ${cards.length} shown`;
    buttons.forEach(button => {
      const selected = button.dataset.questionFilter === active;
      button.setAttribute('aria-pressed', String(selected));
      button.classList.toggle('btn-cbt-primary', selected);
      button.classList.toggle('btn-cbt-outline', !selected);
    });
  }
  buttons.forEach(button => button.addEventListener('click', () => { active = button.dataset.questionFilter; update(); }));
  search.addEventListener('input', update);
  update();
})();
