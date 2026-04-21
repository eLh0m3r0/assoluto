// Small vanilla JS glue for the portal.
// No framework; keeps the single static asset under 2 kB.

(function () {
  "use strict";

  // -------- htmx CSRF header --------
  // HTMX fires ``htmx:configRequest`` on every request just before it goes
  // out; we attach the double-submit token as ``X-CSRF-Token`` so the
  // backend's ``verify_csrf`` dependency accepts it on non-GET requests.
  // The token travels via the ``csrftoken`` cookie (stamped by
  // ``CsrfCookieMiddleware``) — we read it here instead of relying on a
  // template-rendered global, which avoids stale tokens on long-lived pages
  // and keeps base.html free of inline handlers (CSP-safe).
  function readCsrfCookie() {
    var name = "csrftoken=";
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i += 1) {
      var part = parts[i].trim();
      if (part.indexOf(name) === 0) return part.substring(name.length);
    }
    return "";
  }

  document.addEventListener("htmx:configRequest", function (evt) {
    // Safe methods don't need a token; skip to avoid leaking it needlessly.
    var method = (evt.detail && evt.detail.verb ? String(evt.detail.verb) : "").toUpperCase();
    if (method === "GET" || method === "HEAD" || method === "OPTIONS") return;
    var token = readCsrfCookie();
    if (token && evt.detail && evt.detail.headers) {
      evt.detail.headers["X-CSRF-Token"] = token;
    }
  });

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

  // -------- theme toggle (system / light / dark) --------
  // The FOUC-prevention snippet in base.html <head> already applied the
  // correct `dark` class to <html>. Here we wire up the cycling button.
  // Cycle order: system → light → dark → system. Persists `theme` key in
  // localStorage; absence means "system" (follow prefers-color-scheme).
  function readTheme() {
    try {
      var v = localStorage.getItem("theme");
      return v === "light" || v === "dark" ? v : "system";
    } catch (e) {
      return "system";
    }
  }

  function writeTheme(mode) {
    try {
      if (mode === "system") localStorage.removeItem("theme");
      else localStorage.setItem("theme", mode);
    } catch (e) { /* ignore — storage might be blocked */ }
  }

  function applyTheme(mode) {
    var sysDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var wantDark = mode === "dark" || (mode === "system" && sysDark);
    document.documentElement.classList.toggle("dark", wantDark);
  }

  function renderThemeButton(btn, mode) {
    // Toggle icon visibility — exactly one shows.
    var icons = btn.querySelectorAll("[data-theme-icon]");
    icons.forEach(function (el) {
      el.classList.toggle("hidden", el.getAttribute("data-theme-icon") !== mode);
    });
    // Update the sr-only label and the accessible name.
    var labelKey = "data-label-" + mode;
    var label = btn.getAttribute(labelKey) || btn.getAttribute("aria-label") || "";
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
    var srLabel = btn.querySelector("[data-theme-label]");
    if (srLabel) srLabel.textContent = label;
  }

  function initThemeToggle() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var current = readTheme();
    renderThemeButton(btn, current);

    btn.addEventListener("click", function () {
      var order = ["system", "light", "dark"];
      var next = order[(order.indexOf(readTheme()) + 1) % order.length];
      writeTheme(next);
      applyTheme(next);
      renderThemeButton(btn, next);
    });

    // If the user is in "system" mode, track live OS preference changes.
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () {
        if (readTheme() === "system") applyTheme("system");
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange); // Safari < 14
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initThemeToggle);
  } else {
    initThemeToggle();
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
