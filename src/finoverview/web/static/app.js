// The only JavaScript in this project: refresh the page when the data is likely
// to have changed. Everything else is server-rendered, so the dashboard works
// with JS disabled.
(function () {
  "use strict";
  var MINUTES = 10;

  // Don't refresh while the tab is hidden — pointless work on a Pi.
  var timer = null;

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (!document.hidden) location.reload();
      else schedule();
    }, MINUTES * 60 * 1000);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) schedule();
  });

  schedule();
})();
