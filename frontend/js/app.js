/*
 * Incident severity advisory — vanilla JS, no dependencies.
 *
 * Flow:  Incident (location + date/time)  ->  Review (road features + conditions)  ->  Result
 *
 * Backend (FastAPI):
 *   POST /auto-populate  { latitude, longitude, timestamp }   -> environmental values + provenance
 *   POST /predict        { month, hour, temperature, ... }    -> severity + probabilities + SOP text
 *   GET  /health         (used once, to wake a sleeping host)
 *
 * Rules:
 *   - Never show a value the backend did not provide. Missing values stay missing.
 *   - Every environmental value carries a status: retrieved | imputed | unavailable | edited.
 *   - All text from the network is written with textContent (never innerHTML).
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* Config                                                              */
  /* ------------------------------------------------------------------ */

  function resolveApiBase() {
    var base = (window.APP_CONFIG && window.APP_CONFIG.apiBase) || "";
    // Allow ?api=http://127.0.0.1:8000 for local development only.
    var override = new URLSearchParams(window.location.search).get("api");
    if (override) {
      try {
        var u = new URL(override);
        if (u.hostname === "localhost" || u.hostname === "127.0.0.1") base = u.origin;
      } catch (e) { /* ignore a malformed override */ }
    }
    return base.replace(/\/+$/, "");
  }

  var API_BASE = resolveApiBase();
  var SLOW_HINT_MS = 6000;       // show the "server may be waking up" hint after this
  var REQUEST_TIMEOUT_MS = 80000; // free hosts can take ~1 minute to wake
  var GEOCODE_MIN_GAP_MS = 1100;  // Nominatim usage policy: at most 1 request per second
  var LEGACY_WINDOW_MS = 6 * 24 * 3600 * 1000; // old backend only had ~7 days of data

  // Backend validation ranges (from the IncidentReport model in main.py).
  var ENV_FIELDS = [
    { key: "temperature",   label: "Temperature",   unit: "°F",  min: -80, max: 140, step: "0.1",
      fmt: function (v) { return round(v, 1) + " °F (" + round((v - 32) * 5 / 9, 1) + " °C)"; } },
    { key: "humidity",      label: "Humidity",      unit: "%",   min: 1,   max: 100, step: "1",
      fmt: function (v) { return round(v, 0) + " %"; } },
    { key: "visibility",    label: "Visibility",    unit: "mi",  min: 0,   max: 50,  step: "0.1",
      fmt: function (v) { return round(v, 1) + " mi (" + round(v * 1.60934, 1) + " km)"; } },
    { key: "wind_speed",    label: "Wind speed",    unit: "mph", min: 0,   max: 150, step: "0.1",
      fmt: function (v) { return round(v, 1) + " mph (" + round(v * 1.60934, 1) + " km/h)"; } },
    { key: "precipitation", label: "Precipitation", unit: "in",  min: 0,   max: 15,  step: "0.01",
      fmt: function (v) { return round(v, 2) + " in (" + round(v * 25.4, 1) + " mm)"; } }
  ];
  var CATEGORICAL = [
    { key: "weather_condition", label: "Weather" },
    { key: "sunrise_sunset",    label: "Day / Night" }
  ];
  var SOLAR_OPTIONS = ["Day", "Night", "Unknown"];
  var KNOWN_WEATHER = new Set((window.MODEL_WEATHER_CONDITIONS || []).concat(["Unknown"]));

  var STATUS_TEXT = {
    retrieved:   "Retrieved",
    imputed:     "Imputed",
    unavailable: "Unavailable",
    edited:      "Edited"
  };

  /* ------------------------------------------------------------------ */
  /* State                                                               */
  /* ------------------------------------------------------------------ */

  var state = freshState();

  function freshState() {
    return {
      loc: null,            // { lat, lon, label, source }
      date: "", time: "",
      env: null,            // key -> { value, status, source, original, invalid }
      meta: null,           // { timezone, messages, legacy, overall }
      retrievedKey: null,   // lat|lon|date|time of the data in `env`
      road: { crossing: null, junction: null, traffic_signal: null },
      result: null,
      retrieving: false,
      running: false,
      lastGeocodeAt: 0,
      searching: false
    };
  }

  /* ------------------------------------------------------------------ */
  /* Small helpers                                                       */
  /* ------------------------------------------------------------------ */

  function $(id) { return document.getElementById(id); }
  function round(n, d) { var f = Math.pow(10, d); return Math.round(n * f) / f; }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function show(el, on) { el.hidden = !on; }

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) { node.setAttribute(k, opts.attrs[k]); });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function setMessage(node, kind, parts) {
    // parts: array of strings, or DOM nodes; each string becomes its own paragraph
    node.replaceChildren();
    node.dataset.kind = kind;
    (parts || []).forEach(function (p) {
      if (typeof p === "string") node.appendChild(el("p", { text: p }));
      else node.appendChild(p);
    });
    show(node, parts && parts.length > 0);
  }

  function clearMessage(node) { node.replaceChildren(); show(node, false); }

  function setError(inputEl, errEl, text) {
    if (text) {
      errEl.textContent = text; show(errEl, true);
      if (inputEl) inputEl.setAttribute("aria-invalid", "true");
    } else {
      errEl.textContent = ""; show(errEl, false);
      if (inputEl) inputEl.removeAttribute("aria-invalid");
    }
  }

  function linkButton(text, onClick) {
    var b = el("button", { className: "link", text: text, attrs: { type: "button" } });
    b.addEventListener("click", onClick);
    return b;
  }

  function fmtCoord(n) { return n.toFixed(4); }

  /* ------------------------------------------------------------------ */
  /* Network layer                                                       */
  /* ------------------------------------------------------------------ */

  function ApiError(kind, message, status, detail) {
    this.kind = kind;       // network | timeout | http | invalid
    this.message = message || "";
    this.status = status || 0;
    this.detail = detail;
  }

  function api(path, options, timeoutMs) {
    options = options || {};
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || REQUEST_TIMEOUT_MS);
    var init = { method: options.method || "GET", signal: ctrl.signal };
    if (options.body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(options.body);
    }
    return fetch(API_BASE + path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null, parsed = true;
        try { data = text ? JSON.parse(text) : null; } catch (e) { parsed = false; }
        if (!res.ok) throw new ApiError("http", "", res.status, parsed && data ? data.detail : null);
        if (!parsed || data === null || typeof data !== "object") throw new ApiError("invalid", "Response was not valid JSON.");
        return data;
      });
    }, function (err) {
      throw new ApiError(err && err.name === "AbortError" ? "timeout" : "network");
    }).then(function (data) { clearTimeout(timer); return data; },
            function (err) { clearTimeout(timer); throw err; });
  }

  function describeError(err, what) {
    if (!(err instanceof ApiError)) return "Something unexpected went wrong. Please try again.";
    switch (err.kind) {
      case "network":
        return "Can’t reach the server. If it has been idle it may still be starting up, which can take up to a minute. Check your connection and try again.";
      case "timeout":
        return "The server took too long to respond. Try again in a moment.";
      case "invalid":
        return "The server sent a response this app doesn’t recognise, so nothing was shown. Try again; if it keeps happening the server needs attention.";
      case "http":
        if (err.status === 422 && Array.isArray(err.detail)) {
          var bits = err.detail.map(function (d) {
            var loc = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : "input";
            return String(loc).replace(/_/g, " ") + ": " + (d.msg || "invalid");
          });
          return "The server rejected the " + what + " (" + bits.join("; ") + ").";
        }
        if (err.status === 422 && typeof err.detail === "string") return "The server rejected the " + what + ": " + err.detail;
        return "The server returned an error (" + err.status + "). Try again shortly.";
    }
    return "Something went wrong. Please try again.";
  }

  /* ------------------------------------------------------------------ */
  /* Navigation                                                          */
  /* ------------------------------------------------------------------ */

  var PANELS = { incident: "panel-incident", review: "panel-review", result: "panel-result" };
  var HEADINGS = { incident: "h-incident", review: "h-review", result: "h-result" };

  function go(step) {
    Object.keys(PANELS).forEach(function (k) {
      show($(PANELS[k]), k === step);
      var li = $("step-" + k);
      if (li) {
        if (k === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      }
    });
    window.scrollTo(0, 0);
    if ($(HEADINGS[step])) $(HEADINGS[step]).focus({ preventScroll: true });
  }

  /* ------------------------------------------------------------------ */
  /* Step 1 — location                                                   */
  /* ------------------------------------------------------------------ */

  function setLocation(loc) {
    state.loc = loc;
    $("chosen-label").textContent = loc.label;
    $("chosen-coords").textContent = fmtCoord(loc.lat) + ", " + fmtCoord(loc.lon);
    $("lat").value = fmtCoord(loc.lat);
    $("lon").value = fmtCoord(loc.lon);
    $("osm-link").href = "https://www.openstreetmap.org/?mlat=" + loc.lat + "&mlon=" + loc.lon +
      "#map=16/" + loc.lat + "/" + loc.lon;
    show($("chosen"), true);
    setError($("place"), $("place-error"), "");
  }

  function showManualCoordinateEntry() {
    // Used when search is unavailable: let the operator type coordinates instead.
    $("chosen-label").textContent = "No location selected";
    $("chosen-coords").textContent = "Enter coordinates below";
    show($("chosen"), true);
    var details = document.querySelector("#chosen details");
    if (details) details.open = true;
  }

  function throttleGeocode() {
    var wait = state.lastGeocodeAt + GEOCODE_MIN_GAP_MS - Date.now();
    state.lastGeocodeAt = Date.now() + Math.max(wait, 0);
    return new Promise(function (resolve) { setTimeout(resolve, Math.max(wait, 0)); });
  }

  function searchPlaces() {
    if (state.searching) return;
    var q = $("place").value.trim();
    var status = $("place-status");
    var list = $("place-results");
    list.replaceChildren(); show(list, false);
    if (q.length < 3) { setError($("place"), $("place-error"), "Enter at least 3 characters to search."); return; }
    setError($("place"), $("place-error"), "");
    state.searching = true;
    $("search-btn").disabled = true;
    status.textContent = "Searching…"; show(status, true);

    throttleGeocode().then(function () {
      var url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&q=" + encodeURIComponent(q);
      return fetch(url, { headers: { "Accept": "application/json" } });
    }).then(function (res) {
      if (!res.ok) throw new Error("geocoder " + res.status);
      return res.json();
    }).then(function (rows) {
      if (!Array.isArray(rows)) throw new Error("geocoder shape");
      var results = rows.filter(function (r) { return r && isFinite(parseFloat(r.lat)) && isFinite(parseFloat(r.lon)) && r.display_name; });
      show(status, false);
      if (results.length === 0) {
        setError($("place"), $("place-error"), "No matching place found. Try adding the town or city, or a nearby landmark.");
        return;
      }
      results.forEach(function (r) {
        var b = el("button", { text: r.display_name, attrs: { type: "button" } });
        b.addEventListener("click", function () {
          setLocation({ lat: parseFloat(r.lat), lon: parseFloat(r.lon), label: r.display_name, source: "search" });
          list.replaceChildren(); show(list, false);
        });
        list.appendChild(el("li", {}, [b]));
      });
      show(list, true);
    }).catch(function () {
      show(status, false);
      setError($("place"), $("place-error"),
        "Location search isn’t available right now. You can enter coordinates instead.");
      showManualCoordinateEntry();
    }).then(function () {
      state.searching = false;
      $("search-btn").disabled = false;
    });
  }

  function useDeviceLocation() {
    var status = $("place-status");
    setError($("place"), $("place-error"), "");
    if (!navigator.geolocation) {
      setError($("place"), $("place-error"), "This browser can’t provide a device location. Search for the address instead.");
      return;
    }
    status.textContent = "Waiting for device location…"; show(status, true);
    navigator.geolocation.getCurrentPosition(function (pos) {
      var lat = pos.coords.latitude, lon = pos.coords.longitude;
      show(status, false);
      setLocation({ lat: lat, lon: lon, label: "Device location", source: "device" });
      reverseGeocode(lat, lon);
    }, function (err) {
      show(status, false);
      var text;
      if (err.code === 1) text = "Location permission was denied. Search for the address instead, or allow location access for this site in your browser settings.";
      else if (err.code === 3) text = "Getting the device location timed out. Search for the address instead.";
      else text = "The device location isn’t available right now. Search for the address instead.";
      setError($("place"), $("place-error"), text);
    }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 });
  }

  function reverseGeocode(lat, lon) {
    // Best effort: only improves the label. Failure leaves "Device location".
    throttleGeocode().then(function () {
      return fetch("https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=17&lat=" + lat + "&lon=" + lon);
    }).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (d && d.display_name && state.loc && state.loc.source === "device" && state.loc.lat === lat) {
        state.loc.label = d.display_name;
        $("chosen-label").textContent = d.display_name;
      }
    }).catch(function () { /* keep the plain label */ });
  }

  function applyCoordinates() {
    var lat = parseFloat($("lat").value), lon = parseFloat($("lon").value);
    var err = $("coords-error");
    if (!isFinite(lat) || lat < -90 || lat > 90) { err.textContent = "Latitude must be between -90 and 90."; show(err, true); return; }
    if (!isFinite(lon) || lon < -180 || lon > 180) { err.textContent = "Longitude must be between -180 and 180."; show(err, true); return; }
    show(err, false);
    var label = state.loc && state.loc.source !== "manual" ? state.loc.label + " (position adjusted)" : "Custom position";
    setLocation({ lat: lat, lon: lon, label: label, source: "manual" });
  }

  /* ------------------------------------------------------------------ */
  /* Step 1 — validation and retrieval                                   */
  /* ------------------------------------------------------------------ */

  function validateIncident() {
    var ok = true;
    state.date = $("date").value;
    state.time = ($("time").value || "").slice(0, 5);

    if (!state.loc) {
      setError($("place"), $("place-error"), "Search for a location or use the device location.");
      ok = false;
    }
    var dateOk = /^\d{4}-\d{2}-\d{2}$/.test(state.date) && parseInt(state.date.slice(0, 4), 10) >= 2000;
    setError($("date"), $("date-error"), dateOk ? "" : "Enter the incident date.");
    var timeOk = /^\d{2}:\d{2}$/.test(state.time);
    setError($("time"), $("time-error"), timeOk ? "" : "Enter the incident time.");
    if (!dateOk || !timeOk) ok = false;

    if (!ok) {
      var first = document.querySelector("#incident-form [aria-invalid='true'], #incident-form .error:not([hidden])");
      if (first && first.focus) first.focus();
    }
    return ok;
  }

  function incidentKey() {
    return [state.loc.lat.toFixed(4), state.loc.lon.toFixed(4), state.date, state.time].join("|");
  }

  function retrievalRequest() {
    return {
      latitude: round(state.loc.lat, 4),
      longitude: round(state.loc.lon, 4),
      timestamp: state.date + "T" + state.time + ":00"   // local time at the incident; backend resolves the zone
    };
  }

  function onGetConditions(ev) {
    ev.preventDefault();
    if (state.retrieving) return;
    if (!validateIncident()) return;
    clearMessage($("retrieve-msg"));

    // Same incident as the data we already hold: keep it, including any manual edits.
    if (state.env && state.retrievedKey === incidentKey()) { enterReview(); return; }

    runRetrieval($("retrieve-msg"), $("get-btn"), { keepEdits: false, inReview: false }).then(function (ok) { if (ok) enterReview(); });
  }

  // Returns a promise<boolean>. Shows its own loading/error state in `msgEl`.
  function runRetrieval(msgEl, buttonEl, opts) {
    var keepEdits = opts.keepEdits;
    state.retrieving = true;
    var idleLabel = buttonEl.textContent;
    buttonEl.disabled = true;
    buttonEl.textContent = "Retrieving conditions…";
    var key = incidentKey();
    var req = retrievalRequest();
    var date = state.date, time = state.time;

    var loading = el("p", {}, [el("span", { className: "spinner", attrs: { "aria-hidden": "true" } }),
                               document.createTextNode("Retrieving weather for the incident time…")]);
    setMessage(msgEl, "loading", [loading]);
    var slowTimer = setTimeout(function () {
      msgEl.appendChild(el("p", { text: "Still working. If the server was idle it can take up to a minute to wake up." }));
    }, SLOW_HINT_MS);

    return api("/auto-populate", { method: "POST", body: req }).then(function (data) {
      var norm = normaliseRetrieval(data, date, time);
      if (keepEdits && state.env) mergeEdits(state.env, norm.env);
      state.env = norm.env;
      state.meta = norm.meta;
      state.retrievedKey = key;
      clearMessage(msgEl);
      return true;
    }).catch(function (err) {
      var parts = [describeError(err, "incident details")];
      if (err instanceof ApiError && err.kind === "http" && err.status === 422) {
        parts.push("Check the location and the date/time, then try again.");
      } else if (opts.inReview) {
        parts.push(linkButton("Try again", function () {
          runRetrieval(msgEl, buttonEl, opts).then(function (ok) { if (ok) enterReview(); });
        }));
      } else {
        parts.push(linkButton("Continue without weather data", function () {
          startManualEnvironment();
          enterReview();
        }));
      }
      setMessage(msgEl, "error", parts);
      return false;
    }).then(function (ok) {
      clearTimeout(slowTimer);
      state.retrieving = false;
      buttonEl.disabled = false;
      buttonEl.textContent = idleLabel;
      return ok;
    });
  }

  function startManualEnvironment() {
    // Retrieval failed or was skipped: every value is unavailable, nothing is invented.
    state.env = {};
    ENV_FIELDS.forEach(function (f) { state.env[f.key] = makeItem(null, "unavailable", ""); });
    CATEGORICAL.forEach(function (c) { state.env[c.key] = makeItem("Unknown", "unavailable", ""); });
    state.meta = { timezone: "", messages: ["Weather could not be retrieved. Values are unavailable unless you enter them under Advanced manual input."],
                   legacy: false, overall: "unavailable" };
    state.retrievedKey = null;
  }

  function makeItem(value, status, source) {
    return { value: value, status: status, source: source || "",
             original: { value: value, status: status, source: source || "" }, invalid: "" };
  }

  function mergeEdits(oldEnv, newEnv) {
    Object.keys(newEnv).forEach(function (k) {
      var o = oldEnv[k];
      if (o && o.status === "edited") {
        newEnv[k] = { value: o.value, status: "edited", source: newEnv[k].source,
                      original: { value: newEnv[k].value, status: newEnv[k].status, source: newEnv[k].source },
                      invalid: o.invalid };
      }
    });
  }

  function normaliseRetrieval(data, date, time) {
    var p = data.prefilled_incident_report;
    if (!p || typeof p !== "object") throw new ApiError("invalid", "Missing prefilled_incident_report.");
    var prov = data.provenance && typeof data.provenance === "object" ? data.provenance : {};
    var sources = prov.sources && typeof prov.sources === "object" ? prov.sources : {};

    // Month/hour must match what the operator entered (local incident time).
    var wantMonth = parseInt(date.slice(5, 7), 10), wantHour = parseInt(time.slice(0, 2), 10);
    if (p.month !== wantMonth || p.hour !== wantHour) {
      throw new ApiError("invalid", "Server resolved a different local time than entered.");
    }

    var messages = Array.isArray(data.messages) ? data.messages.filter(function (m) { return typeof m === "string"; }) : [];
    var status = (data.field_status && typeof data.field_status === "object") ? data.field_status : null;
    var legacy = false, hideWeather = false;

    if (!status) {
      legacy = true;
      status = {};
      var metricsSrc = String(sources.environmental_metrics || "");
      var solarSrc = String(sources.solar_phase || "");
      var resolved = Date.parse(prov.resolved_timestamp || "");
      var outside = isFinite(resolved) && Math.abs(resolved - Date.now()) > LEGACY_WINDOW_MS;
      var offline = /^Fallback/i.test(metricsSrc);
      hideWeather = outside;
      ENV_FIELDS.forEach(function (f) { status[f.key] = outside ? "unavailable" : (offline ? "imputed" : "retrieved"); });
      status.weather_condition = (outside || offline) ? "unavailable" : "retrieved";
      status.sunrise_sunset = (!outside && /^Calculated \(Daily Solar/i.test(solarSrc)) ? "retrieved" : "unavailable";
      if (outside) messages.push("This server can only retrieve weather for roughly the past week, so values for this date are not shown. Enter conditions manually if you have them.");
      else if (offline) messages.push("The weather service could not be reached; values are placeholders, not observations.");
      else messages.push("This server version does not report which values were imputed, so every value is shown as retrieved.");
    }

    var env = {};
    ENV_FIELDS.forEach(function (f) {
      var s = status[f.key], v = p[f.key], src = sources.environmental_metrics || "";
      if (hideWeather || s === "unavailable") env[f.key] = makeItem(null, "unavailable", "");
      else if (s === "retrieved" && isNum(v)) env[f.key] = makeItem(v, "retrieved", src);
      else if (s === "imputed") env[f.key] = makeItem(isNum(v) ? v : null, "imputed", "");
      else env[f.key] = makeItem(null, "unavailable", "");
    });
    var w = p.weather_condition, sp = p.sunrise_sunset;
    env.weather_condition = (status.weather_condition === "retrieved" && typeof w === "string" && w && !hideWeather)
      ? makeItem(w, "retrieved", sources.weather_condition || "") : makeItem("Unknown", "unavailable", "");
    env.sunrise_sunset = (status.sunrise_sunset === "retrieved" && SOLAR_OPTIONS.indexOf(sp) !== -1 && sp !== "Unknown")
      ? makeItem(sp, "retrieved", sources.solar_phase || "") : makeItem("Unknown", "unavailable", "");

    var all = ENV_FIELDS.map(function (f) { return f.key; }).concat(CATEGORICAL.map(function (c) { return c.key; }));
    var got = all.filter(function (k) { return env[k].status === "retrieved"; }).length;
    var overall = got === all.length ? "complete" : (got === 0 ? "none" : "partial");

    return { env: env, meta: { timezone: String(prov.resolved_timezone || ""), messages: messages, legacy: legacy, overall: overall } };
  }

  /* ------------------------------------------------------------------ */
  /* Step 2–4 — review                                                   */
  /* ------------------------------------------------------------------ */

  function enterReview() {
    renderSummary();
    renderBanner();
    buildAdvanced();
    renderEnv();
    clearMessage($("run-msg"));
    syncRoadFromState();
    go("review");
  }

  function renderSummary() {
    $("sum-place").textContent = state.loc.label;
    var y = +state.date.slice(0, 4), m = +state.date.slice(5, 7), d = +state.date.slice(8, 10);
    var nice = new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB",
      { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
    var tz = state.meta && state.meta.timezone ? " · " + state.meta.timezone.replace(/_/g, " ") : "";
    $("sum-when").textContent = nice + ", " + state.time + " local time" + tz;
  }

  function renderBanner() {
    var b = $("retrieve-banner");
    var meta = state.meta;
    if (!meta || (meta.overall === "complete" && meta.messages.length === 0)) { clearMessage(b); return; }
    var kind = meta.overall === "none" ? "none" : "partial";
    var lead = meta.overall === "none" ? "No weather conditions could be retrieved."
      : (meta.overall === "partial" ? "Some conditions could not be retrieved." : "Conditions retrieved with notes.");
    var parts = [lead].concat(meta.messages);
    if (meta.overall !== "complete") {
      parts.push(linkButton("Try retrieving again", function () {
        runRetrieval($("retrieve-banner"), document.createElement("button"), { keepEdits: true, inReview: true })
          .then(function (ok) { if (ok) enterReview(); });
      }));
    }
    setMessage(b, kind === "none" ? "none" : "partial", parts);
    b.dataset.kind = kind;
  }

  function statusLine(item) {
    var line = el("div", { className: "env-status", attrs: { "data-status": item.status } });
    line.appendChild(el("span", { className: "tag", text: STATUS_TEXT[item.status] }));
    var extra = "";
    if (item.status === "retrieved") extra = item.source;
    else if (item.status === "imputed") extra = "typical training-set value, not retrieved";
    else if (item.status === "unavailable") extra = "will be treated as missing";
    else if (item.status === "edited") extra = "was " + STATUS_TEXT[item.original.status].toLowerCase();
    if (extra) line.appendChild(document.createTextNode(" · " + extra));
    return line;
  }

  function renderEnv() {
    var dl = $("env-list");
    dl.replaceChildren();

    ENV_FIELDS.forEach(function (f) {
      var item = state.env[f.key];
      var valueText = isNum(item.value) ? f.fmt(item.value) : (item.status === "edited" ? "Left blank" : "—");
      addEnvRow(dl, f.label, valueText, item, f.key, item.invalid);
    });
    CATEGORICAL.forEach(function (c) {
      var item = state.env[c.key];
      var flag = "";
      if (c.key === "weather_condition" && item.value !== "Unknown" && !KNOWN_WEATHER.has(item.value)) {
        flag = "Not a category the model knows, so it will be treated as Unknown.";
      }
      addEnvRow(dl, c.label, item.value, item, c.key, flag);
    });

    var substituted = substitutedCount();
    $("env-note").textContent = substituted
      ? "Values that are imputed, unavailable or left blank are sent as missing; the model fills them with typical training-set values (or Unknown for categories). Weather data is an estimate for the nearest grid point, not a measurement at the scene."
      : "Weather data is an estimate for the nearest grid point at the incident hour, not a measurement at the scene.";
  }

  function addEnvRow(dl, label, valueText, item, key, flagText) {
    var dd = el("dd", {}, [el("div", { className: "env-value", text: valueText }), statusLine(item)]);
    if (flagText) dd.appendChild(el("div", { className: "env-flag", text: flagText }));
    if (item.status === "unavailable" || item.status === "imputed") {
      var b = linkButton("Enter manually", function () { openAdvancedFor(key); });
      b.style.fontSize = "0.8125rem";
      dd.appendChild(b);
    }
    var row = el("div", { className: "env-row" }, [el("dt", { text: label }), dd]);
    dl.appendChild(row);
  }

  function substitutedCount() {
    var n = 0;
    Object.keys(state.env).forEach(function (k) {
      var it = state.env[k];
      var isNumeric = ENV_FIELDS.some(function (f) { return f.key === k; });
      if (isNumeric) { if (!isNum(sentNumeric(it))) n++; }
      else if (it.value === "Unknown" || !KNOWN_WEATHER.has(it.value) && k === "weather_condition") n++;
    });
    return n;
  }

  function sentNumeric(item) {
    return (item.status === "retrieved" || item.status === "edited") && isNum(item.value) ? item.value : null;
  }

  /* ---- Advanced manual input ---- */

  function buildAdvanced() {
    var host = $("advanced-fields");
    host.replaceChildren();

    ENV_FIELDS.forEach(function (f) {
      var item = state.env[f.key];
      var id = "adv-" + f.key;
      var input = el("input", { attrs: { id: id, type: "number", step: f.step, min: String(f.min), max: String(f.max), inputmode: "decimal" } });
      var baseline = item.original.status === "retrieved" ? item.original.value : null;
      input.value = item.status === "edited" ? (isNum(item.value) ? item.value : "") : (isNum(baseline) ? baseline : "");
      if (!input.value && isNum(item.original.value)) input.placeholder = "typical " + item.original.value;
      var meta = el("div", { className: "adv-meta", attrs: { id: id + "-meta", "aria-live": "polite" } });
      var label = el("label", { attrs: { "for": id } }, [document.createTextNode(f.label + " "), el("span", { className: "unit", text: "(" + f.unit + ")" })]);
      host.appendChild(el("div", { className: "field adv-field" }, [label, input, meta]));
      input.addEventListener("input", function () { onNumericEdit(f, input); });
      updateAdvMeta(f.key);
    });

    // Weather condition
    var wSel = el("select", { attrs: { id: "adv-weather_condition" } });
    var current = state.env.weather_condition;
    var opts = ["Unknown"].concat(window.MODEL_WEATHER_CONDITIONS || []);
    var origValue = current.original.value;
    if (opts.indexOf(origValue) === -1) opts.push(origValue);
    opts.forEach(function (o) {
      var label = o + (KNOWN_WEATHER.has(o) ? "" : " (not a model category)");
      var op = el("option", { text: label, attrs: { value: o } });
      wSel.appendChild(op);
    });
    wSel.value = current.value;
    wSel.addEventListener("change", function () { onCategoricalEdit("weather_condition", wSel.value); });
    host.appendChild(el("div", { className: "field adv-field" }, [
      el("label", { text: "Weather", attrs: { "for": "adv-weather_condition" } }), wSel,
      el("div", { className: "adv-meta", attrs: { id: "adv-weather_condition-meta", "aria-live": "polite" } })]));

    // Day / Night
    var sSel = el("select", { attrs: { id: "adv-sunrise_sunset" } });
    SOLAR_OPTIONS.forEach(function (o) { sSel.appendChild(el("option", { text: o, attrs: { value: o } })); });
    sSel.value = state.env.sunrise_sunset.value;
    sSel.addEventListener("change", function () { onCategoricalEdit("sunrise_sunset", sSel.value); });
    host.appendChild(el("div", { className: "field adv-field" }, [
      el("label", { text: "Day / Night", attrs: { "for": "adv-sunrise_sunset" } }), sSel,
      el("div", { className: "adv-meta", attrs: { id: "adv-sunrise_sunset-meta", "aria-live": "polite" } })]));
    updateAdvMeta("weather_condition");
    updateAdvMeta("sunrise_sunset");
  }

  function numericProblem(f, item) {
    if (!isNum(item.value)) return "";
    if (item.value < f.min || item.value > f.max) {
      return "Must be between " + f.min + " and " + f.max + " " + f.unit + " for the model.";
    }
    return "";
  }

  function onNumericEdit(f, input) {
    var item = state.env[f.key];
    var raw = input.value.trim();
    var baseline = item.original.status === "retrieved" ? item.original.value : null;
    var val = raw === "" ? null : Number(raw);
    if (raw !== "" && !isFinite(val)) val = NaN;

    var same = (val === null && baseline === null) || (isNum(val) && isNum(baseline) && val === baseline);
    if (same) {
      item.value = item.original.value; item.status = item.original.status; item.source = item.original.source; item.invalid = "";
    } else {
      item.status = "edited";
      item.value = isNaN(val) ? null : val;
      item.invalid = isNaN(val) ? "Enter a number." : numericProblem(f, item);
    }
    input.setAttribute("aria-invalid", item.invalid ? "true" : "false");
    if (!item.invalid) input.removeAttribute("aria-invalid");
    updateAdvMeta(f.key);
    renderEnv();
  }

  function onCategoricalEdit(key, value) {
    var item = state.env[key];
    if (value === item.original.value) {
      item.value = item.original.value; item.status = item.original.status; item.source = item.original.source;
    } else {
      item.value = value; item.status = "edited";
    }
    updateAdvMeta(key);
    renderEnv();
  }

  function updateAdvMeta(key) {
    var meta = $("adv-" + key + "-meta");
    if (!meta) return;
    var item = state.env[key];
    meta.replaceChildren();
    if (item.invalid) { meta.appendChild(el("span", { className: "error", text: item.invalid })); return; }
    if (item.status === "edited") {
      meta.appendChild(el("span", { className: "tag", text: "Edited" }));
      meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(linkButton("Reset", function () { resetField(key); }));
    }
  }

  function resetField(key) {
    var item = state.env[key];
    item.value = item.original.value; item.status = item.original.status; item.source = item.original.source; item.invalid = "";
    var input = $("adv-" + key);
    if (input.tagName === "SELECT") input.value = item.value;
    else {
      var baseline = item.original.status === "retrieved" ? item.original.value : null;
      input.value = isNum(baseline) ? baseline : "";
      input.removeAttribute("aria-invalid");
    }
    updateAdvMeta(key);
    renderEnv();
  }

  function openAdvancedFor(key) {
    var d = $("advanced");
    d.open = true;
    var input = $("adv-" + key);
    if (input) { input.focus(); input.scrollIntoView({ block: "center" }); }
  }

  /* ---- Road features ---- */

  function syncRoadFromState() {
    ["crossing", "junction", "traffic_signal"].forEach(function (k) {
      var v = state.road[k];
      var yes = document.querySelector("input[name='" + k + "'][value='yes']");
      var no = document.querySelector("input[name='" + k + "'][value='no']");
      if (yes) yes.checked = v === true;
      if (no) no.checked = v === false;
    });
  }

  function onRoadChange(ev) {
    var t = ev.target;
    if (!t || t.type !== "radio") return;
    state.road[t.name] = t.value === "yes";
    var fs = document.querySelector(".q[data-key='" + t.name + "']");
    if (fs) {
      fs.removeAttribute("data-invalid");
      var err = fs.querySelector(".error");
      if (err) show(err, false);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Step 5 — run assessment                                             */
  /* ------------------------------------------------------------------ */

  function validateForRun() {
    var problems = [];
    var firstFocus = null;

    ["crossing", "junction", "traffic_signal"].forEach(function (k) {
      var fs = document.querySelector(".q[data-key='" + k + "']");
      if (state.road[k] === null && fs) {
        fs.dataset.invalid = "true";
        var err = fs.querySelector(".error");
        if (err) show(err, true);
        if (!firstFocus) firstFocus = fs.querySelector("input");
        problems.push("road");
      }
    });

    var envProblem = null;
    ENV_FIELDS.forEach(function (f) {
      var item = state.env[f.key];
      var msg = item.invalid || ((item.status === "retrieved" || item.status === "edited") ? numericProblem(f, item) : "");
      if (msg && !envProblem) {
        envProblem = { key: f.key, text: f.label + ": " + msg +
          (item.status === "retrieved" ? " Correct it, or clear the field, under Advanced manual input." : "") };
      }
    });
    if (envProblem) { problems.push("env"); }

    return { problems: problems, firstFocus: firstFocus, envProblem: envProblem };
  }

  function buildPayload() {
    var payload = {
      month: parseInt(state.date.slice(5, 7), 10),
      hour: parseInt(state.time.slice(0, 2), 10),
      crossing: state.road.crossing,
      junction: state.road.junction,
      traffic_signal: state.road.traffic_signal,
      weather_condition: state.env.weather_condition.value || "Unknown",
      sunrise_sunset: state.env.sunrise_sunset.value || "Unknown"
    };
    ENV_FIELDS.forEach(function (f) { payload[f.key] = sentNumeric(state.env[f.key]); });
    return payload;
  }

  function onRun() {
    if (state.running) return;
    var msg = $("run-msg");
    clearMessage(msg);

    var check = validateForRun();
    if (check.problems.length) {
      var parts = [];
      if (check.problems.indexOf("road") !== -1) parts.push("Answer all three road-feature questions.");
      if (check.envProblem) parts.push(check.envProblem.text);
      setMessage(msg, "error", parts);
      if (check.envProblem && check.problems.indexOf("road") === -1) openAdvancedFor(check.envProblem.key);
      else if (check.firstFocus) check.firstFocus.focus();
      return;
    }

    var btn = $("run-btn");
    state.running = true;
    btn.disabled = true;
    btn.textContent = "Running assessment…";
    setMessage(msg, "loading", [el("p", {}, [el("span", { className: "spinner", attrs: { "aria-hidden": "true" } }),
                                              document.createTextNode("Running the model…")])]);

    api("/predict", { method: "POST", body: buildPayload() }).then(function (data) {
      state.result = validatePrediction(data);
      renderResult();
      clearMessage(msg);
      go("result");
    }).catch(function (err) {
      setMessage(msg, "error", [describeError(err, "assessment request")]);
    }).then(function () {
      state.running = false;
      btn.disabled = false;
      btn.textContent = "Run assessment";
    });
  }

  function validatePrediction(d) {
    var bad = function (why) { return new ApiError("invalid", why); };
    var level = d.predicted_severity_level;
    if (!Number.isInteger(level) || level < 1 || level > 4) throw bad("predicted_severity_level");
    ["impact_tier", "estimated_clearance_time", "dispatch_priority", "recommended_action"].forEach(function (k) {
      if (typeof d[k] !== "string" || !d[k]) throw bad(k);
    });
    var dist = d.confidence_distribution_percent;
    if (!dist || typeof dist !== "object") throw bad("confidence_distribution_percent");
    var probs = [];
    Object.keys(dist).forEach(function (k) {
      var m = /^Severity_(\d+)$/.exec(k);
      if (!m || !isNum(dist[k]) || dist[k] < 0 || dist[k] > 100) throw bad("confidence key " + k);
      probs.push({ level: parseInt(m[1], 10), pct: dist[k] });
    });
    if (probs.length === 0) throw bad("empty distribution");
    probs.sort(function (a, b) { return a.level - b.level; });
    return { level: level, tier: d.impact_tier, clearance: d.estimated_clearance_time,
             priority: d.dispatch_priority, action: d.recommended_action, probs: probs };
  }

  /* ------------------------------------------------------------------ */
  /* Step 6 — result                                                     */
  /* ------------------------------------------------------------------ */

  function renderResult() {
    var r = state.result;
    $("result-hero").dataset.level = String(r.level);
    $("res-level").textContent = r.level;
    $("res-level-sr").textContent = r.level;
    $("res-tier").textContent = r.tier;

    var list = $("res-probs");
    list.replaceChildren();
    var top = Math.max.apply(null, r.probs.map(function (p) { return p.pct; }));
    r.probs.forEach(function (p) {
      var bar = el("div", { className: "bar", attrs: { "aria-hidden": "true" } }, [el("span")]);
      bar.firstChild.style.width = p.pct + "%";
      var li = el("li", {}, [el("span", { text: "Level " + p.level }), bar, el("span", { className: "pct", text: round(p.pct, 1) + " %" })]);
      if (p.pct === top) li.dataset.top = "true";
      list.appendChild(li);
    });

    var sop = $("res-sop");
    sop.replaceChildren();
    [["Clearance window", r.clearance], ["Dispatch priority", r.priority], ["Recommended action", r.action]].forEach(function (pair) {
      sop.appendChild(el("div", {}, [el("dt", { text: pair[0] }), el("dd", { text: pair[1] })]));
    });

    var n = substitutedCount();
    var note = $("res-inputs-note");
    if (n > 0) {
      note.textContent = n + " of 7 conditions were not available as real values and were replaced by typical values or Unknown, so this estimate is less specific to the incident.";
      show(note, true);
    } else { show(note, false); }
  }

  /* ------------------------------------------------------------------ */
  /* Wiring                                                              */
  /* ------------------------------------------------------------------ */

  function setDateTimeDefaults() {
    var now = new Date();
    var yyyy = now.getFullYear();
    var mm = String(now.getMonth() + 1).padStart(2, "0");
    var dd = String(now.getDate()).padStart(2, "0");
    var dateStr = yyyy + "-" + mm + "-" + dd;
    var hh = String(now.getHours()).padStart(2, "0");
    var min = String(now.getMinutes()).padStart(2, "0");
    var timeStr = hh + ":" + min;

    var dateInput = $("date");
    var timeInput = $("time");
    if (dateInput) {
      dateInput.value = dateStr;
      state.date = dateStr;
    }
    if (timeInput) {
      timeInput.value = timeStr;
      state.time = timeStr;
    }
  }

  function newIncident() {
    state = freshState();
    $("incident-form").reset();
    $("place").value = "";
    show($("chosen"), false);
    show($("place-results"), false);
    show($("place-status"), false);
    ["place", "date", "time"].forEach(function (id) { setError($(id), $(id + "-error"), ""); });
    clearMessage($("retrieve-msg"));
    var adv = $("advanced");
    if (adv) adv.open = false;
    setDateTimeDefaults();
    go("incident");
  }

  function init() {
    $("incident-form").addEventListener("submit", onGetConditions);
    $("search-btn").addEventListener("click", searchPlaces);
    $("place").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); searchPlaces(); }
    });
    $("geo-btn").addEventListener("click", useDeviceLocation);
    $("apply-coords").addEventListener("click", applyCoordinates);
    $("edit-incident").addEventListener("click", function () { go("incident"); });
    $("panel-review").addEventListener("change", onRoadChange);
    $("run-btn").addEventListener("click", onRun);
    $("new-btn").addEventListener("click", newIncident);
    $("back-review-btn").addEventListener("click", function () { go("review"); });
    ["date", "time"].forEach(function (id) {
      $(id).addEventListener("input", function () { setError($(id), $(id + "-error"), ""); });
    });

    setDateTimeDefaults();

    // Wake a sleeping host early; failure is silent and changes nothing.
    fetch(API_BASE + "/health").catch(function () {});
  }

  // Exposed only so automated tests can inspect what would be sent; not used by the UI.
  window.__advisoryDebug = { buildPayload: function () { return buildPayload(); }, state: function () { return state; } };

  init();
})();
