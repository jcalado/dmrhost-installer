// Closes the language menu on Escape or a click outside it. Without JS it still opens and closes on its summary.
(function () {
  var menu = document.querySelector("details.lang");
  if (!menu) return;
  document.addEventListener("click", function (e) {
    if (menu.open && !menu.contains(e.target)) menu.open = false;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && menu.open) {
      menu.open = false;
      menu.querySelector("summary").focus();
    }
  });
})();
