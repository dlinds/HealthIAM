// Theme toggle, tooltips, and HTMX niceties.
(function () {
  var toggle = document.getElementById("themeToggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) {}
    });
  }

  function initTooltips(scope) {
    (scope || document).querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
      if (!bootstrap.Tooltip.getInstance(el)) new bootstrap.Tooltip(el);
    });
  }
  initTooltips();

  // Re-init Bootstrap widgets and close modals after HTMX swaps.
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    initTooltips(evt.detail.elt);
  });
  document.body.addEventListener("closeModal", function () {
    document.querySelectorAll(".modal.show").forEach(function (m) {
      var inst = bootstrap.Modal.getInstance(m);
      if (inst) inst.hide();
    });
  });
  // Forms with class js-autosubmit submit on change.
  document.addEventListener("change", function (e) {
    var form = e.target.closest("form.js-autosubmit");
    if (form) form.requestSubmit();
  });
})();
