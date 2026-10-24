// Marks the download card for the visitor's system. Without JS all three cards look the same.
(function () {
  var p = ((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent).toLowerCase();
  var os = /win/.test(p) ? "windows" : /mac|darwin/.test(p) ? "mac" : /linux|x11/.test(p) && !/android/.test(p) ? "linux" : null;
  if (!os) return;
  var card = document.querySelector('.dl[data-os="' + os + '"]');
  if (card) card.classList.add("is-yours");
})();
