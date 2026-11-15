/* Small, progressive enhancements for the public homepage. No network calls. */
(() => {
  const init = () => {
    const demoOptions = [...document.querySelectorAll('[data-demo-option]')];
    const demoStatus = document.querySelector('[data-demo-status]');
    demoOptions.forEach((button) => {
      button.addEventListener('click', () => {
        demoOptions.forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
        if (demoStatus) demoStatus.textContent = `Option ${button.querySelector('span').textContent} selected · demo only`;
      });
    });

    const carousel = document.querySelector('[data-carousel]');
    if (carousel) {
      const slides = [...carousel.querySelectorAll('[data-slide]')];
      const dots = [...carousel.querySelectorAll('[data-carousel-dot]')];
      const controls = carousel.querySelector('[data-carousel-controls]');
      const count = carousel.querySelector('[data-carousel-current]');
      const pause = carousel.querySelector('[data-carousel-pause]');
      const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
      const announcement = document.createElement('span');
      announcement.className = 'visually-hidden';
      announcement.setAttribute('aria-live', 'polite');
      carousel.appendChild(announcement);
      let current = 0;
      let pausedByUser = reducedMotion.matches;
      let hovered = false;
      let focused = false;
      let timer;

      const show = (index, announce = false) => {
        current = (index + slides.length) % slides.length;
        slides.forEach((slide, i) => { slide.hidden = i !== current; });
        dots.forEach((dot, i) => dot.setAttribute('aria-pressed', String(i === current)));
        if (count) count.textContent = String(current + 1).padStart(2, '0');
        if (announce) announcement.textContent = `Slide ${current + 1} of ${slides.length}: ${slides[current].querySelector('h3').textContent.trim()}`;
      };
      const updatePauseButton = () => {
        const label = pausedByUser ? 'Play automatic slides' : 'Pause automatic slides';
        pause.setAttribute('aria-label', label);
        pause.setAttribute('title', label);
        pause.innerHTML = `<i class="bi ${pausedByUser ? 'bi-play-fill' : 'bi-pause-fill'}" aria-hidden="true"></i><span class="visually-hidden">${label}</span>`;
      };
      const schedule = () => {
        window.clearTimeout(timer);
        if (!pausedByUser && !hovered && !focused && !document.hidden && slides.length > 1) {
          timer = window.setTimeout(() => { show(current + 1); schedule(); }, 6500);
        }
      };
      const move = (index) => { show(index, true); schedule(); };
      if (slides.length && controls && pause) {
        controls.hidden = false;
        show(0);
        updatePauseButton();
        carousel.querySelector('[data-carousel-prev]').addEventListener('click', () => move(current - 1));
        carousel.querySelector('[data-carousel-next]').addEventListener('click', () => move(current + 1));
        dots.forEach((dot, i) => dot.addEventListener('click', () => move(i)));
        pause.addEventListener('click', () => { pausedByUser = !pausedByUser; updatePauseButton(); schedule(); });
        carousel.addEventListener('mouseenter', () => { hovered = true; schedule(); });
        carousel.addEventListener('mouseleave', () => { hovered = false; schedule(); });
        carousel.addEventListener('focusin', () => { focused = true; schedule(); });
        carousel.addEventListener('focusout', () => window.setTimeout(() => { focused = carousel.contains(document.activeElement); schedule(); }, 0));
        document.addEventListener('visibilitychange', schedule);
        if (reducedMotion.addEventListener) reducedMotion.addEventListener('change', (event) => { if (event.matches) { pausedByUser = true; updatePauseButton(); schedule(); } });
        schedule();
      }
    }

    const roleTabs = document.querySelector('[data-role-tabs]');
    if (roleTabs) {
      const tabs = [...roleTabs.querySelectorAll('[data-role-tab]')];
      const panels = [...document.querySelectorAll('[data-role-panel]')];
      const activate = (tab, focus = false) => {
        tabs.forEach((item) => { const selected = item === tab; item.setAttribute('aria-selected', String(selected)); item.tabIndex = selected ? 0 : -1; });
        panels.forEach((panel) => { panel.hidden = panel.dataset.rolePanel !== tab.dataset.roleTab; });
        if (focus) tab.focus();
      };
      if (tabs.length && panels.length) {
        roleTabs.hidden = false;
        activate(tabs[0]);
        tabs.forEach((tab, index) => {
          tab.addEventListener('click', () => activate(tab));
          tab.addEventListener('keydown', (event) => {
            if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
            event.preventDefault();
            const delta = event.key === 'ArrowRight' ? 1 : -1;
            activate(tabs[(index + delta + tabs.length) % tabs.length], true);
          });
        });
      }
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
