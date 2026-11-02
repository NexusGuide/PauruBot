// Start the Mini App even when telegram.org cannot be reached (REF-03).
//
// Telegram passes the signed launch data in the address (#tgWebAppData=...), so the app can sign in
// without Telegram's SDK. The SDK only adds the native buttons, theme and links. Where only Telegram's
// own proxy works, its script never loads, and as a blocking <script> it held the whole app for as
// long as the request hung. It now loads in the background; the app waits for it a moment at most.
(function () {
  "use strict";
  var initData = "";
  try {
    var hash = String(window.location.hash || "").replace(/^#/, "");
    var params = new URLSearchParams(hash);
    initData = params.get("tgWebAppData") || "";
    if (initData) {
      // Where the SDK itself keeps the launch parameters, so it still finds them if it arrives
      // after the app has already moved to another page (and the address no longer has them).
      var stored = {};
      params.forEach(function (value, key) {
        if (key.indexOf("tgWebApp") === 0) stored[key] = value;
      });
      window.sessionStorage.setItem("__telegram__initParams", JSON.stringify(stored));
    }
  } catch (error) {
    initData = initData || "";
  }
  var settle;
  var sdk = new Promise(function (resolve) {
    settle = resolve;
  });
  var script = document.createElement("script");
  script.src = "https://telegram.org/js/telegram-web-app.js";
  script.async = true;
  script.onload = function () {
    settle(Boolean(window.Telegram && window.Telegram.WebApp));
  };
  script.onerror = function () {
    settle(false);
  };
  document.head.appendChild(script);
  window.__pauruLaunch = { initData: initData, sdk: sdk };
})();
