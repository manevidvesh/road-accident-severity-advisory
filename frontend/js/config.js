/*
 * Application API Configuration
 * 
 * - If running locally (localhost / 127.0.0.1) or directly from Render, uses same-origin ("").
 * - If hosted on a separate frontend platform (Vercel, GitHub Pages, Netlify), automatically
 *   routes API requests to the deployed Render backend.
 */
(function() {
  var host = window.location.hostname;
  var isSameOriginHost = (host === "localhost" || host === "127.0.0.1" || host.endsWith("onrender.com"));

  window.APP_CONFIG = {
    apiBase: isSameOriginHost ? "" : "https://road-accident-severity-advisory.onrender.com"
  };
})();
