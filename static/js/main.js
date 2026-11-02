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

  // Tooltip initialization
  const tooltipTriggerList = [].slice.call(
    document.querySelectorAll('[data-bs-toggle="tooltip"]')
  );
  tooltipTriggerList.map(function (tooltipTriggerEl) {
    return new bootstrap.Tooltip(tooltipTriggerEl);
  });
});

