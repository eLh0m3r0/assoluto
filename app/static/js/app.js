// Small vanilla JS glue for the portal.
// No framework; keeps the single static asset under 2 kB.

(function () {
  "use strict";

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
