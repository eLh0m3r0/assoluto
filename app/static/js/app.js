// Small vanilla JS glue for the portal.
// No framework; keeps the single static asset under 2 kB.

(function () {
  "use strict";

  // -------- mobile nav toggle --------
  // The header renders two copies of the nav: one inline (md+) and one
  // stacked in a drawer (below md). The hamburger button toggles the
  // drawer's visibility by flipping the ``hidden`` class.
  document.addEventListener("click", function (event) {
    var target = event.target.closest("#nav-toggle");
    if (!target) return;
    var drawer = document.getElementById("mobile-nav");
    if (!drawer) return;
    var open = drawer.classList.toggle("hidden") === false;
    target.setAttribute("aria-expanded", open ? "true" : "false");
  });

  // -------- clickable table rows --------
  // Rows in list tables carry ``data-href`` instead of an inline onclick
  // (CSP ``script-src 'self'`` forbids inline handlers). A middle-click
  // or ctrl/cmd-click should open in a new tab, matching native <a>.
  document.addEventListener("click", function (event) {
    var row = event.target.closest("[data-href]");
    if (!row) return;
    // Don't steal clicks on inline actions (buttons, links, forms).
    if (event.target.closest("a, button, input, form")) return;
    var href = row.getAttribute("data-href");
    if (!href) return;
    if (event.ctrlKey || event.metaKey || event.button === 1) {
      window.open(href, "_blank", "noopener");
    } else {
      window.location.href = href;
    }
  });

  // -------- destructive-action confirmation --------
  // Forms that carry ``data-confirm="..."`` pop up the native confirm
  // dialog on submit; cancelling aborts the submission. Replaces the
  // inline ``onsubmit="return confirm(...)"`` that CSP blocked.
  document.addEventListener("submit", function (event) {
    var form = event.target.closest("form[data-confirm]");
    if (!form) return;
    var msg = form.getAttribute("data-confirm") || "";
    if (msg && !window.confirm(msg)) {
      event.preventDefault();
    }
  });

  // -------- submit button "busy" state --------
  // Prevents double-submit by disabling the form's submit button(s) after
  // the first submit. Swaps the label with a spinner + original text so
  // the click feels alive. A 5s fallback re-enables the button in case
  // nothing actually navigated (validation error, network hiccup) so the
  // user isn't stranded with a dead form.
  var SPINNER_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" class="inline-block h-4 w-4 animate-spin mr-2 -mt-0.5 align-middle" fill="none" viewBox="0 0 24 24">' +
    '<circle class="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
    '<path class="opacity-80" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>' +
    "</svg>";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !(form instanceof HTMLFormElement)) return;
    // Skip forms that explicitly opt out — e.g. filters, search.
    if (form.hasAttribute("data-no-busy")) return;
    var buttons = form.querySelectorAll("button[type='submit'], input[type='submit']");
    buttons.forEach(function (btn) {
      if (btn.disabled) return;
      btn.disabled = true;
      btn.setAttribute("data-busy", "1");
      if (btn.tagName === "BUTTON") {
        btn.setAttribute("data-orig-text", btn.innerHTML);
        btn.innerHTML = SPINNER_SVG + btn.innerHTML;
      }
      // Fallback re-enable: if nothing navigated after 5s (validation
      // failure from backend, network error), let the user try again.
      setTimeout(function () {
        if (!btn.isConnected) return;
        btn.disabled = false;
        btn.removeAttribute("data-busy");
        var orig = btn.getAttribute("data-orig-text");
        if (orig !== null) {
          btn.innerHTML = orig;
          btn.removeAttribute("data-orig-text");
        }
      }, 5000);
    });
  });

  // -------- flash message auto-dismiss --------
  // Success notices (``role="status"``, blue) fade out after 4s so the
  // user gets the confirmation but isn't left with a permanent banner.
  // Errors (``role="alert"``, red) stay — they usually need action.
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('[role="status"]').forEach(function (el) {
      setTimeout(function () {
        el.style.transition = "opacity 400ms ease";
        el.style.opacity = "0";
        setTimeout(function () {
          el.remove();
        }, 450);
      }, 4000);
    });
  });

  // -------- orders list bulk selection --------
  // Toggles a sticky action bar (``[data-bulk-bar]``) once any row
  // checkbox in the orders list is ticked, and keeps the header
  // "select all" checkbox in sync with the row state (checked when all
  // rows are, indeterminate when some are). CSP-safe — no inline
  // handlers; we rely on data attributes only.
  function syncBulkBar() {
    var form = document.querySelector("[data-bulk-form]");
    if (!form) return;
    var rows = form.querySelectorAll("[data-bulk-row-checkbox]");
    var selected = 0;
    rows.forEach(function (cb) { if (cb.checked) selected += 1; });

    var bar = form.querySelector("[data-bulk-bar]");
    if (bar) {
      if (selected > 0) bar.classList.remove("hidden");
      else bar.classList.add("hidden");
    }
    var count = form.querySelector("[data-bulk-count]");
    if (count) count.textContent = String(selected);

    var master = form.querySelector("[data-bulk-select-all]");
    if (master) {
      master.checked = rows.length > 0 && selected === rows.length;
      master.indeterminate = selected > 0 && selected < rows.length;
    }
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.hasAttribute("data-bulk-select-all")) {
      var form = target.closest("[data-bulk-form]");
      if (!form) return;
      form.querySelectorAll("[data-bulk-row-checkbox]").forEach(function (cb) {
        cb.checked = target.checked;
      });
      syncBulkBar();
      return;
    }
    if (target.hasAttribute("data-bulk-row-checkbox")) {
      syncBulkBar();
    }
  });

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-bulk-clear]");
    if (!btn) return;
    var form = btn.closest("[data-bulk-form]");
    if (!form) return;
    form.querySelectorAll("[data-bulk-row-checkbox]").forEach(function (cb) {
      cb.checked = false;
    });
    syncBulkBar();
  });

  // Initial sync on page load (browser back-forward cache can restore
  // checkbox state — keep the bar consistent with the checkboxes).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncBulkBar);
  } else {
    syncBulkBar();
  }

  // -------- order item product picker --------
  // When a staff/contact picks a product from the dropdown on the order
  // detail page, pre-fill the description, unit, and unit_price inputs.
  // All three remain editable so the user can override per line.
  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLSelectElement)) return;
    if (!target.hasAttribute("data-product-picker")) return;

    var option = target.options[target.selectedIndex];
    var descEl = document.getElementById("add-item-description");
    var unitEl = document.getElementById("add-item-unit");
    var priceEl = document.getElementById("add-item-price");

    var name = option.getAttribute("data-name") || "";
    var unit = option.getAttribute("data-unit") || "";
    var price = option.getAttribute("data-price") || "";

    if (descEl && !descEl.value) descEl.value = name;
    if (unitEl) unitEl.value = unit || "ks";
    if (priceEl && !priceEl.value) priceEl.value = price;
  });
})();
