/* Keep the original question primary, but never leave an inaccessible card
   when the image file is missing or a protected image fails to load. */
(() => {
  document.querySelectorAll('[data-image-question]').forEach(card => {
    const fallback = card.querySelector('[data-image-fallback]');
    const warning = card.querySelector('[data-image-error]');
    if (!fallback || !warning) return;
    const failed = image => {
      image.hidden = true;
      warning.hidden = false;
      fallback.open = true;
    };
    card.querySelectorAll('[data-primary-question-image]').forEach(image => {
      image.addEventListener('error', () => failed(image));
      // Also catch a cached failure that happened before this script ran.
      if (image.complete && image.currentSrc && image.naturalWidth === 0) failed(image);
    });
  });
})();
