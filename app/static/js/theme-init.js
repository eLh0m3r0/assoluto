/*
  No-FOUC theme init. Loaded synchronously (no defer) from <head>
  BEFORE the CSS, so the `dark` class is on <html> at first paint and
  Tailwind's `dark:` variants apply without a flash.

  Tri-state: explicit `light` / `dark` in localStorage wins; missing /
  other values (including the `system` sentinel used by the toggle)
  defer to `prefers-color-scheme`. CSP-safe — this is a `script-src
  'self'` external file, no inline script.
*/
(function () {
  try {
    var pref = localStorage.getItem("theme");
    var sysDark =
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    var wantDark = pref === "dark" || (pref !== "light" && sysDark);
    if (wantDark) document.documentElement.classList.add("dark");
  } catch (e) {
    /* localStorage blocked → fall back to no-dark. */
  }
})();
