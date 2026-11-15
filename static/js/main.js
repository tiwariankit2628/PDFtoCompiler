/**
 * Commercial CBT Examination Platform - Core Frontend Scripts
 */

document.addEventListener("DOMContentLoaded", function () {
  // Auto-dismiss alerts after 5 seconds if not closed manually
  const alerts = document.querySelectorAll(".alert-dismissible");
  alerts.forEach(function (alert) {
    setTimeout(function () {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
      if (bsAlert) {
        bsAlert.close();
      }
    }, 6000);
  });

  // Close the mobile menu after choosing a link so the sticky header does not
  // cover the destination (especially same-page #features/#categories links).
  const navigation = document.getElementById("cbtNavbar");
  if (navigation && typeof bootstrap !== "undefined") {
    navigation.querySelectorAll(".navbar-nav .nav-link").forEach(function (link) {
      link.addEventListener("click", function () {
        if (navigation.classList.contains("show")) {
          bootstrap.Collapse.getOrCreateInstance(navigation, { toggle: false }).hide();
        }
      });
    });
  }

  // Password visibility is an enhancement; the underlying fields work without JS.
  document.querySelectorAll('[data-password-toggle]').forEach(function (button) {
    const input = document.getElementById(button.dataset.passwordToggle);
    if (!input) return;
    button.hidden = false;
    button.addEventListener('click', function () {
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.textContent = show ? 'Hide' : 'Show';
      button.setAttribute('aria-pressed', String(show));
      const name = input.id.includes('confirm') ? 'confirmation password' : 'password';
      button.setAttribute('aria-label', `${show ? 'Hide' : 'Show'} ${name}`);
    });
  });

  // Keep native file inputs usable; drag-and-drop is an optional enhancement.
  document.querySelectorAll('[data-file-drop]').forEach(function (zone) {
    const input = zone.querySelector('input[type="file"]');
    const label = zone.querySelector('[data-file-name]');
    if (!input) return;
    input.addEventListener('change', function () {
      if (label) label.textContent = input.files.length ? `Selected: ${input.files[0].name}` : 'Choose a PDF or drop it here.';
    });
    ['dragenter', 'dragover'].forEach(function (eventName) {
      zone.addEventListener(eventName, function (event) { event.preventDefault(); zone.classList.add('is-dragging'); });
    });
    ['dragleave', 'drop'].forEach(function (eventName) {
      zone.addEventListener(eventName, function (event) { event.preventDefault(); zone.classList.remove('is-dragging'); });
    });
    zone.addEventListener('drop', function (event) {
      if (!event.dataTransfer || !event.dataTransfer.files.length) return;
      try { input.files = event.dataTransfer.files; input.dispatchEvent(new Event('change', { bubbles: true })); }
      catch (_) { if (label) label.textContent = 'Please choose your PDF using the file selector.'; }
    });
  });
  document.querySelectorAll('[data-upload-form]').forEach(function (form) {
    // Restoring a page from the back/forward cache must not leave it locked.
    window.addEventListener('pageshow', function (event) {
      if (!event.persisted) return;
      delete form.dataset.submitting;
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector('[data-upload-status]');
      if (button) button.disabled = false;
      if (status) status.textContent = '';
    });
    form.addEventListener('submit', function (event) {
      if (!form.checkValidity()) return;
      if (form.dataset.submitting === 'true') { event.preventDefault(); return; }
      form.dataset.submitting = 'true';
      const status = form.querySelector('[data-upload-status]');
      const button = form.querySelector('button[type="submit"]');
      if (status) status.textContent = 'Uploading and preparing extraction. Please keep this page open.';
      if (button) button.disabled = true;
    });
  });

  // Tooltip initialization
  const tooltipTriggerList = [].slice.call(
    document.querySelectorAll('[data-bs-toggle="tooltip"]')
  );
  tooltipTriggerList.map(function (tooltipTriggerEl) {
    return new bootstrap.Tooltip(tooltipTriggerEl);
  });
});

