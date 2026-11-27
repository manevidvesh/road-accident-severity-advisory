/*
 * Workspace Operations: Authentication, Role-Based Access, Command Queue CRUD,
 * Incident Lifecycle Updates, and Supervisor Analytics.
 * Vanilla JS, XSS-safe (textContent only for network data).
 */
(function () {
  "use strict";

  var TOKEN_KEY = "toc_access_token";

  function resolveApiBase() {
    var base = (window.APP_CONFIG && window.APP_CONFIG.apiBase) || "";
    var override = new URLSearchParams(window.location.search).get("api");
    if (override) {
      try {
        var u = new URL(override);
        if (u.hostname === "localhost" || u.hostname === "127.0.0.1") base = u.origin;
      } catch (e) {}
    }
    return base.replace(/\/+$/, "");
  }

  var API_BASE = resolveApiBase();

  var ops = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    user: null,
    activeView: "assess",
    incidents: [],
    staff: [],
    selectedIncidentId: null
  };

  function $(id) { return document.getElementById(id); }
  function show(node, on) { if (node) node.hidden = !on; }

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) { node.setAttribute(k, opts.attrs[k]); });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function showMsg(node, kind, text) {
    if (!node) return;
    node.replaceChildren();
    if (!text) { show(node, false); return; }
    node.dataset.kind = kind;
    node.appendChild(el("p", { text: text }));
    show(node, true);
  }

  function apiFetch(path, options) {
    options = options || {};
    var headers = {};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (ops.token) headers["Authorization"] = "Bearer " + ops.token;
    return fetch(API_BASE + path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined
    }).then(function (res) {
      return res.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) {}
        if (!res.ok) {
          var detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : ("Request failed (" + res.status + ")");
          var err = new Error(detail);
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Auth Session & Modal                                                */
  /* ------------------------------------------------------------------ */

  function setToken(token) {
    ops.token = token || "";
    if (ops.token) localStorage.setItem(TOKEN_KEY, ops.token);
    else localStorage.removeItem(TOKEN_KEY);
  }

  function updateAuthUI() {
    var loggedIn = !!ops.user;
    var pill = $("auth-user-pill");
    var openBtn = $("open-auth-btn");
    var outBtn = $("signout-btn");
    var analyticsTab = $("tab-analytics");
    var banner = $("auth-status-banner");

    if (loggedIn) {
      var shortName = String(ops.user.full_name || "").replace(/\s*\([^)]*\)\s*$/, "");
      pill.replaceChildren(
        el("span", { className: "user-pill-name", text: shortName }),
        el("span", { className: "role-tag", text: ops.user.role })
      );
      show(pill, true);
      show(openBtn, false);
      show(outBtn, true);
      show(analyticsTab, ops.user.role === "dispatcher" || ops.user.role === "supervisor");
      show(banner, false);
    } else {
      show(pill, false);
      show(openBtn, true);
      show(outBtn, false);
      show(analyticsTab, false);
      banner.replaceChildren();
      var p = el("p", {}, [
        document.createTextNode("Running in guest preview mode. "),
        (function () {
          var b = el("button", { className: "link", text: "Sign in or register", attrs: { type: "button" } });
          b.addEventListener("click", function () { openAuthModal("login"); });
          return b;
        })(),
        document.createTextNode(" to save assessments to the Command Queue and manage dispatches.")
      ]);
      banner.appendChild(p);
      show(banner, ops.activeView === "assess");
    }
  }

  function openAuthModal(tab) {
    switchAuthTab(tab || "login");
    showMsg($("login-msg"), "error", "");
    showMsg($("reg-msg"), "error", "");
    show($("auth-modal"), true);
    var focusEl = tab === "register" ? $("reg-name") : $("login-email");
    if (focusEl) focusEl.focus();
  }

  function closeAuthModal() {
    show($("auth-modal"), false);
  }

  function switchAuthTab(tab) {
    var isLogin = tab !== "register";
    $("auth-tab-login").setAttribute("aria-selected", isLogin ? "true" : "false");
    $("auth-tab-register").setAttribute("aria-selected", isLogin ? "false" : "true");
    show($("login-form"), isLogin);
    show($("register-form"), !isLogin);
  }

  function handleLogin(ev) {
    ev.preventDefault();
    showMsg($("login-msg"), "error", "");
    var email = $("login-email").value.trim();
    var password = $("login-password").value;
    if (!email || !password) {
      showMsg($("login-msg"), "error", "Enter both email and password.");
      return;
    }
    var btn = $("login-submit");
    btn.disabled = true;
    apiFetch("/api/auth/login", { method: "POST", body: { email: email, password: password } })
      .then(function (data) {
        setToken(data.access_token);
        ops.user = data.user;
        updateAuthUI();
        closeAuthModal();
        if (ops.activeView === "queue") loadQueue();
        if (ops.activeView === "analytics") loadAnalytics();
      })
      .catch(function (err) {
        showMsg($("login-msg"), "error", err.message || "Sign in failed.");
      })
      .then(function () { btn.disabled = false; });
  }

  function handleRegister(ev) {
    ev.preventDefault();
    showMsg($("reg-msg"), "error", "");
    var fullName = $("reg-name").value.trim();
    var email = $("reg-email").value.trim();
    var password = $("reg-password").value;
    var role = $("reg-role").value;
    if (fullName.length < 2 || !email || password.length < 6) {
      showMsg($("reg-msg"), "error", "Enter your name, a valid email, and a password of at least 6 characters.");
      return;
    }
    var btn = $("reg-submit");
    btn.disabled = true;
    apiFetch("/api/auth/register", {
      method: "POST",
      body: { full_name: fullName, email: email, password: password, role: role }
    })
      .then(function (data) {
        setToken(data.access_token);
        ops.user = data.user;
        updateAuthUI();
        closeAuthModal();
        if (ops.activeView === "queue") loadQueue();
      })
      .catch(function (err) {
        showMsg($("reg-msg"), "error", err.message || "Registration failed.");
      })
      .then(function () { btn.disabled = false; });
  }

  function signOut() {
    setToken("");
    ops.user = null;
    ops.incidents = [];
    ops.selectedIncidentId = null;
    updateAuthUI();
    if (ops.activeView === "analytics") switchView("assess");
    else if (ops.activeView === "queue") renderQueueAuthGate();
  }

  function restoreSession() {
    if (!ops.token) { updateAuthUI(); return Promise.resolve(); }
    return apiFetch("/api/auth/me")
      .then(function (user) { ops.user = user; })
      .catch(function () { setToken(""); ops.user = null; })
      .then(function () { updateAuthUI(); });
  }

  /* ------------------------------------------------------------------ */
  /* View Switching                                                      */
  /* ------------------------------------------------------------------ */

  function switchView(view) {
    ops.activeView = view;
    $("tab-assess").setAttribute("aria-pressed", view === "assess" ? "true" : "false");
    $("tab-queue").setAttribute("aria-pressed", view === "queue" ? "true" : "false");
    $("tab-analytics").setAttribute("aria-pressed", view === "analytics" ? "true" : "false");

    show($("view-assess"), view === "assess");
    show($("assess-steps-bar"), view === "assess");
    show($("view-queue"), view === "queue");
    show($("view-analytics"), view === "analytics");
    show($("auth-status-banner"), view === "assess" && !ops.user);

    if (view === "queue") loadQueue();
    if (view === "analytics") loadAnalytics();
  }

  /* ------------------------------------------------------------------ */
  /* Command Queue (List, Filter, Detail, Update, Re-assess, Delete)     */
  /* ------------------------------------------------------------------ */

  function renderQueueAuthGate() {
    var gate = $("queue-auth-gate");
    show($("queue-body"), false);
    show($("queue-detail"), false);
    gate.replaceChildren();
    gate.appendChild(el("p", {}, [
      document.createTextNode("Sign in as a Field Responder, TOC Dispatcher, or Supervisor to view and manage persisted incidents. "),
      (function () {
        var b = el("button", { className: "link", text: "Open sign in", attrs: { type: "button" } });
        b.addEventListener("click", function () { openAuthModal("login"); });
        return b;
      })()
    ]));
    show(gate, true);
  }

  function loadQueue() {
    if (!ops.user) { renderQueueAuthGate(); return; }
    show($("queue-auth-gate"), false);
    show($("queue-detail"), false);
    show($("queue-body"), true);
    showMsg($("queue-msg"), "loading", "Loading incidents…");

    var params = new URLSearchParams();
    var q = $("filter-q").value.trim();
    var st = $("filter-status").value;
    var sev = $("filter-severity").value;
    var mine = $("filter-mine").checked;
    if (q) params.set("q", q);
    if (st) params.set("status", st);
    if (sev) params.set("severity", sev);
    if (mine) params.set("mine_only", "true");
    var qs = params.toString() ? "?" + params.toString() : "";

    Promise.all([
      apiFetch("/api/incidents" + qs),
      apiFetch("/api/users/dispatchers").catch(function () { return []; })
    ])
      .then(function (res) {
        ops.incidents = res[0] || [];
        ops.staff = res[1] || [];
        showMsg($("queue-msg"), "loading", "");
        renderQueueKPIs(ops.incidents);
        renderQueueList(ops.incidents);
      })
      .catch(function (err) {
        showMsg($("queue-msg"), "error", err.message || "Could not load incidents.");
      });
  }

  function renderQueueKPIs(list) {
    var host = $("queue-kpis");
    host.replaceChildren();
    var openCount = list.filter(function (i) { return i.status === "open"; }).length;
    var activeDisp = list.filter(function (i) { return i.status === "dispatched" || i.status === "in_clearance"; }).length;
    var severeCount = list.filter(function (i) {
      return i.latest_assessment && i.latest_assessment.predicted_severity_level >= 3;
    }).length;
    [
      [list.length, "Matching incidents"],
      [openCount, "Open / unassigned"],
      [activeDisp, "In dispatch / clearance"],
      [severeCount, "High / Critical (L3–L4)"]
    ].forEach(function (pair) {
      host.appendChild(el("div", { className: "kpi-card" }, [
        el("div", { className: "kpi-val", text: String(pair[0]) }),
        el("div", { className: "kpi-lbl", text: pair[1] })
      ]));
    });
  }

  function formatStatusLabel(st) {
    return String(st || "open").replace(/_/g, " ");
  }

  function renderQueueList(list) {
    var host = $("queue-list");
    host.replaceChildren();
    if (!list.length) {
      host.appendChild(el("p", { className: "note", text: "No incidents match the current filters. Run an assessment while signed in to log one." }));
      return;
    }
    list.forEach(function (inc) {
      var a = inc.latest_assessment;
      var sev = a ? a.predicted_severity_level : "?";
      var tier = a ? a.impact_tier : "Unassessed";
      var prio = a ? a.dispatch_priority : "—";
      var reporter = inc.reported_by ? inc.reported_by.full_name : "Unknown";
      var assignee = inc.assigned_to ? inc.assigned_to.full_name : "Unassigned";

      var card = el("button", { className: "q-card", attrs: { type: "button" } }, [
        el("div", { className: "q-card-main" }, [
          el("div", { className: "q-card-top" }, [
            el("span", { className: "ref-code", text: inc.reference_code }),
            el("span", { className: "status-pill", text: formatStatusLabel(inc.status), attrs: { "data-status": inc.status } }),
            el("span", { className: "tier-badge", text: "L" + sev + " · " + prio, attrs: { "data-level": String(sev) } })
          ]),
          el("div", { className: "q-loc", text: inc.location_label }),
          el("div", { className: "q-meta", text: inc.occurred_at + " · " + tier + " · Reporter: " + reporter + " · Assignee: " + assignee })
        ])
      ]);
      card.addEventListener("click", function () { openIncidentDetail(inc.id); });
      host.appendChild(card);
    });
  }

  function openIncidentDetail(id) {
    if (!ops.user) { renderQueueAuthGate(); return; }
    ops.selectedIncidentId = id;
    switchView("queue");
    show($("queue-body"), false);
    show($("queue-detail"), true);
    var content = $("detail-content");
    content.replaceChildren(el("p", { className: "note", text: "Loading incident details…" }));

    Promise.all([
      apiFetch("/api/incidents/" + id),
      ops.staff.length ? Promise.resolve(ops.staff) : apiFetch("/api/users/dispatchers").catch(function () { return []; })
    ])
      .then(function (res) {
        ops.staff = res[1] || ops.staff;
        renderIncidentDetail(res[0]);
      })
      .catch(function (err) {
        content.replaceChildren(el("p", { className: "error", text: err.message || "Could not load incident." }));
      });
  }

  function renderIncidentDetail(inc) {
    $("detail-ref").textContent = inc.reference_code;
    var host = $("detail-content");
    host.replaceChildren();

    var a = inc.latest_assessment;
    var role = ops.user ? ops.user.role : "responder";
    var isStaff = role === "dispatcher" || role === "supervisor";
    var isOwnOpen = ops.user && inc.reported_by && inc.reported_by.id === ops.user.id && inc.status === "open";
    var canEditConditions = isStaff || isOwnOpen;
    var canDelete = role === "supervisor" || isOwnOpen;

    host.appendChild(el("div", { className: "q-card-top" }, [
      el("span", { className: "status-pill", text: formatStatusLabel(inc.status), attrs: { "data-status": inc.status } }),
      a ? el("span", { className: "tier-badge", text: "Level " + a.predicted_severity_level + " · " + a.dispatch_priority, attrs: { "data-level": String(a.predicted_severity_level) } }) : null
    ]));
    host.appendChild(el("h2", { text: inc.location_label, attrs: { style: "margin: 10px 0 4px; text-transform: none; letter-spacing: -0.01em; font-size: 1.25rem; color: var(--ink);" } }));
    host.appendChild(el("p", { className: "hint", text: "Occurred: " + inc.occurred_at + " · Coords: " + inc.latitude.toFixed(4) + ", " + inc.longitude.toFixed(4) }));

    if (a) {
      var sop = el("dl", { className: "sop" });
      [
        ["Impact tier", a.impact_tier],
        ["Clearance window", a.estimated_clearance_time],
        ["Recommended action", a.recommended_action],
        ["Road features", "Crossing: " + (inc.crossing ? "Yes" : "No") + " · Junction: " + (inc.junction ? "Yes" : "No") + " · Signal: " + (inc.traffic_signal ? "Yes" : "No")],
        ["Weather snapshot", inc.weather_condition + " (" + inc.sunrise_sunset + "), Temp " + (inc.temperature != null ? inc.temperature + "°F" : "imputed") + ", Vis " + (inc.visibility != null ? inc.visibility + " mi" : "imputed")]
      ].forEach(function (pair) {
        sop.appendChild(el("div", {}, [el("dt", { text: pair[0] }), el("dd", { text: pair[1] })]));
      });
      host.appendChild(sop);
    }

    // Workflow / Update Form
    if (canEditConditions) {
      var formSec = el("div", { className: "detail-section" });
      formSec.appendChild(el("h3", { text: isStaff ? "Dispatch & Lifecycle Update" : "Update Open Report Notes & Road Flags" }));

      var statusSel = el("select", { attrs: { id: "det-status" } });
      ["open", "dispatched", "in_clearance", "cleared"].forEach(function (st) {
        statusSel.appendChild(el("option", { text: formatStatusLabel(st), attrs: { value: st } }));
      });
      statusSel.value = inc.status;

      var assignSel = el("select", { attrs: { id: "det-assign" } });
      assignSel.appendChild(el("option", { text: "Unassigned", attrs: { value: "" } }));
      ops.staff.forEach(function (u) {
        assignSel.appendChild(el("option", { text: u.full_name + " (" + u.role + ")", attrs: { value: String(u.id) } }));
      });
      assignSel.value = inc.assigned_to ? String(inc.assigned_to.id) : "";

      if (isStaff) {
        formSec.appendChild(el("div", { className: "grid-2" }, [
          el("div", { className: "field" }, [el("label", { text: "Status", attrs: { "for": "det-status" } }), statusSel]),
          el("div", { className: "field" }, [el("label", { text: "Assigned Dispatcher", attrs: { "for": "det-assign" } }), assignSel])
        ]));
      }

      var chkCross = el("input", { attrs: { type: "checkbox", id: "det-cross" } });
      chkCross.checked = !!inc.crossing;
      var chkJunc = el("input", { attrs: { type: "checkbox", id: "det-junc" } });
      chkJunc.checked = !!inc.junction;
      var chkSig = el("input", { attrs: { type: "checkbox", id: "det-sig" } });
      chkSig.checked = !!inc.traffic_signal;
      var chkReassess = el("input", { attrs: { type: "checkbox", id: "det-reassess" } });

      var flagsRow = el("div", { className: "field", attrs: { style: "display:flex; gap:14px; flex-wrap:wrap;" } }, [
        el("label", { className: "mine-toggle" }, [chkCross, document.createTextNode(" Crossing")]),
        el("label", { className: "mine-toggle" }, [chkJunc, document.createTextNode(" Junction")]),
        el("label", { className: "mine-toggle" }, [chkSig, document.createTextNode(" Traffic Signal")]),
        el("label", { className: "mine-toggle" }, [chkReassess, document.createTextNode(" Force ML re-assessment")])
      ]);
      formSec.appendChild(flagsRow);

      var notesEl = el("textarea", { attrs: { id: "det-notes", placeholder: "Operational dispatch notes, lane closures, unit IDs…" } });
      notesEl.value = inc.notes || "";
      formSec.appendChild(el("div", { className: "field" }, [el("label", { text: "Operational Notes", attrs: { "for": "det-notes" } }), notesEl]));

      var detMsg = el("div", { className: "msg", attrs: { role: "status", hidden: "hidden" } });
      var saveBtn = el("button", { className: "btn primary small", text: "Save changes", attrs: { type: "button" } });
      saveBtn.addEventListener("click", function () {
        saveBtn.disabled = true;
        var body = {
          notes: notesEl.value,
          crossing: chkCross.checked,
          junction: chkJunc.checked,
          traffic_signal: chkSig.checked,
          reassess: chkReassess.checked
        };
        if (isStaff) {
          body.status = statusSel.value;
          if (assignSel.value) body.assigned_to_id = parseInt(assignSel.value, 10);
          else body.clear_assignee = true;
        }
        apiFetch("/api/incidents/" + inc.id, { method: "PATCH", body: body })
          .then(function (updated) { renderIncidentDetail(updated); })
          .catch(function (err) { showMsg(detMsg, "error", err.message || "Update failed."); })
          .then(function () { saveBtn.disabled = false; });
      });

      var btnRow = el("div", { className: "actions row", attrs: { style: "margin-top: 12px;" } }, [saveBtn]);
      if (canDelete) {
        var delBtn = el("button", { className: "btn small danger", text: "Delete incident", attrs: { type: "button" } });
        delBtn.addEventListener("click", function () {
          if (!window.confirm("Permanently delete " + inc.reference_code + " and its assessment history?")) return;
          delBtn.disabled = true;
          apiFetch("/api/incidents/" + inc.id, { method: "DELETE" })
            .then(function () { loadQueue(); })
            .catch(function (err) {
              delBtn.disabled = false;
              showMsg(detMsg, "error", err.message || "Delete failed.");
            });
        });
        btnRow.appendChild(delBtn);
      }
      formSec.appendChild(btnRow);
      formSec.appendChild(detMsg);
      host.appendChild(formSec);
    }

    // Assessment History
    var histSec = el("div", { className: "detail-section" });
    histSec.appendChild(el("h3", { text: "ML Assessment History (" + (inc.assessments ? inc.assessments.length : 0) + ")" }));
    var histList = el("ul", { className: "timeline" });
    (inc.assessments || []).forEach(function (item) {
      histList.appendChild(el("li", {}, [
        el("div", { text: "Level " + item.predicted_severity_level + " (" + item.impact_tier + ") · Priority: " + item.dispatch_priority }),
        el("div", { className: "timeline-meta", text: "By " + (item.assessed_by || "Operator") + " · " + item.created_at })
      ]));
    });
    histSec.appendChild(histList);
    host.appendChild(histSec);

    // Audit Log
    var logSec = el("div", { className: "detail-section" });
    logSec.appendChild(el("h3", { text: "Audit Trail" }));
    var logList = el("ul", { className: "timeline" });
    (inc.audit_logs || []).forEach(function (log) {
      logList.appendChild(el("li", {}, [
        el("div", { text: log.action + " — " + log.detail }),
        el("div", { className: "timeline-meta", text: log.actor_name + " · " + log.created_at })
      ]));
    });
    logSec.appendChild(logList);
    host.appendChild(logSec);
  }

  /* ------------------------------------------------------------------ */
  /* Operations Analytics & Supervisor User Role Governance              */
  /* ------------------------------------------------------------------ */

  function loadAnalytics() {
    if (!ops.user || (ops.user.role !== "dispatcher" && ops.user.role !== "supervisor")) {
      showMsg($("analytics-msg"), "error", "Restricted to Dispatcher or Supervisor roles.");
      return;
    }
    showMsg($("analytics-msg"), "loading", "Loading analytics…");
    var isSupervisor = ops.user.role === "supervisor";
    Promise.all([
      apiFetch("/api/analytics/summary"),
      isSupervisor ? apiFetch("/api/users") : Promise.resolve(null)
    ])
      .then(function (res) {
        showMsg($("analytics-msg"), "loading", "");
        renderAnalytics(res[0], res[1]);
      })
      .catch(function (err) {
        showMsg($("analytics-msg"), "error", err.message || "Could not load analytics.");
      });
  }

  function renderAnalytics(summary, users) {
    var host = $("analytics-content");
    host.replaceChildren();

    var kpis = el("div", { className: "kpi-grid" });
    [
      [summary.total_incidents, "Total incidents"],
      [summary.total_assessments, "ML assessments run"],
      [summary.by_status.open || 0, "Open queue"],
      [summary.active_users, "Active stakeholders"]
    ].forEach(function (pair) {
      kpis.appendChild(el("div", { className: "kpi-card" }, [
        el("div", { className: "kpi-val", text: String(pair[0]) }),
        el("div", { className: "kpi-lbl", text: pair[1] })
      ]));
    });
    host.appendChild(kpis);

    host.appendChild(el("h2", { text: "Severity Breakdown" }));
    var sevList = el("ul", { className: "probs" });
    var total = Math.max(summary.total_incidents, 1);
    ["1", "2", "3", "4"].forEach(function (lvl) {
      var cnt = (summary.by_severity && summary.by_severity[lvl]) || 0;
      var pct = Math.round((cnt / total) * 100);
      var bar = el("div", { className: "bar" }, [el("span")]);
      bar.firstChild.style.width = pct + "%";
      sevList.appendChild(el("li", {}, [
        el("span", { text: "Level " + lvl }),
        bar,
        el("span", { className: "pct", text: String(cnt) })
      ]));
    });
    host.appendChild(sevList);

    if (users && Array.isArray(users)) {
      host.appendChild(el("h2", { text: "Stakeholder Role Management (Supervisor)" }));
      var uList = el("div", { className: "user-list" });
      users.forEach(function (u) {
        var roleSel = el("select", { attrs: { "aria-label": "Role for " + u.full_name } });
        ["responder", "dispatcher", "supervisor"].forEach(function (r) {
          roleSel.appendChild(el("option", { text: r, attrs: { value: r } }));
        });
        roleSel.value = u.role;

        var statusBtn = el("button", {
          className: "btn small",
          text: u.is_active ? "Active" : "Disabled",
          attrs: { type: "button" }
        });

        roleSel.addEventListener("change", function () {
          apiFetch("/api/users/" + u.id, { method: "PATCH", body: { role: roleSel.value } })
            .then(function () { loadAnalytics(); })
            .catch(function (err) { showMsg($("analytics-msg"), "error", err.message); });
        });
        statusBtn.addEventListener("click", function () {
          apiFetch("/api/users/" + u.id, { method: "PATCH", body: { is_active: !u.is_active } })
            .then(function () { loadAnalytics(); })
            .catch(function (err) { showMsg($("analytics-msg"), "error", err.message); });
        });

        uList.appendChild(el("div", { className: "user-row" }, [
          el("div", {}, [
            el("div", { className: "q-loc", text: u.full_name }),
            el("div", { className: "q-meta", text: u.email })
          ]),
          el("div", { className: "user-row-controls" }, [roleSel, statusBtn])
        ]));
      });
      host.appendChild(uList);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Public Bridge for app.js                                            */
  /* ------------------------------------------------------------------ */

  window.WorkspaceOps = {
    isAuthenticated: function () { return !!ops.user; },
    submitAssessment: function (payload, context) {
      if (!ops.user) {
        return apiFetch("/predict", { method: "POST", body: payload }).then(function (data) {
          return { persisted: false, prediction: data, incident: null };
        });
      }
      var body = Object.assign({}, payload, {
        location_label: context.location_label,
        latitude: context.latitude,
        longitude: context.longitude,
        occurred_at: context.occurred_at,
        telemetry_provenance: context.telemetry_provenance || {},
        notes: ""
      });
      return apiFetch("/api/incidents", { method: "POST", body: body }).then(function (inc) {
        return { persisted: true, prediction: inc.latest_assessment, incident: inc };
      });
    },
    openAuthModal: openAuthModal,
    openIncidentDetail: openIncidentDetail,
    switchView: switchView
  };

  /* ------------------------------------------------------------------ */
  /* Wiring                                                              */
  /* ------------------------------------------------------------------ */

  function init() {
    $("tab-assess").addEventListener("click", function () { switchView("assess"); });
    $("tab-queue").addEventListener("click", function () { switchView("queue"); });
    $("tab-analytics").addEventListener("click", function () { switchView("analytics"); });

    $("open-auth-btn").addEventListener("click", function () { openAuthModal("login"); });
    $("close-auth-btn").addEventListener("click", closeAuthModal);
    $("signout-btn").addEventListener("click", signOut);
    $("auth-tab-login").addEventListener("click", function () { switchAuthTab("login"); });
    $("auth-tab-register").addEventListener("click", function () { switchAuthTab("register"); });
    $("login-form").addEventListener("submit", handleLogin);
    $("register-form").addEventListener("submit", handleRegister);

    document.querySelectorAll(".demo-fill").forEach(function (btn) {
      btn.addEventListener("click", function () {
        $("login-email").value = btn.dataset.email;
        $("login-password").value = "TocDemo@2026";
      });
    });

    $("queue-refresh-btn").addEventListener("click", loadQueue);
    $("filter-q").addEventListener("input", loadQueue);
    $("filter-status").addEventListener("change", loadQueue);
    $("filter-severity").addEventListener("change", loadQueue);
    $("filter-mine").addEventListener("change", loadQueue);
    $("detail-back-btn").addEventListener("click", loadQueue);

    restoreSession();
  }

  init();
})();
