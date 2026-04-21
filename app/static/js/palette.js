// Command palette (Cmd+K / Ctrl+K) wiring.
//
// The modal itself and its HTMX-driven input live in _palette.html; all
// this file does is toggle the modal, trap focus inside it, and handle
// keyboard navigation between result links.
//
// CSP-safe: no inline handlers, listeners attach at DOM-ready and live
// on the document. The modal element is `<div id="palette-root">`.

(function () {
  "use strict";

  var rootId = "palette-root";
  var inputId = "palette-input";
  var resultsId = "palette-results";
  var itemSelector = "[data-palette-item]";

  // Remembers which element had focus before the palette opened, so we
  // can return focus on close (matches native dialog/ARIA expectations).
  var previouslyFocused = null;

  function getRoot() {
    return document.getElementById(rootId);
  }

  function isOpen() {
    var root = getRoot();
    return !!root && !root.classList.contains("hidden");
  }

  function items() {
    var results = document.getElementById(resultsId);
    if (!results) return [];
    return Array.prototype.slice.call(results.querySelectorAll(itemSelector));
  }

  function open() {
    var root = getRoot();
    if (!root) return;
    previouslyFocused = document.activeElement;
    // The shell uses `hidden` to toggle; when visible it becomes a flex
    // container (centered above the fold).
    root.classList.remove("hidden");
    root.classList.add("flex");
    var input = document.getElementById(inputId);
    if (input) {
      input.value = "";
      var results = document.getElementById(resultsId);
      if (results) results.innerHTML = "";
      // Timeout lets the browser paint before we steal focus — avoids
      // iOS keyboards missing the first request.
      setTimeout(function () {
        input.focus();
      }, 0);
    }
  }

  function close() {
    var root = getRoot();
    if (!root) return;
    root.classList.add("hidden");
    root.classList.remove("flex");
    if (previouslyFocused && typeof previouslyFocused.focus === "function") {
      try {
        previouslyFocused.focus();
      } catch (_e) {
        // Ignore focus errors on disconnected nodes.
      }
    }
    previouslyFocused = null;
  }

  function toggle() {
    if (isOpen()) close();
    else open();
  }

  function isPaletteShortcut(e) {
    var k = (e.key || "").toLowerCase();
    if (k !== "k") return false;
    // Support both Cmd (macOS) and Ctrl (everywhere else). Require
    // exactly that modifier — plain "k" typed into an input must not
    // trigger.
    return (e.metaKey || e.ctrlKey) && !e.altKey;
  }

  // ---------- global shortcut ----------
  document.addEventListener("keydown", function (e) {
    if (isPaletteShortcut(e)) {
      e.preventDefault();
      toggle();
      return;
    }
    if (!isOpen()) return;

    // Escape closes regardless of which element inside has focus.
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }

    // Arrow navigation between result links.
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      var list = items();
      if (!list.length) return;
      e.preventDefault();
      var active = document.activeElement;
      var idx = list.indexOf(active);
      var next;
      if (e.key === "ArrowDown") {
        next = idx < 0 ? 0 : Math.min(idx + 1, list.length - 1);
      } else {
        next = idx <= 0 ? -1 : idx - 1;
      }
      if (next < 0) {
        var input = document.getElementById(inputId);
        if (input) input.focus();
      } else {
        list[next].focus();
      }
      return;
    }

    // Enter on the input opens the first result.
    if (e.key === "Enter") {
      var input = document.getElementById(inputId);
      if (document.activeElement === input) {
        var list2 = items();
        if (list2.length) {
          e.preventDefault();
          window.location.href = list2[0].getAttribute("href");
        }
      }
      // Enter on a result link uses the native anchor behaviour.
    }
  });

  // ---------- click outside closes ----------
  document.addEventListener("click", function (e) {
    if (!isOpen()) return;
    var root = getRoot();
    if (!root) return;
    // The inner card is the direct child; clicks inside it stay.
    var inner = root.firstElementChild;
    if (!inner) return;
    if (e.target === root) {
      close();
    } else if (!inner.contains(e.target)) {
      close();
    }
  });

  // ---------- focus trap ----------
  // After HTMX swaps new results in, keep focus somewhere useful —
  // otherwise Tab could walk off into the hidden main page.
  document.addEventListener("focusin", function (e) {
    if (!isOpen()) return;
    var root = getRoot();
    if (!root) return;
    if (!root.contains(e.target)) {
      var input = document.getElementById(inputId);
      if (input) input.focus();
    }
  });
})();
