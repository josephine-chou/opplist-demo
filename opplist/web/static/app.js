/* Plain browser JavaScript, no framework and no build step.
   Nothing here is load bearing: every rule it applies is checked again on the
   server, so the page still works with scripting turned off. */
(function () {
  "use strict";

  // ---------------------------------------------------------- helpers --
  function $(id) { return document.getElementById(id); }
  function on(el, evt, fn) { if (el) { el.addEventListener(evt, fn); } }

  // ------------------------------------------------ RQ2: a mail dropped --
  // The Read quote panel's box: a .msg dragged in from Outlook (or an
  // .eml) goes into the file input; the server checks the kind again.
  document.querySelectorAll("[data-rq-drop]").forEach(function (box) {
    var input = box.querySelector("input[type=file]");
    var said = box.querySelector("[data-rq-name]");
    function show() {
      var f = input && input.files && input.files[0];
      if (said) { said.textContent = f ? f.name + " (" + Math.max(1, Math.round(f.size / 1024)) + " KB)" : ""; }
      box.classList.toggle("has-file", !!f);
    }
    on(input, "change", show);
    ["dragenter", "dragover"].forEach(function (evt) {
      on(box, evt, function (e) { e.preventDefault(); box.classList.add("over"); });
    });
    on(box, "dragleave", function () { box.classList.remove("over"); });
    on(box, "drop", function (e) {
      e.preventDefault();
      box.classList.remove("over");
      if (input && e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        show();
      }
    });
  });

  // ------------------------------------------------------------ toasts --
  // F1: every message is a toast fixed under the top bar. Done (green)
  // fades after 6 seconds and waits while the pointer is on it; a warning
  // (amber: refused, nothing done) stays until its x or her next action.
  var TOAST_MS = 6000;
  function topbarHeight() {
    var bar = document.querySelector("header.topbar");
    if (bar) { document.documentElement.style.setProperty("--topbar-h", bar.getBoundingClientRect().height + "px"); }
  }
  function toastGone(t) { if (t && t.parentNode) { t.parentNode.removeChild(t); } }
  function toastFade(t) {
    t.classList.add("fading");
    setTimeout(function () { toastGone(t); }, 450);
  }
  function toastArm(t) {
    if (t.__armed) { return; }
    t.__armed = true;
    var close = t.querySelector(".t-close");
    if (close) { close.addEventListener("click", function () { toastGone(t); }); }
    if (!t.classList.contains("done")) { return; }
    var timer = setTimeout(function () { toastFade(t); }, TOAST_MS);
    t.addEventListener("mouseenter", function () { clearTimeout(timer); t.classList.remove("fading"); });
    t.addEventListener("mouseleave", function () { timer = setTimeout(function () { toastFade(t); }, TOAST_MS); });
  }
  function toastsArm(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll(".toast"), toastArm);
  }
  // her next action (a click on a button or a link, a form sent) clears the warnings
  function clearWarnings(evt) {
    var t = evt.target;
    if (t && t.closest && t.closest(".toasts")) { return; }
    if (evt.type === "click" && !(t && t.closest && t.closest("button, a, input[type=submit]"))) { return; }
    Array.prototype.forEach.call(document.querySelectorAll(".toast.warn"), toastGone);
  }
  document.addEventListener("click", clearWarnings, true);
  document.addEventListener("submit", clearWarnings, true);
  window.addEventListener("resize", topbarHeight);
  topbarHeight();
  toastsArm(document);
  // the case page's in-place refresh brings its new messages here
  window.OppToasts = {
    add: function (doc) {
      var box = document.getElementById("toasts");
      var fresh = doc && doc.querySelectorAll("#toasts .toast");
      if (!box || !fresh) { return; }
      Array.prototype.forEach.call(fresh, function (t) { box.appendChild(document.importNode(t, true)); });
      toastsArm(box);
    }
  };

  // Dates month first (11c), the twin of opplist/dates.py month_day:
  // dayMonth("2026-08-25") -> "Aug 25"; with weekday "Tue Aug 25"; with
  // year "Sep 20, 2026". "" when the value is not an ISO date.
  var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var WDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function dayMonth(value, weekday, year) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ""));
    if (!m) { return ""; }
    var d = new Date(+m[1], +m[2] - 1, +m[3]);
    var text = MON[d.getMonth()] + " " + d.getDate();
    if (weekday) { text = WDAY[d.getDay()] + " " + text; }
    if (year) { text += ", " + d.getFullYear(); }
    return text;
  }

  // Show or hide an inline "Add ..." block; a similar party picked as the
  // requester; 'Register as new anyway'; copy a textarea. Delegated (15a):
  // the case page swaps parts of itself in after a write, and a button in a
  // new part works like the one it replaced.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-toggle], [data-pick-party], [data-hide], [data-copy]") : null;
    if (!btn) { return; }
    if (btn.hasAttribute("data-toggle")) {
      var target = $(btn.getAttribute("data-toggle"));
      if (target) { target.hidden = !target.hidden; if (!target.hidden) target.focus(); }
      // 'Show the mail text' (v34): the button says what it will do next
      if (btn.classList.contains("src-toggle") && target) {
        btn.textContent = !target.hidden ? btn.getAttribute("data-hide") : btn.getAttribute("data-show");
      }
    } else if (btn.hasAttribute("data-pick-party")) {
      var select = $("requester");
      if (!select) { return; }
      select.value = btn.getAttribute("data-pick-party");
      var box = $("new-party");
      if (box) {
        var key = box.querySelector('[name="new_party_key"]');
        if (key) { key.value = ""; }
        box.hidden = true;
      }
      select.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (btn.hasAttribute("data-hide")) {
      var gone = $(btn.getAttribute("data-hide"));
      if (gone) { gone.hidden = true; }
    } else if (btn.hasAttribute("data-copy")) {
      copyText(btn);
    }
  });

  // AL1: the case number in the case header copies '[OPP-1504] ' - the
  // brackets and one trailing space - for the acknowledgement's subject;
  // 'Copied' shows beside it for two seconds. The clipboard API works on
  // 127.0.0.1 (a secure context); a hidden textarea is the fallback.
  function copyOpp(el) {
    var text = el.getAttribute("data-copy-opp") || "";
    var note = el.nextElementSibling && el.nextElementSibling.classList.contains("opp-copied") ? el.nextElementSibling : null;
    function done() {
      if (!note) { return; }
      note.hidden = false;
      clearTimeout(note.__t);
      note.__t = setTimeout(function () { note.hidden = true; }, 2000);
    }
    function fallback() {
      var box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed"; box.style.left = "-9999px"; box.style.top = "0";
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      document.body.removeChild(box);
      if (ok) { done(); }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("[data-copy-opp]") : null;
    if (el) { e.preventDefault(); copyOpp(el); }
  });
  document.addEventListener("keydown", function (e) {
    var el = e.target && e.target.closest ? e.target.closest("[data-copy-opp]") : null;
    if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); copyOpp(el); }
  });

  // The batch 'Mark selected Lost' under R3: say how many before it goes,
  // and (L2) how many with each No response reason.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    on(form, "submit", function (event) {
      var boxes = document.querySelectorAll('input.decide-pick[form="' + form.id + '"]:checked');
      var picked = boxes.length;
      if (!picked) {
        event.preventDefault();
        window.alert("Tick the cases to mark Lost first.");
        return;
      }
      var counts = {}, order = [];
      Array.prototype.forEach.call(boxes, function (box) {
        var reason = box.getAttribute("data-reason") || "No response";
        if (!counts[reason]) { counts[reason] = 0; order.push(reason); }
        counts[reason] += 1;
      });
      var lines = order.map(function (reason) { return counts[reason] + " x " + reason; }).join("\n");
      if (!window.confirm(form.getAttribute("data-confirm").replace("{n}", picked).replace("{reasons}", lines))) {
        event.preventDefault();
      }
    });
  });

  // The case page's Mail card (2026-09-25): tabs and rows switch what is
  // shown; every mail is already on the page, nothing is fetched. Delegated
  // (15a): the card is swapped in again after a write.
  document.addEventListener("click", function (e) {
    var card = $("mail");
    if (!card || !e.target.closest || !card.contains(e.target)) { return; }
    function show(row) {
      card.querySelectorAll(".mail").forEach(function (x) { x.classList.remove("on"); });
      row.classList.add("on");
      var wanted = "read-" + row.getAttribute("data-mail");
      card.querySelectorAll(".reading").forEach(function (r) { r.hidden = (r.id !== wanted); });
    }
    var tab = e.target.closest(".tab");
    if (tab) {
      card.querySelectorAll(".tab").forEach(function (x) { x.classList.remove("on"); });
      tab.classList.add("on");
      var key = tab.getAttribute("data-tab");
      card.querySelectorAll(".mails").forEach(function (list) { list.hidden = (list.id !== "tab-" + key); });
      var first = card.querySelector("#tab-" + key + " .mail.on") || card.querySelector("#tab-" + key + " .mail");
      if (first) { show(first); }
      return;
    }
    var row = e.target.closest(".mail");
    if (row) { show(row); }
  });

  // Copy a textarea (the follow-up text) to the clipboard; the button says
  // so for a moment. Without a clipboard the text is selected instead.
  function copyText(btn) {
    var box = $(btn.getAttribute("data-copy"));
    if (!box) { return; }
    var label = btn.textContent;
    function done() {
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = label; }, 1500);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(box.value).then(done, function () { box.select(); });
    } else {
      box.select();
      try { if (document.execCommand("copy")) { done(); } } catch (err) { /* leave it selected */ }
    }
  }

  // Folded until its button is pressed (Tracking's Drop... / Lost...):
  // without scripting the block simply stays visible.
  document.querySelectorAll(".js-hide").forEach(function (el) { el.hidden = true; });

  // ------------------------------------------- tracking: other rules ----
  // The folded rules remember whether she left them open, per rule.
  document.querySelectorAll("details.tracking-section.other[data-rule]").forEach(function (box) {
    var key = "tracking-open-" + box.getAttribute("data-rule");
    try { if (localStorage.getItem(key) === "1") { box.open = true; } } catch (e) { /* storage off */ }
    on(box, "toggle", function () {
      try { localStorage.setItem(key, box.open ? "1" : "0"); } catch (e) { /* storage off */ }
    });
  });

  // ------------------------------------------- conditional quantity ----
  // Mirrors forms.field_state() in Python. Fields are dimmed, not hidden.
  var FREE_TEXT = ["RORO", "Break Bulk", "Project", "Multi-mode", "Truck", "Cross-border Truck"];
  var AIR_MODES = ["Air", "Sea-Air"];       // SA1: forms.AIR_MODES
  var ORIGIN_ALT = ["EXW", "FCA"];
  var DEST_ALT = ["DAP", "DPU", "DDP", "DDU"];
  // S1: the Service decides the places (pickup, delivery); the incoterm
  // only when the Service is empty or not one of these four
  var SERVICE_PLACES = {
    "door to door": [true, true], "door to port": [true, false],
    "port to door": [false, true], "port to port": [false, false]
  };
  function placesNeeded(incoterm, service) {
    var known = SERVICE_PLACES[(service || "").trim().toLowerCase()];
    if (known) { return { origin_alt: known[0], dest_alt: known[1] }; }
    return { origin_alt: ORIGIN_ALT.indexOf(incoterm) >= 0, dest_alt: DEST_ALT.indexOf(incoterm) >= 0 };
  }

  function fieldState(mode, incoterm, service) {
    var s = {
      air_weight_kg: "dim", lcl_cbm: "dim", container_type: "dim",
      container_qty: "dim", qty_raw: "dim", origin_alt: "dim", dest_alt: "dim"
    };
    if (AIR_MODES.indexOf(mode) >= 0) { s.air_weight_kg = "required"; s.lcl_cbm = "normal"; }
    else if (mode === "LCL") { s.lcl_cbm = "required"; s.air_weight_kg = "normal"; }
    else if (mode === "FCL") { s.container_type = "required"; s.container_qty = "required"; }
    else if (FREE_TEXT.indexOf(mode) >= 0) { s.qty_raw = "required"; }
    else if (mode === "Not specified") {
      ["air_weight_kg", "lcl_cbm", "container_type", "container_qty", "qty_raw"]
        .forEach(function (k) { s[k] = "normal"; });
    }
    var places = placesNeeded(incoterm, service);
    if (places.origin_alt) { s.origin_alt = "required"; }
    if (places.dest_alt) { s.dest_alt = "required"; }
    return s;
  }

  var LABELS = {
    Air: { lcl_cbm: "Volume (cbm), for chargeable weight" },
    LCL: { air_weight_kg: "Weight (kg)" }
  };
  var DEFAULT_LABELS = {
    air_weight_kg: "Air weight (kg)", lcl_cbm: "LCL volume (cbm)"
  };

  function paintState(scope, state, names, mode) {
    var labels = LABELS[mode] || {};
    names.forEach(function (name) {
      var box = scope.querySelector('[data-field="' + name + '"]');
      if (!box) { return; }
      box.classList.remove("dim", "required", "normal");
      box.classList.add(state[name]);
      var caption = box.querySelector("span");
      if (caption && DEFAULT_LABELS[name]) {
        caption.textContent = labels[name] || DEFAULT_LABELS[name];
      }
    });
  }
  // One route of the register page (15c): its own Service; a mode or an
  // incoterm left blank on route B is route A's, as the server reads it
  function applyRouteState(block) {
    var a = document.querySelector('.reg-route[data-route-key="a"]');
    function val(b, name) { var el = b ? b.querySelector('[name$="' + name + '"]') : null; return el ? el.value : ""; }
    var mode = val(block, "service_mode") || (block !== a ? val(a, "service_mode") : "");
    var incoterm = val(block, "incoterm") || (block !== a ? val(a, "incoterm") : "");
    paintState(block, fieldState(mode, incoterm, val(block, "service_scope")), ["origin_alt", "dest_alt"], mode);
  }
  function applyState(scope) {
    scope = scope || document;
    if (scope.classList && scope.classList.contains("reg-route")) { applyRouteState(scope); return; }
    var modeBox = scope.querySelector('[name="service_mode"]'), termBox = scope.querySelector('[name="incoterm"]');
    if (!modeBox && !termBox) { return; }
    var mode = modeBox ? modeBox.value : "";
    var incoterm = termBox ? termBox.value : "";
    var serviceBox = scope.querySelector('[name="service_scope"]');
    var state = fieldState(mode, incoterm, serviceBox ? serviceBox.value : "");
    var blocks = scope.querySelectorAll(".reg-route");
    var names = Object.keys(state);
    if (blocks.length) {
      // the register page: the quantities follow route A's mode, the
      // places each route's own Service and incoterm
      names = names.filter(function (n) { return n !== "origin_alt" && n !== "dest_alt"; });
    }
    paintState(scope, state, names, mode);
    Array.prototype.forEach.call(blocks, applyRouteState);
  }
  function applyStates(root) {
    (root || document).querySelectorAll("form").forEach(function (f) {
      if (f.querySelector('select[name="service_mode"], select[name="incoterm"]')) { applyState(f); }
    });
  }
  document.addEventListener("change", function (e) {
    var name = (e.target && e.target.name) || "";
    // S1: the Service greys the places as the mode and the incoterm do; a
    // route B field (route_1_incoterm) repaints the page's routes too
    if (/(^|_)(service_mode|incoterm|service_scope)$/.test(name)) { applyState(e.target.form || document); }
  });
  // ----------------------------------- E1: a suggestion's Edit ----
  // Edit opens the row under the suggestion; Record as shows the fields of
  // the action chosen (the others disabled, so the form sends only those).
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-sg-edit], [data-sg-cancel]") : null;
    if (!b) { return; }
    var row = b.hasAttribute("data-sg-edit") ? document.getElementById("sg-edit-" + b.getAttribute("data-sg-edit"))
      : b.closest("tr.s-edit");
    if (!row) { return; }
    row.hidden = b.hasAttribute("data-sg-edit") ? !row.hidden : true;
    var btn = document.querySelector('[data-sg-edit="' + row.id.replace("sg-edit-", "") + '"]');
    if (btn) { btn.classList.toggle("on", !row.hidden); }
  });
  document.addEventListener("change", function (e) {
    var sel = e.target;
    if (!sel || !sel.matches || !sel.matches("[data-sg-as]")) { return; }
    Array.prototype.forEach.call(sel.form.querySelectorAll("fieldset.sg-as"), function (fs) {
      var on = fs.getAttribute("data-as") === sel.value;
      fs.disabled = !on;
      fs.hidden = !on;
    });
  });
  // ------------------------------------------------- end customer hint --
  // A direct customer is its own end customer; an agent's is the shipper.
  document.addEventListener("change", function (e) {
    if (!e.target || e.target.id !== "requester") { return; }
    var sel = e.target, box = $("end_customer");
    if (!box) { return; }
    var option = sel.options[sel.selectedIndex];
    var type = option ? option.getAttribute("data-type") : "";
    if (type === "Direct Customer" && !box.value) {
      box.value = option.textContent.trim().split(" (")[0];
    }
    box.placeholder = (type === "Direct Customer")
      ? "Same as the requester unless it differs"
      : "Actual shipper / consignee";
  });
  // ------------------------------------------- RD1: a gateway's parent ---
  // A city has no parent and no 'No parent city' box; an airport, seaport
  // or ramp shows both. The server refuses a gateway without either.
  document.addEventListener("change", function (e) {
    if (!e.target || !e.target.hasAttribute || !e.target.hasAttribute("data-location-type")) { return; }
    var form = e.target.form, city = e.target.value === "city";
    Array.prototype.forEach.call(form.querySelectorAll(".gateway-only"), function (el) { el.hidden = city; });
  });
  // --------------------------------------------- locations by country ---
  // The dropdown lists every master place; the search box narrows it on
  // key, name, alias and area, the country select on the country, and an
  // area picked lists the whole area whatever the country says.
  function filterLocations(panel, side) {
    // ends with: the register page's routes B, C carry a prefix (15c)
    var country = panel.querySelector('select[name$="' + side + '_country"]');
    var list = panel.querySelector('select[name$="' + side + '_loc"]');
    if (!list) { return; }
    var wanted = country ? country.value : "";
    var search = panel.querySelector('.loc-search[data-side="' + side + '"]');
    var term = search ? search.value.trim().toLowerCase() : "";
    var areaBox = panel.querySelector('.loc-area[data-side="' + side + '"]');
    var area = areaBox ? areaBox.value : "";
    var firstVisible = null;
    Array.prototype.forEach.call(list.options, function (opt) {
      if (!opt.value) { return; }
      var show;
      if (area) {
        show = (opt.getAttribute("data-area") || "") === area;
      } else {
        var owner = opt.getAttribute("data-country") || "";
        show = !wanted || owner === wanted;
        if (show && term) {
          show = (opt.getAttribute("data-search") || opt.textContent.toLowerCase()).indexOf(term) !== -1;
        }
      }
      // Keep the current choice visible even if the filters disagree, so a
      // saved value never silently disappears from the dropdown.
      opt.hidden = !show && !opt.selected;
      if (show && !firstVisible) { firstVisible = opt; }
    });
    if (term && firstVisible && !list.value) { list.value = firstVisible.value; }
  }
  function filterAll(root) {
    (root || document).querySelectorAll(".route-panels").forEach(function (panel) {
      filterLocations(panel, "origin");
      filterLocations(panel, "dest");
    });
  }
  ["input", "change"].forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      var el = e.target, side = el && el.getAttribute && el.getAttribute("data-side");
      if (!side || !el.closest) { return; }
      var panel = el.closest(".route-panels");
      if (panel) { filterLocations(panel, side); }
    });
  });
  filterAll(document);

  // ------------------------------------------------ place pickers (23) ---
  // One picker for Origin location, Dest. location and Port of discharge
  // (batch 23; the sample OppList_Pickers_v2_SAMPLE.html), on
  // the case page's route editor and the register page. Typing lists
  // matches right under the box - the side's country first, the rest under
  // "Other countries" - on key, name, aliases, UN/LOCODE and IATA code;
  // retired places are not on the page's list (#place-data) and To confirm
  // ones say so. Up and down move, Enter takes the highlighted place and
  // never sends the card, Esc closes, a click takes it; a chosen place is a
  // chip with an x. Dest. location and Port of discharge take up to four,
  // read with "or". The Port of discharge lists ports (airports, as
  // Airport of destination, on Air); a master place without that flag,
  // typed in full, offers "+ Mark ... as a port". No match offers
  // "+ Add location" (a small form; the place is To confirm). The picker
  // posts the keys joined with "|"; a place added in its small form and a
  // place marked ride along as JSON and are written when the route is
  // saved. Nothing here is load bearing: the server reads it all again.
  var Pickers = (function () {
    var cache = { el: null, data: null }, added = [];
    function norm(s) { return String(s == null ? "" : s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }
    function copy(p) { var out = {}; Object.keys(p || {}).forEach(function (k) { out[k] = p[k]; }); return out; }
    function prep(p) {
      var all = [p.key, p.name, p.unlocode, p.iata].concat(p.aliases || []).filter(Boolean);
      p._t = norm(all.join(" ")).split(/[^a-z0-9]+/).filter(Boolean);
      p._x = all.map(function (x) { return norm(x).trim(); });
      return p;
    }
    // the page's list of places, read again when the case page swaps it in;
    // a place added in a small form on this page joins it until the page goes
    function data() {
      var el = document.getElementById("place-data");
      if (!el) { return { places: [], countries: [], byKey: {}, max: 1 }; }
      if (cache.el !== el) {
        var d;
        try { d = JSON.parse(el.textContent || "{}") || {}; } catch (err) { d = {}; }
        d.places = (d.places || []).map(prep);
        d.byKey = {};
        d.places.forEach(function (p) { d.byKey[p.key] = p; });
        added.forEach(function (p) { if (!d.byKey[p.key]) { d.places.push(p); d.byKey[p.key] = p; } });
        d.countries = d.countries || [];
        cache = { el: el, data: d };
      }
      return cache.data;
    }
    function isPort(p) { return !!(p && (p.port || p.type === "seaport")); }
    function isAir(p) { return !!(p && (p.airport || p.type === "airport")); }
    // what a place is, from its flags (places.kind in Python)
    function kind(p) {
      if (!p || p.legacy) { return ""; }
      var a = isPort(p), b = isAir(p);
      if (a && b) { return "port, airport"; }
      if (a) { return "port"; }
      if (b) { return "airport"; }
      return p.type === "ramp" ? "rail ramp" : "city";
    }
    function parseList(text) {
      try { var v = JSON.parse(text || "[]"); return Array.isArray(v) ? v : []; } catch (err) { return []; }
    }
    function part(pk, cls) { return pk.querySelector("input." + cls); }
    function sideOf(pk) { return pk.getAttribute("data-side") || ""; }
    function maxOf(pk) { return parseInt(pk.getAttribute("data-max"), 10) || 1; }
    // the route a picker belongs to: a register-page route, else its form
    function blockOf(pk) { return pk.closest(".reg-route") || pk.closest("form") || document; }
    function fieldOf(pk, plain) { return blockOf(pk).querySelector('[name$="' + plain + '"]'); }
    function countryBox(pk) { return fieldOf(pk, sideOf(pk) === "origin" ? "origin_country" : "dest_country"); }
    function modeOf(pk) {
      var box = fieldOf(pk, "service_mode"), mode = box ? box.value : "";
      if (!mode && pk.closest(".reg-route")) {        // route B with no mode of its own: route A's
        var a = document.querySelector('.reg-route[data-route-key="a"] select[name="service_mode"]');
        mode = a ? a.value : "";
      }
      return mode;
    }
    function want(pk) { return sideOf(pk) !== "pod" ? "" : (AIR_MODES.indexOf(modeOf(pk)) >= 0 ? "airport" : "port"); }
    function fits(pk, p) { var w = want(pk); return !w || (w === "airport" ? isAir(p) : isPort(p)); }

    // ---- the state: the field's keys, read against the page's places
    function load(pk) {
      var d = data(), own = {}, fresh = {}, marks = {};
      parseList(pk.getAttribute("data-picks")).forEach(function (p) { if (p && p.key) { own[p.key] = p; } });
      parseList((part(pk, "pk-new") || {}).value).forEach(function (e) { if (e && e.key) { fresh[e.key] = e; } });
      parseList((part(pk, "pk-mark") || {}).value).forEach(function (e) { if (e && e.key) { marks[e.key] = e.kind; } });
      var keys = String((part(pk, "pk-value") || {}).value || "").split("|")
        .map(function (k) { return k.trim(); }).filter(Boolean);
      pk.__picks = keys.map(function (k) {
        if (fresh[k]) {
          var e = fresh[k];
          return prep({ key: k, name: e.name || "", country: e.country || "", port: e.port ? 1 : 0,
                        airport: e.airport ? 1 : 0, status: "to_confirm", type: "city", isNew: true });
        }
        var p = d.byKey[k] || (own[k] && !own[k].legacy ? own[k] : null);
        if (!p) { return { key: k, legacy: true }; }
        var c = copy(p);
        if (marks[k]) { c.mark = marks[k]; if (marks[k] === "airport") { c.airport = 1; } else { c.port = 1; } }
        return c;
      });
    }
    function save(pk) {
      var picks = pk.__picks || [], value = part(pk, "pk-value"), fresh = part(pk, "pk-new"), mk = part(pk, "pk-mark");
      value.value = picks.map(function (p) { return p.key; }).join("|");
      var made = picks.filter(function (p) { return p.isNew; }).map(function (p) {
        return { key: p.key, name: p.name || "", country: p.country || "", port: p.port ? 1 : 0, airport: p.airport ? 1 : 0 };
      });
      if (fresh) { fresh.value = made.length ? JSON.stringify(made) : ""; }
      if (mk) {
        var marked = picks.filter(function (p) { return p.mark; }).map(function (p) { return { key: p.key, kind: p.mark }; });
        mk.value = marked.length ? JSON.stringify(marked) : "";
      }
      // the page's own scripts follow it: the card's outline and the bottom
      // bar, the register page's title, totals, checks and its draft
      value.dispatchEvent(new Event("input", { bubbles: true }));
      value.dispatchEvent(new Event("change", { bubbles: true }));
    }

    // ---- drawing
    function chip(p, i, single) {
      var bits = [], note = "";
      if (single && p.name && norm(p.name) !== norm(p.key)) { bits.push(esc(p.name)); }
      if (kind(p)) { bits.push(kind(p)); }
      if (p.legacy) { note = "not in the location list"; }
      else if (p.mark) { note = "marked as " + (p.mark === "airport" ? "an airport" : "a port"); }
      else if (p.isNew) { note = "to confirm"; }
      else if (p.status === "to_confirm") { note = "to confirm"; }
      return '<span class="pk-chip' + (p.legacy ? " legacy" : "") + (p.isNew ? " new" : "") + '" data-i="' + i + '"><b>' +
        (p.isNew ? "new: " : "") + esc(p.key) + "</b>" + (bits.length ? '<span class="pk-m"> &middot; ' + bits.join(" &middot; ") + "</span>" : "") +
        (note ? '<span class="pk-note"> (' + note + ")</span>" : "") +
        '<button type="button" class="pk-x" data-i="' + i + '" title="Remove" aria-label="Remove ' + esc(p.key) + '">&times;</button></span>';
    }
    function placeholder(pk) {
      if ((pk.__picks || []).length) { return "or…"; }
      var w = want(pk);
      return w === "airport" ? "search airport" : (w === "port" ? "search port" : "search key, name, alias, code");
    }
    function labelOf(pk) {
      var f = pk.closest(".pk-f"), label = f ? f.querySelector(".pk-label") : null;
      return label ? label.textContent : "";
    }
    function render(pk) {
      var picks = pk.__picks || [], single = maxOf(pk) === 1, html = "";
      picks.forEach(function (p, i) {
        if (i) { html += '<span class="pk-or">or</span>'; }
        html += chip(p, i, single);
      });
      if (picks.length < maxOf(pk) || (single && pk.__editing)) {
        html += '<input type="text" class="pk-q" autocomplete="off" spellcheck="false" aria-label="' +
          esc(labelOf(pk)) + '" placeholder="' + esc(single && picks.length ? "" : placeholder(pk)) + '">';
      }
      pk.querySelector(".pk-box").innerHTML = html;
    }
    // the box takes the cursor; `quiet` (after a pick or an x) leaves the
    // list shut until she types, so it does not cover the boxes below
    function focusQ(pk, quiet) {
      var q = pk.querySelector(".pk-q");
      if (!q) { return; }
      pk.__quiet = !!quiet;
      q.focus();
      pk.__quiet = false;
    }

    // ---- the list under the box
    function search(pk, term) {
      var d = data(), t = norm(term).trim(), words = t.split(/[^a-z0-9]+/).filter(Boolean);
      var box = countryBox(pk), country = box ? box.value : "", taken = {}, mine = [], other = [];
      (pk.__picks || []).forEach(function (p) { taken[p.key] = true; });
      d.places.forEach(function (p) {
        if (taken[p.key] || !fits(pk, p)) { return; }
        // every word typed starts a word of the place's key, name, alias or code
        if (words.length && !words.every(function (w) {
          return p._t.some(function (x) { return x.indexOf(w) === 0; });
        })) { return; }
        if (!country || p.country === country) { mine.push(p); } else if (words.length) { other.push(p); }
      });
      if (!words.length && !country) { mine = []; }
      function rank(p) { return p._x.indexOf(t) >= 0 ? 0 : (norm(p.key).indexOf(t) === 0 ? 1 : 2); }
      function order(a, b) { return (rank(a) - rank(b)) || (a.key < b.key ? -1 : (a.key > b.key ? 1 : 0)); }
      mine.sort(order); other.sort(order);
      return { mine: mine.slice(0, words.length ? 12 : 40), other: other.slice(0, 8), country: country };
    }
    // the places the text is, in full: its key, else its name, alias or code
    function exactly(term) {
      var t = norm(term).trim();
      if (!t) { return []; }
      return data().places.filter(function (p) { return p._x.indexOf(t) >= 0; });
    }
    function sameOf(term, hits) {
      var t = norm(term).trim(), keyed = hits.filter(function (p) { return norm(p.key) === t; });
      return keyed.length ? keyed[0] : (hits.length === 1 ? hits[0] : null);
    }
    function open(pk, term) {
      var list = pk.querySelector(".pk-list"), r = search(pk, term), items = [], html = "";
      function row(p) {
        items.push({ place: p });
        var where = [p.country !== r.country ? p.country : "", p.unlocode || p.iata || ""].filter(Boolean).join(" · ");
        html += '<div class="pk-it' + (p.status === "to_confirm" ? " tc" : "") + '" role="option" data-i="' + (items.length - 1) + '">' +
          '<span class="k">' + esc(p.key) + "</span>" +
          (p.name && norm(p.name) !== norm(p.key) ? '<span class="n">' + esc(p.name) + "</span>" : "") +
          '<span class="t">' + kind(p) + (p.status === "to_confirm" ? ' <span class="tcf">(to confirm)</span>' : "") + "</span>" +
          (where ? '<span class="c">' + esc(where) + "</span>" : "") + "</div>";
      }
      r.mine.forEach(row);
      if (r.other.length) { html += '<div class="pk-dv">Other countries</div>'; r.other.forEach(row); }
      var t = (term || "").trim(), hits = exactly(t), same = sameOf(t, hits), w = want(pk);
      var taken = same && (pk.__picks || []).some(function (p) { return p.key === same.key; });
      // an airport (a seaport on Air) is never offered as the other kind: 'SAN' typed in the port box
      var other = same && same.type === (w === "airport" ? "seaport" : "airport");
      if (t && same && w && !fits(pk, same) && !taken && !other) {
        // 3: in the master but without this box's flag
        items.push({ mark: same });
        html += '<div class="pk-it pk-act" role="option" data-i="' + (items.length - 1) + '">+ Mark “' + esc(same.key) +
          "” as " + (w === "airport" ? "an airport" : "a port") + "</div>";
      } else if (t && !hits.length) {
        items.push({ add: t });
        html += '<div class="pk-it pk-act" role="option" data-i="' + (items.length - 1) + '">+ Add ' +
          (w || "location") + " “" + esc(t) + "”</div>";
      } else if (!items.length && !t && r.country) {
        html += '<div class="pk-none">No places for this country yet</div>';
      }
      if (!html) { close(pk); return; }
      list.innerHTML = html;
      list.hidden = false;
      // wider than its box when the rows need it; kept inside the window
      list.style.left = "";
      list.style.right = "";
      if (list.getBoundingClientRect().right > document.documentElement.clientWidth - 8) {
        list.style.left = "auto";
        list.style.right = "0";
      }
      pk.classList.add("open");
      pk.__items = items;
      pk.__act = t && items.length ? 0 : -1;
      mark(pk);
    }
    function mark(pk) {
      var list = pk.querySelector(".pk-list");
      list.querySelectorAll(".pk-it").forEach(function (el) {
        el.classList.toggle("act", +el.getAttribute("data-i") === pk.__act);
      });
      var act = list.querySelector(".pk-it.act");
      if (act && act.scrollIntoView) { act.scrollIntoView({ block: "nearest" }); }
    }
    function close(pk) {
      var list = pk.querySelector(".pk-list");
      if (list) { list.hidden = true; list.innerHTML = ""; }
      pk.classList.remove("open");
      pk.__items = [];
      pk.__act = -1;
    }

    // ---- picking
    function setCountry(pk, p) {
      // 1e: the first place of a side sets its country when the place's is known
      if (sideOf(pk) === "pod") { return; }
      var box = countryBox(pk);
      if (!box || !p || !p.country || p.country === "Unknown" || box.value === p.country) { return; }
      if (!Array.prototype.some.call(box.options || [], function (o) { return o.value === p.country; })) { return; }
      box.value = p.country;
      box.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function take(pk, p) {
      var had = (pk.__picks || []).filter(function (x) { return !x.legacy; }).length;
      var picks = maxOf(pk) === 1 ? [p] : (pk.__picks || []).filter(function (x) {
        return !x.legacy && x.key !== p.key;           // a place picked replaces the text a route held
      }).concat([p]).slice(0, maxOf(pk));
      pk.__picks = picks;
      pk.__editing = false;
      if (picks[0] === p && (maxOf(pk) === 1 || !had)) { setCountry(pk, p); }
      save(pk);
      render(pk);
      if (picks.length < maxOf(pk)) { focusQ(pk, true); }
    }
    function choose(pk, i) {
      var it = (pk.__items || [])[i];
      if (!it) { return; }
      close(pk);
      if (it.add) { addForm(pk, it.add); return; }
      if (it.mark) {
        var c = copy(it.mark);
        c.mark = want(pk);
        if (c.mark === "airport") { c.airport = 1; } else { c.port = 1; }
        take(pk, c);
        return;
      }
      take(pk, copy(it.place));
    }
    function remove(pk, i) {
      (pk.__picks || []).splice(i, 1);
      if (maxOf(pk) === 1) { pk.__editing = true; }
      save(pk);
      render(pk);
      focusQ(pk, true);
    }

    // ---- '+ Add location': a small form under the picker, To confirm
    function titleCase(s) {
      return String(s || "").toLowerCase().replace(/(^|[\s\-'(])([a-zà-ÿ])/g, function (m, a, b) { return a + b.toUpperCase(); });
    }
    function addForm(pk, term) {
      var f = pk.closest(".pk-f"), box = f.querySelector(".pk-addf"), d = data();
      if (!box) { box = document.createElement("div"); box.className = "pk-addf"; f.appendChild(box); }
      var cbox = countryBox(pk), country = cbox ? cbox.value : "", w = want(pk);
      var countries = d.countries.length ? d.countries.slice() : ["Unknown"];
      if (country && countries.indexOf(country) < 0) { countries.unshift(country); }
      box.innerHTML = '<label>Key<input type="text" class="pk-ak" value="' + esc(term.toUpperCase()) + '"></label>' +
        '<label>Name<input type="text" class="pk-an" value="' + esc(titleCase(term)) + '"></label>' +
        '<label>Country<select class="pk-ac">' + countries.map(function (c) {
          return "<option" + (c === (country || "Unknown") ? " selected" : "") + ">" + esc(c) + "</option>";
        }).join("") + "</select></label>" +
        '<label class="ck"><input type="checkbox" class="pk-ap"' + (w === "port" ? " checked" : "") + "> Port</label>" +
        '<label class="ck"><input type="checkbox" class="pk-aa"' + (w === "airport" ? " checked" : "") + "> Airport</label>" +
        '<button type="button" class="button primary small-button pk-add-ok">Add</button>' +
        '<button type="button" class="button small-button pk-add-no">Cancel</button>';
      box.hidden = false;
      box.__pk = pk;
      var key = box.querySelector(".pk-ak");
      key.focus();
      key.select();
    }
    function addClose(box) { box.hidden = true; box.innerHTML = ""; }
    // RD1c: a place added here is pending - "new: KEY" - and written with
    // the case (or the route) when it is saved; leaving the page writes
    // nothing. The master's naming rules answer at once (/api/place-rules):
    // a refusal stays in the small form, a mode word taken off or a state's
    // comma put in is said under the picker, a spelling on file is that place
    function addNote(pk, notes) {
      var f = pk.closest(".pk-f"), el = f ? f.querySelector(".pk-addnote") : null;
      if (!f) { return; }
      if (!notes.length) { if (el) { el.remove(); } return; }
      if (!el) { el = document.createElement("div"); el.className = "pk-addnote muted"; f.appendChild(el); }
      el.textContent = notes.map(function (n) { return n.charAt(0).toUpperCase() + n.slice(1) + "."; }).join(" ");
    }
    function addOk(box) {
      var pk = box.__pk, input = box.querySelector(".pk-ak");
      var key = input.value.trim().replace(/\s+/g, " ").toUpperCase();
      if (!pk || !key) { input.focus(); return; }
      var d = data(), known = d.byKey[key] || sameOf(key, exactly(key));
      var country = box.querySelector(".pk-ac").value, err = box.querySelector(".pk-adderr");
      function finish(k, place, notes) {
        var nameBox = box.querySelector(".pk-an"), name = nameBox.value.trim();
        if (k !== key && name === titleCase(key)) { name = titleCase(k); }   // the name was the text the rules changed
        var p = place ? copy(place) : prep({
          key: k, name: name, country: country,
          port: box.querySelector(".pk-ap").checked ? 1 : 0, airport: box.querySelector(".pk-aa").checked ? 1 : 0,
          status: "to_confirm", type: "city", isNew: true, aliases: [] });
        if (!place) { added.push(p); d.places.push(p); d.byKey[p.key] = p; }   // the page's other pickers find it too
        addClose(box);
        take(pk, p);
        addNote(pk, notes || []);
      }
      if (known) { finish(key, known, []); return; }
      fetch("/api/place-rules?text=" + encodeURIComponent(key) + "&country=" + encodeURIComponent(country),
            { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (r) {
          if (!r) { finish(key, null, []); return; }              // the server asks again on save
          if (r.error) {
            if (!err) { err = document.createElement("em"); err.className = "err pk-adderr"; box.appendChild(err); }
            err.textContent = r.notes.concat([r.error]).join("; ");
            input.focus();
            return;
          }
          var place = r.found ? (d.byKey[r.found] || sameOf(r.found, exactly(r.found))) : null;
          finish(r.key || key, place, r.notes || []);
        })
        .catch(function () { finish(key, null, []); });
    }

    // ---- the Port of discharge follows the route's mode: Air lists
    // airports (Airport of destination); a switch drops the picks of the
    // other kind (a place that is both stays; text a route held stays)
    function modeChanged(pk) {
      var w = want(pk), f = pk.closest(".pk-f");
      if (pk.__want === w) { return; }               // FCL to LCL: the same kind of place
      pk.__want = w;
      var air = w === "airport", label = f ? f.querySelector(".pk-label[data-air]") : null;
      if (label) { label.textContent = label.getAttribute(air ? "data-air" : "data-sea"); }
      var d = data(), before = (pk.__picks || []).map(function (p) { return p.key + (p.mark || ""); }).join("|");
      pk.__picks = (pk.__picks || []).filter(function (p) {
        if (p.legacy) { return true; }
        if (p.mark) { var base = d.byKey[p.key]; return !!base && (air ? isAir(base) : isPort(base)); }
        return air ? (isAir(p) || !isPort(p)) : (isPort(p) || !isAir(p));
      }).map(function (p) { return p.mark && d.byKey[p.key] ? copy(d.byKey[p.key]) : p; });
      if (pk.__picks.map(function (p) { return p.key + (p.mark || ""); }).join("|") !== before) { save(pk); }
      close(pk);
      render(pk);
    }

    // ---- wiring: the page's pickers, the ones a refresh or a new route brings
    function setup(pk) {
      if (pk.__ready) { return; }
      pk.__ready = true;
      pk.__init = { value: part(pk, "pk-value").value, fresh: (part(pk, "pk-new") || {}).value || "",
                    mark: (part(pk, "pk-mark") || {}).value || "" };
      pk.__want = want(pk);
      pk.__editing = false;
      load(pk);
      close(pk);
      render(pk);
    }
    function wire(root) {
      (root || document).querySelectorAll(".picker").forEach(setup);
    }
    function refresh(root) {
      (root || document).querySelectorAll(".picker").forEach(function (pk) {
        if (!pk.__ready) { setup(pk); return; }
        pk.__want = want(pk);
        load(pk);
        close(pk);
        render(pk);
      });
    }
    function reset(root) {
      (root || document).querySelectorAll(".picker").forEach(function (pk) {
        if (!pk.__ready || !pk.__init) { return; }
        part(pk, "pk-value").value = pk.__init.value;
        if (part(pk, "pk-new")) { part(pk, "pk-new").value = pk.__init.fresh; }
        if (part(pk, "pk-mark")) { part(pk, "pk-mark").value = pk.__init.mark; }
        pk.__editing = false;
        pk.__want = want(pk);
        var f = pk.closest(".pk-f"), label = f ? f.querySelector(".pk-label[data-air]") : null;
        if (label) { label.textContent = label.getAttribute(pk.__want === "airport" ? "data-air" : "data-sea"); }
        load(pk);
        close(pk);
        render(pk);
        var add = f ? f.querySelector(".pk-addf") : null;
        if (add) { addClose(add); }
      });
    }
    // a place put in from outside the picker (the register page's place
    // choices); a key not on the list stays as its text, to be added To
    // confirm when the case is registered
    function setKeys(pk, keys) {
      if (!pk || !pk.classList || !pk.classList.contains("picker")) { return false; }
      setup(pk);
      part(pk, "pk-value").value = (keys || []).slice(0, maxOf(pk)).join("|");
      load(pk);
      pk.__editing = false;
      if (pk.__picks[0]) { setCountry(pk, pk.__picks[0]); }
      save(pk);
      render(pk);
      return true;
    }
    // a key the picker answers itself: Enter in it always, Esc while its
    // list or its small form is open
    function owns(target, key) {
      if (!target || !target.closest) { return false; }
      if (target.closest(".pk-addf")) { return key === "Enter" || key === "Escape"; }
      var pk = target.closest(".picker");
      if (!pk) { return false; }
      return key === "Enter" || (key === "Escape" && pk.classList.contains("open"));
    }

    document.addEventListener("input", function (e) {
      // typing in the box is the picker's own (the page reacts to a pick)
      var q = e.target.closest ? e.target.closest(".pk-q") : null;
      if (q) { e.stopPropagation(); open(q.closest(".picker"), q.value); }
    }, true);
    document.addEventListener("focusin", function (e) {
      var q = e.target.closest ? e.target.closest(".pk-q") : null;
      if (q && !q.closest(".picker").__quiet) { open(q.closest(".picker"), q.value); }
    });
    document.addEventListener("focusout", function (e) {
      var q = e.target.closest ? e.target.closest(".pk-q") : null;
      if (!q) { return; }
      var pk = q.closest(".picker");
      setTimeout(function () {
        if (pk.contains(document.activeElement)) { return; }
        close(pk);
        if (maxOf(pk) === 1 && pk.__editing && (pk.__picks || []).length) { pk.__editing = false; render(pk); }
      }, 150);
    });
    document.addEventListener("keydown", function (e) {
      var t = e.target;
      if (!t || !t.closest) { return; }
      var add = t.closest(".pk-addf");
      if (add) {
        if (e.key === "Enter") { e.preventDefault(); addOk(add); }
        else if (e.key === "Escape") { e.preventDefault(); var back = add.__pk; addClose(add); if (back) { focusQ(back, true); } }
        return;
      }
      var q = t.closest(".pk-q");
      if (!q) { return; }
      var pk = q.closest(".picker"), items = pk.__items || [], n = items.length, isOpen = pk.classList.contains("open");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!isOpen) { open(pk, q.value); return; }
        if (!n) { return; }
        pk.__act = pk.__act < 0 ? (e.key === "ArrowDown" ? 0 : n - 1) : (pk.__act + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
        mark(pk);
      } else if (e.key === "Enter") {
        // takes the highlighted place; never sends the card's form
        e.preventDefault();
        if (isOpen && n) { choose(pk, pk.__act >= 0 ? pk.__act : 0); }
      } else if (e.key === "Escape") {
        if (isOpen) { e.preventDefault(); close(pk); }
      } else if (e.key === "Backspace" && !q.value && maxOf(pk) > 1 && (pk.__picks || []).length) {
        remove(pk, pk.__picks.length - 1);
      }
    });
    document.addEventListener("mousedown", function (e) {
      var it = e.target.closest ? e.target.closest(".pk-it") : null;
      if (it) {
        e.preventDefault();                            // the box keeps its focus
        choose(it.closest(".picker"), +it.getAttribute("data-i"));
        return;
      }
      var box = e.target.closest ? e.target.closest(".pk-box") : null;
      if (box && !e.target.closest(".pk-x") && !e.target.closest(".pk-q")) {
        var pk = box.closest(".picker");
        e.preventDefault();
        if (maxOf(pk) === 1 && (pk.__picks || []).length && !pk.__editing) { pk.__editing = true; render(pk); }
        focusQ(pk);
      }
    });
    document.addEventListener("click", function (e) {
      var x = e.target.closest ? e.target.closest(".pk-x") : null;
      if (x) { e.preventDefault(); remove(x.closest(".picker"), +x.getAttribute("data-i")); return; }
      var ok = e.target.closest ? e.target.closest(".pk-add-ok") : null;
      if (ok) { addOk(ok.closest(".pk-addf")); return; }
      var no = e.target.closest ? e.target.closest(".pk-add-no") : null;
      if (no) { var add = no.closest(".pk-addf"), back = add.__pk; addClose(add); if (back) { focusQ(back, true); } }
    });
    document.addEventListener("change", function (e) {
      var el = e.target;
      if (!el || !el.name || !/service_mode$/.test(el.name)) { return; }
      var scope = el.closest(".reg-route") || el.closest("form") || document;
      var pickers = Array.prototype.slice.call(scope.querySelectorAll('.picker[data-side="pod"]'));
      if (el.closest('.reg-route[data-route-key="a"]')) {
        // route A's mode is also the mode of a route B left without one
        document.querySelectorAll('.reg-route:not([data-route-key="a"])').forEach(function (b) {
          var own = b.querySelector('[name$="service_mode"]');
          if (own && !own.value) { pickers = pickers.concat(Array.prototype.slice.call(b.querySelectorAll('.picker[data-side="pod"]'))); }
        });
      }
      pickers.forEach(function (pk) { if (pk.__ready) { modeChanged(pk); } });
    });
    // Cancel on the case page puts back what the route had
    document.addEventListener("unit:close", function (e) {
      if (e.detail && e.detail.reset && e.detail.unit) { reset(e.detail.unit); }
    });
    // a draft restored on the register page: the pickers read their fields again
    document.addEventListener("draft:filled", function () { refresh(document); });

    wire(document);
    return { wire: wire, refresh: refresh, reset: reset, set: setKeys, owns: owns };
  }());
  window.OppPickers = Pickers;

  // ------------------------------------------------- quantity parsing ---
  // Same suggestion the server makes; the user still confirms it.
  document.addEventListener("click", function (e) {
    if (!e.target || e.target.id !== "parse-qty") { return; }
    var raw = $("qty_raw") ? $("qty_raw").value : "";
    if (!raw) { return; }
    var air = /air\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(?:kgs?)?(?:\s|\||$)/i.exec(raw);
    var lcl = /lcl\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(?:cbm|m3)?(?:\s|\||$)/i.exec(raw);
    var fcl = /([0-9]+)\s*[xX]\s*([0-9]{2})\s*'?\s*(GP|HQ|HC|OT|FR|RF|NOR)?/.exec(raw);
    if (air) { setValue("air_weight_kg", air[1]); }
    if (lcl) { setValue("lcl_cbm", lcl[1]); }
    if (fcl) {
      var suffix = (fcl[3] || "").toUpperCase();
      if (suffix === "HC") { suffix = "HQ"; }
      if (!suffix) { suffix = fcl[2] === "45" ? "HQ" : "GP"; }
      setValue("container_qty", fcl[1]);
      setSelect("container_type", fcl[2] + suffix);
    }
  });

  function setValue(name, value) {
    var el = document.querySelector('[name="' + name + '"]');
    if (el && !el.value) { el.value = value; }
  }
  function setSelect(name, value) {
    var el = document.querySelector('[name="' + name + '"]');
    if (!el || el.value) { return; }
    Array.prototype.forEach.call(el.options, function (opt) {
      if (opt.value === value) { el.value = value; }
    });
  }

  // --------------------------------------------------- status dialogue --
  // Lost, Cancelled and Dropped have their own vocabularies: an option
  // says which status it belongs to, and only that half is offered. With
  // scripting off both halves show and the server refuses the wrong one.
  function filterReasons(select, status) {
    if (!select) { return; }
    Array.prototype.forEach.call(select.options, function (option) {
      var owner = option.getAttribute("data-status");
      var show = !owner || owner === status;
      option.hidden = !show;
      option.disabled = !show;
      if (!show && option.selected) { select.value = ""; }
    });
  }

  // The tracking rows carry a status menu and a reason menu side by side.
  document.addEventListener("change", function (evt) {
    var pick = evt.target;
    if (!pick || !pick.classList || !pick.classList.contains("status-pick")) { return; }
    var box = pick.closest ? pick.closest("form") : null;
    var scope = pick.parentNode || box;
    var reason = scope && scope.querySelector('select[name="closed_reason"]');
    filterReasons(reason, pick.value);
    // the tracking rows (v2) show the reason only once Lost / Dropped is picked
    if (reason && reason.classList.contains("js-hide")) {
      reason.hidden = ["Lost", "Cancelled", "Dropped"].indexOf(pick.value) < 0;
    }
  });

  // ------------------------------------------- list columns by drag ---
  // Native drag and drop on the headings; the new order goes to the
  // server (setting list_columns) so every browser sees the same list.
  // The Enquiries list (v2) calls it on the headings it has just drawn.
  function bindColumnDrag(heads) {
    var form = $("column-order-form");
    if (!form || !heads.length) { return; }
    var dragged = null;

    function clearMarks() {
      heads.forEach(function (h) { h.classList.remove("drop-before", "drop-after"); });
    }
    function after(th, evt) {
      var box = th.getBoundingClientRect();
      return evt.clientX > box.left + box.width / 2;
    }
    heads.forEach(function (th) {
      on(th, "dragstart", function (evt) {
        dragged = th;
        th.classList.add("dragging");
        evt.dataTransfer.effectAllowed = "move";
        evt.dataTransfer.setData("text/plain", th.getAttribute("data-col"));
      });
      on(th, "dragend", function () { th.classList.remove("dragging"); clearMarks(); });
      on(th, "dragover", function (evt) {
        if (!dragged || dragged === th) { return; }
        evt.preventDefault();
        clearMarks();
        th.classList.add(after(th, evt) ? "drop-after" : "drop-before");
      });
      on(th, "drop", function (evt) {
        if (!dragged || dragged === th) { return; }
        evt.preventDefault();
        var order = heads.map(function (h) { return h.getAttribute("data-col"); });
        var from = order.indexOf(dragged.getAttribute("data-col"));
        var to = order.indexOf(th.getAttribute("data-col"));
        var moving = order.splice(from, 1)[0];
        if (from < to) { to -= 1; }
        order.splice(to + (after(th, evt) ? 1 : 0), 0, moving);
        // hidden columns keep their place after the shown ones
        var all = $("column-order").value.split(",");
        var hidden = all.filter(function (c) { return order.indexOf(c) < 0; });
        $("column-order").value = order.concat(hidden).join(",");
        form.submit();
      });
    });
  }

  // --------------------------------------------------- enquiries v2 ---
  // (2026-09-25) The server renders every row of the view once,
  // into #enq-pool; this groups, sorts, filters, searches and draws the
  // board from their data attributes. The view, group, sort and filters
  // are read from the query string too, and written back to it; the last
  // group and sort are remembered in this browser. Nothing is fetched.
  (function () {
    var pool = $("enq-pool");
    if (!pool) { return; }
    var T = JSON.parse($("enq-text").textContent);
    var main = document.querySelector("main.enquiries-page");
    var rows = Array.prototype.slice.call(pool.querySelectorAll("tbody tr.enq-row"));
    var cards = {};
    document.querySelectorAll("#enq-kpool .kcard").forEach(function (c) { cards[c.getAttribute("data-id")] = c; });
    var head = pool.querySelector("thead");
    pool.hidden = true;
    var empty = document.querySelector(".enq-empty");
    if (empty) { empty.hidden = true; }

    var LIMIT = 15, BOARD_LIMIT = 12, KEY = "opplist.enquiries";
    // SC1: a legacy row (stored Dropped) is its own group, Legacy, after Dropped
    var STATUS_ORDER = ["New", "Quoted", "Won", "Lost", "Cancelled", "Dropped", "Legacy"];
    // v31: grouped by status, the cases another of our offices handles make
    // one last group, collapsed - the home office is only copied on them
    var OTHERS = "others", HOME = (document.body && document.body.dataset.home) || "";
    var STATUS_TONE = { New: ["grey", "steel"], Quoted: ["accent", "accent"], Won: ["green", "green"],
                        Lost: ["rose", "rose"], Cancelled: ["grey", "steel"], Dropped: ["grey", "steel"],
                        Legacy: ["grey", "steel"] };
    var MODE_TONE = { Air: "navy", "Sea-Air": "navy", FCL: "accent", LCL: "accent", RORO: "accent", "Break Bulk": "accent",
                      Truck: "amber", "Cross-border Truck": "amber" };
    var SORT_ALIAS = { received: "received_on", last: "last_event_on", last_activity_on: "last_event_on" };
    var NUMERIC = ["opp_no", "qty", "request_group"];
    var DIRECTIONS = ["US Export", "US Import", "US Domestic", "Cross-trade", "To Confirm"];
    var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December"];

    var P = new URLSearchParams(location.search);
    var stored = {};
    try { stored = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { stored = {}; }
    function sortKey(v) { v = SORT_ALIAS[v] || v; return v || ""; }
    function defaultDir(key) { return key === "requester" ? "asc" : "desc"; }
    var urlDir = P.get("dir") || "";
    var sort = sortKey(P.get("sort")) || stored.sort || "received_on";
    var latam = P.get("latam") || "";
    var state = {
      // L1: the No reply view groups by the requester's country unless asked otherwise
      group: ["status", "requester", "country", "month", "mode", "office"].indexOf(P.get("group")) >= 0 ? P.get("group")
        : (T.view === "silent" ? "country" : (stored.group === "country" ? "status" : (stored.group || "status"))),
      sort: sort,
      dir: (urlDir === "asc" || urlDir === "desc") ? urlDir
        : (P.get("sort") ? defaultDir(sort) : (stored.dir || defaultDir(sort))),
      board: P.get("board") === "1",
      showDropped: P.get("dropped") === "1",
      search: "",
      filters: {
        latam: (latam === "1" || latam.toUpperCase() === "Y") ? "Y" : (latam.toUpperCase() === "N" ? "N" : ""),
        mode: P.get("mode") || "",
        // 'dir' is the sort direction; a direction's name there is the filter
        direction: P.get("direction") || (DIRECTIONS.indexOf(urlDir) >= 0 ? urlDir : ""),
        month: P.get("month") || "",
        requester: P.get("requester") || "",
        scope: P.get("scope") || "In",
        status: P.get("status") || "",
        owner: P.get("owner") || ""
      }
    };

    function attr(r, name) { return r.getAttribute("data-" + name) || ""; }
    function droppedAllowed() {
      return T.view === "closed" || T.view === "legacy" || !!T.q || state.filters.status === "Dropped"
        || state.filters.status === "Legacy" || state.showDropped;
    }
    // the rows kept out until asked for: Dropped, and (SC1) Legacy as before
    function away(r) { var s = attr(r, "status"); return s === "Dropped" || s === "Legacy"; }
    function passesFilters(r) {
      var f = state.filters;
      if (f.scope !== "all" && attr(r, "scope") !== f.scope) { return false; }
      if (f.latam && attr(r, "latam") !== f.latam) { return false; }
      // v33: a mode or a direction matches any route of the OPP
      if (f.mode && (attr(r, "modes") || "|" + attr(r, "mode") + "|").indexOf("|" + f.mode + "|") < 0) { return false; }
      if (f.direction && (attr(r, "directions") || "|" + attr(r, "direction") + "|").indexOf("|" + f.direction + "|") < 0) { return false; }
      if (f.month && attr(r, "month") !== f.month) { return false; }
      if (f.requester && attr(r, "requester") !== f.requester) { return false; }
      if (f.status && attr(r, "status") !== f.status) { return false; }
      if (f.owner && attr(r, "owner") !== f.owner) { return false; }
      if (state.search && attr(r, "search").indexOf(state.search.toLowerCase()) < 0) { return false; }
      return true;
    }
    function sortValue(r, key) {
      if (key === "received_on") { return attr(r, "received"); }
      if (key === "last_event_on") { return attr(r, "last"); }
      if (key === "requester") { return attr(r, "requester-name").toLowerCase(); }
      return attr(r, "s-" + key).toLowerCase();
    }
    function compare(a, b) {
      var key = state.sort, x = sortValue(a, key), y = sortValue(b, key), c;
      if (!x && y) { return 1; } else if (x && !y) { return -1; }
      else if (NUMERIC.indexOf(key) >= 0) { c = (parseFloat(x) || 0) - (parseFloat(y) || 0); }
      else { c = x < y ? -1 : (x > y ? 1 : 0); }
      if (state.dir === "desc") { c = -c; }
      if (c) { return c; }
      var ra = attr(a, "received"), rb = attr(b, "received");
      if (ra !== rb) { return ra < rb ? 1 : -1; }
      return (parseInt(attr(b, "s-opp_no"), 10) || 0) - (parseInt(attr(a, "s-opp_no"), 10) || 0);
    }
    function groupKey(r) {
      if (state.group === "status") { return attr(r, "office") ? OTHERS : attr(r, "status"); }
      if (state.group === "office") { return attr(r, "office") || HOME; }
      if (state.group === "requester") { return attr(r, "requester-name") || "?"; }
      if (state.group === "country") { return attr(r, "country") || "Country unknown"; }
      if (state.group === "month") { return attr(r, "month") || "(none)"; }
      return attr(r, "mode") || "(none)";
    }
    function groupOrder(groups) {
      var keys = Object.keys(groups);
      if (state.group === "status") { return STATUS_ORDER.concat([OTHERS]).filter(function (k) { return groups[k]; }); }
      if (state.group === "office") {
        return keys.sort(function (a, b) {
          return ((b === HOME) - (a === HOME)) || (groups[b].length - groups[a].length) || a.localeCompare(b);
        });
      }
      if (state.group === "month") { return keys.sort().reverse(); }
      return keys.sort(function (a, b) { return (groups[b].length - groups[a].length) || a.localeCompare(b); });
    }
    function meta(key) {
      if (state.group === "status" && key === OTHERS) { return { dot: "steel", band: "steel", hint: T.hints.others || "" }; }
      if (state.group === "office" && key !== HOME) { return { dot: "steel", band: "steel", hint: "" }; }
      if (state.group === "status") {
        var t = STATUS_TONE[key] || ["grey", "steel"];
        return { dot: t[0], band: t[1], hint: T.hints[key] || "" };
      }
      if (state.group === "mode") { return { dot: MODE_TONE[key] || "grey", band: "steel", hint: "" }; }
      return { dot: "grey", band: "steel", hint: "" };
    }
    function title(key) {
      if (state.group === "status" && key === OTHERS) { return T.others_title; }
      if (state.group === "month" && /^\d{4}-\d{2}$/.test(key)) {
        return MONTHS[parseInt(key.slice(5), 10) - 1] + " " + key.slice(0, 4);
      }
      return key;
    }
    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }
    function fmt(template, n) { return template.replace("{n}", n); }

    function groupCard(key, list) {
      var m = meta(key);
      var card = document.createElement("section");
      card.className = "card enq-group";
      card.setAttribute("data-group-key", key);
      card.innerHTML = '<div class="g-head band-' + m.band + '"><span class="dot tone-' + m.dot + '"></span>' +
        "<h2>" + esc(title(key)) + '</h2><span class="count">' + list.length + "</span>" +
        '<span class="hint">' + esc(m.hint) + '</span><span class="chev">&#9660;</span></div>';
      var table = document.createElement("table");
      table.className = "tbl";
      table.appendChild(head.cloneNode(true));
      var body = document.createElement("tbody");
      list.forEach(function (r, i) {
        var copy = r.cloneNode(true);
        if (i >= LIMIT) { copy.classList.add("more"); }
        body.appendChild(copy);
      });
      table.appendChild(body);
      card.appendChild(table);
      if (list.length > LIMIT) {
        var more = document.createElement("div");
        more.className = "showall";
        more.innerHTML = '<a href="#">' + esc(fmt(T.show_all, list.length)) + '</a> <span class="muted">(' +
          LIMIT + " of " + list.length + ")</span>";
        more.querySelector("a").addEventListener("click", function (evt) {
          evt.preventDefault();
          card.classList.toggle("expanded");
          this.textContent = card.classList.contains("expanded") ? fmt(T.show_first, LIMIT) : fmt(T.show_all, list.length);
        });
        card.appendChild(more);
      }
      card.querySelector(".g-head").addEventListener("click", function () { card.classList.toggle("closed"); });
      // collapsed by default, but not on Other offices, where it is the page
      if (state.group === "status" && key === OTHERS && T.view !== "others") { card.classList.add("closed"); }
      return card;
    }
    function boardColumn(key, list) {
      var m = meta(key);
      var col = document.createElement("div");
      col.className = "col";
      col.innerHTML = '<div class="col-h"><span class="dot tone-' + m.dot + '"></span>' + esc(title(key)) +
        '<span class="count">' + list.length + "</span></div>";
      list.slice(0, BOARD_LIMIT).forEach(function (r) {
        var c = cards[attr(r, "id")];
        if (c) { col.appendChild(c.cloneNode(true)); }
      });
      if (list.length > BOARD_LIMIT) {
        var more = document.createElement("div");
        more.className = "showall board-more";
        more.textContent = fmt(T.more, list.length - BOARD_LIMIT);
        col.appendChild(more);
      }
      return col;
    }

    function writeUrl() {
      var q = new URLSearchParams();
      q.set("view", T.view);
      if (T.q) { q.set("q", T.q); }
      q.set("group", state.group);
      q.set("sort", state.sort);
      q.set("dir", state.dir);
      Object.keys(state.filters).forEach(function (k) {
        var v = state.filters[k];
        if (v && !(k === "scope" && v === "In")) { q.set(k, k === "latam" && v === "Y" ? "1" : v); }
      });
      if (state.showDropped) { q.set("dropped", "1"); }
      if (state.board) { q.set("board", "1"); }
      try { history.replaceState(null, "", location.pathname + "?" + q.toString()); } catch (e) { /* file: */ }
      try {
        // the No reply view's own grouping is not remembered for the other views
        if (T.view !== "silent") { localStorage.setItem(KEY, JSON.stringify({ group: state.group, sort: state.sort, dir: state.dir })); }
      }
      catch (e) { /* storage off: nothing to remember */ }
      document.querySelectorAll(".enquiries-page .views a[data-view]").forEach(function (a) {
        var v = new URLSearchParams(q.toString());
        v.set("view", a.getAttribute("data-view"));
        a.href = location.pathname + "?" + v.toString();
      });
    }

    function render() {
      ["status", "requester", "country", "month", "mode", "office"].forEach(function (g) { main.classList.toggle("group-" + g, state.group === g); });
      main.classList.toggle("board", state.board);
      var seen = rows.filter(passesFilters);
      var allowDropped = droppedAllowed();
      var dropped = seen.filter(away).length;
      var list = allowDropped ? seen : seen.filter(function (r) { return !away(r); });
      list.sort(compare);
      var groups = {};
      list.forEach(function (r) { var k = groupKey(r); (groups[k] = groups[k] || []).push(r); });
      var keys = groupOrder(groups);
      var out = $("enq-list"), board = $("enq-board");
      out.innerHTML = "";
      board.innerHTML = "";
      keys.forEach(function (k) {
        out.appendChild(groupCard(k, groups[k]));
        board.appendChild(boardColumn(k, groups[k]));
      });
      if (!keys.length) {
        out.innerHTML = '<section class="card enq-group"><div class="nothing">' + esc(T.nothing) + "</div></section>";
      }
      $("enq-shown").textContent = list.length;
      var line = $("enq-dropped");
      // as before (2026-09-18): 'N dropped hidden · show', and
      // 'including N dropped' once they are asked for; nothing on Closed
      if (T.view === "closed" || T.view === "legacy") { line.innerHTML = ""; }
      else if (!allowDropped) {
        line.innerHTML = dropped ? esc(fmt(T.dropped_hidden, dropped)) + ' &middot; <a href="#" data-dropped="1">' + esc(T.show) + "</a>" : "";
      } else {
        line.innerHTML = dropped ? esc(fmt(T.including, dropped)) : "";
      }
      document.querySelectorAll(".enquiries-page [data-group]").forEach(function (b) {
        b.classList.toggle("on", b.getAttribute("data-group") === state.group);
      });
      document.querySelectorAll(".enquiries-page [data-sort]").forEach(function (b) {
        var on = b.getAttribute("data-sort") === state.sort;
        b.classList.toggle("on", on);
        b.setAttribute("data-arrow", on ? (state.dir === "asc" ? "↑" : "↓") : "");
      });
      document.querySelectorAll(".enquiries-page .chip[data-filter]").forEach(function (b) {
        b.classList.toggle("on", state.filters[b.getAttribute("data-filter")] === b.getAttribute("data-value"));
      });
      document.querySelectorAll(".enquiries-page details.chip-menu[data-menu]").forEach(function (d) {
        var k = d.getAttribute("data-menu"), v = state.filters[k], label = "";
        if (v) {
          var pick = d.querySelector('[data-filter="' + k + '"][data-value="' + v.replace(/"/g, "") + '"]');
          label = pick ? (pick.getAttribute("data-label") || v) : v;
        }
        d.querySelector("summary .v").textContent = k === "scope" ? (v || "In") : label;
        d.querySelector("summary").classList.toggle("on", !!v && !(k === "scope" && v === "In"));
      });
      var active = $("enq-active");
      active.innerHTML = "";
      [["status", "Status"], ["owner", "Owner"]].forEach(function (pair) {
        var v = state.filters[pair[0]];
        if (v) { active.innerHTML += '<button type="button" class="chip on" data-clear="' + pair[0] + '">' + esc(pair[1] + ": " + v) + " &times;</button>"; }
      });
      var d = state.filters.direction;
      if (d && ["US Export", "US Import", "Cross-trade"].indexOf(d) < 0) {
        active.innerHTML += '<button type="button" class="chip on" data-clear="direction">' + esc(d) + " &times;</button>";
      }
      $("enq-v-list").classList.toggle("on", !state.board);
      $("enq-v-board").classList.toggle("on", state.board);
      writeUrl();
      bindColumnDrag(Array.prototype.slice.call(out.querySelectorAll("section.enq-group:first-child thead th[data-col]")));
    }

    document.querySelector(".enquiries-page").addEventListener("click", function (evt) {
      var b = evt.target.closest("[data-group],[data-sort],[data-filter],[data-clear],[data-dropped]");
      if (!b) { return; }
      if (b.hasAttribute("data-group")) { state.group = b.getAttribute("data-group"); }
      else if (b.hasAttribute("data-sort")) {
        var s = b.getAttribute("data-sort");
        state.dir = state.sort === s ? (state.dir === "asc" ? "desc" : "asc") : defaultDir(s);
        state.sort = s;
      } else if (b.hasAttribute("data-filter")) {
        var k = b.getAttribute("data-filter"), v = b.getAttribute("data-value");
        if (b.classList.contains("chip")) { state.filters[k] = state.filters[k] === v ? "" : v; }
        else { state.filters[k] = v; var menu = b.closest("details"); if (menu) { menu.open = false; } }
      } else if (b.hasAttribute("data-clear")) { state.filters[b.getAttribute("data-clear")] = ""; }
      else if (b.hasAttribute("data-dropped")) { evt.preventDefault(); state.showDropped = b.getAttribute("data-dropped") === "1"; }
      render();
    });
    on($("enq-v-list"), "click", function () { state.board = false; render(); });
    on($("enq-v-board"), "click", function () { state.board = true; render(); });
    on($("enq-q"), "input", function () {
      var v = this.value.trim();
      state.search = (T.q && v === T.q) ? "" : v;
      render();
    });
    render();
  })();

  // Scope: a direct customer's case defaults to Out (v9). Only
  // until she picks a scope herself - then the form keeps her choice.
  var scope = $("scope");
  on(scope, "change", function () { scope.setAttribute("data-auto", "no"); });
  on($("requester"), "change", function () {
    if (!scope || scope.getAttribute("data-auto") !== "yes") { return; }
    var picked = this.options[this.selectedIndex];
    scope.value = (picked && picked.getAttribute("data-type") === "Direct Customer") ? "Out" : "In";
  });

  // Whose rate was used: only the picker for the chosen answer is shown.
  function rateUsedShow(box) {
    var chosen = box.querySelector("input[name=rate_used]:checked");
    var value = chosen ? chosen.value : "";
    box.querySelectorAll("[data-for]").forEach(function (el) {
      el.hidden = el.getAttribute("data-for") !== value;
    });
  }
  function rateUsedAll(root) { (root || document).querySelectorAll(".rate-used").forEach(rateUsedShow); }
  document.addEventListener("change", function (e) {
    if (e.target && e.target.name === "rate_used" && e.target.closest) {
      var box = e.target.closest(".rate-used");
      if (box) { rateUsedShow(box); }
    }
  });
  rateUsedAll(document);

  // ------------------------------ places the body could not decide on ---
  // The mail named several destinations (or origins), so nothing was
  // pre-filled. One click puts the chosen one in route A's place picker
  // (batch 23); a place that is not on the location list yet stays as its
  // text and is added To confirm when the case is registered.
  document.querySelectorAll(".body-conflict .choice").forEach(function (btn) {
    on(btn, "click", function () {
      var picker = $(btn.getAttribute("data-target"));
      if (picker && window.OppPickers) { window.OppPickers.set(picker, [btn.getAttribute("data-place")]); }
    });
  });

  // ------------------------------------------------- 4.3 saving safely ---
  // A registration is a couple of minutes of typing. Three things protect it:
  // a draft in this browser, a warning when leaving with it unsaved, and the
  // green banner on the case page that says it did save.
  var DRAFT_PREFIX = "opplist:draft:";
  var DRAFT_EVERY = 2000;

  function draftKey(form) { return DRAFT_PREFIX + form.getAttribute("data-draft-key"); }

  function formValues(form) {
    var out = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.type === "submit" || el.type === "button") { return; }
      if (el.type === "checkbox" || el.type === "radio") {
        if (el.checked) { (out[el.name] = out[el.name] || []).push(el.value); }
      } else if (el.value) {
        out[el.name] = el.value;
      }
    });
    return out;
  }

  function fillForm(form, values) {
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || !(el.name in values)) { return; }
      var saved = values[el.name];
      if (el.type === "checkbox" || el.type === "radio") {
        el.checked = Array.isArray(saved) && saved.indexOf(el.value) >= 0;
      } else if (!Array.isArray(saved)) {
        el.value = saved;
      }
    });
  }

  function store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }
  function read(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function drop(key) {
    try { window.localStorage.removeItem(key); } catch (e) { /* nothing to do */ }
  }

  // The case page clears the draft after a save, so the next new enquiry
  // starts empty.
  var savedBanner = document.querySelector("[data-clear-draft]");
  if (savedBanner) {
    drop(DRAFT_PREFIX + savedBanner.getAttribute("data-clear-draft"));
  }

  var draftForm = document.querySelector("form[data-draft-key]");
  if (draftForm) {
    var key = draftKey(draftForm);
    var mustChoose = draftForm.getAttribute("data-must-choose") === "1";
    var saving = false;
    var typedSomething = false;

    // Offer what is in the browser only when this form is a fresh one: after
    // a rejected save the server already sent her own values back.
    var offer = $("draft-offer");
    var stored = read(key);
    if (offer && stored && !mustChoose && !document.querySelector(".error-summary")) {
      try {
        var parsed = JSON.parse(stored);
        if (parsed && parsed.saved_at && Object.keys(parsed.values || {}).length) {
          offer.hidden = false;
          on($("draft-restore"), "click", function () {
            // 15c: the register page makes the lines and routes the draft had first
            document.dispatchEvent(new CustomEvent("draft:fill", { detail: { values: parsed.values } }));
            fillForm(draftForm, parsed.values);
            offer.hidden = true;
            applyState();
            document.dispatchEvent(new CustomEvent("draft:filled"));
          });
          on($("draft-discard"), "click", function () {
            drop(key);
            offer.hidden = true;
          });
        }
      } catch (e) { drop(key); }
    }

    draftForm.addEventListener("input", function () { typedSomething = true; });
    window.setInterval(function () {
      if (saving) { return; }
      var values = formValues(draftForm);
      if (!Object.keys(values).length) { return; }
      store(key, JSON.stringify({ saved_at: Date.now(), values: values }));
    }, DRAFT_EVERY);

    draftForm.addEventListener("submit", function () { saving = true; });

    // 13g: the register form sends once. After the first submit its buttons
    // (Save, and the duplicate panel's) read 'Saving...' and do nothing, and
    // Ctrl+Enter does nothing, until the page changes. A submit another
    // check stopped (a box with no number, 13c) unlocks it again.
    var sent = false;
    var sendButtons = function () {
      // DD1: the block's buttons sit outside the form (form="enquiry-form")
      var inside = Array.prototype.slice.call(draftForm.querySelectorAll("button[type=submit], button:not([type])"));
      var outside = draftForm.id ? Array.prototype.slice.call(
        document.querySelectorAll('button[form="' + draftForm.id + '"]')) : [];
      return inside.concat(outside);
    };
    draftForm.addEventListener("submit", function (e) {
      if (sent) { e.preventDefault(); e.stopImmediatePropagation(); return; }
      sent = true;
      window.setTimeout(function () {
        if (e.defaultPrevented) { sent = false; saving = false; return; }
        sendButtons().forEach(function (b) {
          if (!b.hasAttribute("data-label")) { b.setAttribute("data-label", b.textContent); }
          b.disabled = true;
          b.textContent = draftForm.getAttribute("data-saving") || "Saving...";
        });
      }, 0);
    });
    window.addEventListener("pageshow", function (e) {
      if (!e.persisted) { return; }
      sent = false;
      saving = false;
      sendButtons().forEach(function (b) {
        b.disabled = false;
        if (b.hasAttribute("data-label")) { b.textContent = b.getAttribute("data-label"); }
      });
    });

    // Leaving with the duplicate panel open saves nothing, so say so. The
    // browser shows its own wording; ours is on the page above.
    window.addEventListener("beforeunload", function (e) {
      if (saving) { return; }
      if (mustChoose || typedSomething) {
        e.preventDefault();
        e.returnValue = "";
        return "";
      }
    });

    // Escape must not look like a way out of the duplicate panel.
    if (mustChoose) {
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          var banner = document.getElementById("already");
          if (banner) { banner.scrollIntoView({ block: "center" }); }
        }
      });
      var cancel = draftForm.querySelector("a.button");
      if (cancel) {
        on(cancel, "click", function (e) {
          if (!window.confirm("This case has not been created yet. Leave "
                              + "without saving it?")) {
            e.preventDefault();
          }
        });
      }
    }
  }

  // ------------------------------------------------------- keyboard -----
  // Esc closes an inline block; Ctrl+Enter saves the form under the cursor.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      document.querySelectorAll(".inline-add, .inline-new").forEach(function (el) {
        el.hidden = true;
      });
    }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      var form = $("enquiry-form");
      if (form) { form.requestSubmit(); }
    }
    // 13e: '/' puts the cursor in the top bar's search box - unless the
    // cursor is already in a field, where it types a slash; Esc leaves it
    var box = $("tb-search-q");
    if (box && e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      var el = document.activeElement;
      var typing = el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
      if (!typing) { e.preventDefault(); box.focus(); box.select(); }
    }
    if (box && e.key === "Escape" && document.activeElement === box) { box.blur(); }
  });

  applyStates(document);

  // ------------------------------------------------- tracking v2 ----
  // Search, the filter chips, the sort chips, the two halves and 'Show
  // all' work over the rows already on the page; nothing
  // is fetched. The count pills and the tiles follow the visible rows.
  (function () {
    var page = document.querySelector("main.tracking-page");
    if (!page) { return; }
    var filters = { latam: false, air: false, ocean: false, office: false };
    var query = "";
    var sortBy = "days";
    var STEP_RANK = { replied: 0, "fu1 due": 1, "fu2 due": 2, decide: 3 };
    function rows() { return page.querySelectorAll("tbody tr[data-search]"); }
    function matches(tr) {
      var d = tr.dataset;
      if (query && d.search.indexOf(query) < 0) { return false; }
      if (filters.latam && d.latam !== "Y") { return false; }
      if ((filters.air || filters.ocean) && !((filters.air && d.air === "Y") || (filters.ocean && d.ocean === "Y"))) { return false; }
      if (filters.office && d.office !== "Y") { return false; }
      return true;
    }
    function apply() {
      var perCard = {};
      rows().forEach(function (tr) {
        var ok = matches(tr);
        tr.classList.toggle("filtered", !ok);
        var extra = tr.classList.contains("extra") && !tr.classList.contains("shown");
        tr.hidden = !ok || extra;
        var card = tr.closest(".group");
        if (card) { perCard[card.id] = (perCard[card.id] || 0) + (ok ? 1 : 0); }
      });
      page.querySelectorAll("[data-count]").forEach(function (el) {
        var key = el.getAttribute("data-count");
        if (key in perCard || filters.latam || filters.air || filters.ocean || filters.office || query) {
          el.textContent = perCard[key] || 0;
        }
      });
      page.querySelectorAll("[data-tile]").forEach(function (el) {
        var key = el.getAttribute("data-tile");
        if (key in perCard) { el.textContent = perCard[key]; }
      });
    }
    function sortRows() {
      page.querySelectorAll(".group tbody").forEach(function (body) {
        var list = Array.prototype.slice.call(body.querySelectorAll("tr[data-search]"));
        list.sort(function (a, b) {
          if (sortBy === "days") { return (+b.dataset.days) - (+a.dataset.days); }
          if (sortBy === "requester") { return a.dataset.requester < b.dataset.requester ? -1 : (a.dataset.requester > b.dataset.requester ? 1 : 0); }
          var ra = STEP_RANK[a.dataset.step], rb = STEP_RANK[b.dataset.step];
          ra = ra === undefined ? 9 : ra; rb = rb === undefined ? 9 : rb;
          return ra - rb || ((+b.dataset.days) - (+a.dataset.days));
        });
        list.forEach(function (tr) { body.appendChild(tr); });
      });
    }
    var search = document.getElementById("trk-search");
    on(search, "input", function () { query = search.value.trim().toLowerCase(); apply(); });
    page.querySelectorAll(".chip[data-filter]").forEach(function (chip) {
      on(chip, "click", function () {
        var key = chip.getAttribute("data-filter");
        filters[key] = !filters[key];
        chip.classList.toggle("on", filters[key]);
        apply();
      });
    });
    page.querySelectorAll(".chip[data-sort]").forEach(function (chip) {
      on(chip, "click", function () {
        sortBy = chip.getAttribute("data-sort");
        page.querySelectorAll(".chip[data-sort]").forEach(function (c) { c.classList.toggle("on", c === chip); });
        sortRows();
      });
    });
    page.querySelectorAll(".view[data-view]").forEach(function (btn) {
      on(btn, "click", function () {
        var view = btn.getAttribute("data-view");
        page.querySelectorAll(".view[data-view]").forEach(function (b) { b.classList.toggle("on", b === btn); });
        page.querySelectorAll("[data-half]").forEach(function (half) {
          half.hidden = view !== "all" && half.getAttribute("data-half") !== view;
        });
      });
    });
    page.querySelectorAll("[data-show-all]").forEach(function (link) {
      on(link, "click", function (e) {
        e.preventDefault();
        var card = document.getElementById(link.getAttribute("data-show-all"));
        if (!card) { return; }
        card.querySelectorAll("tr.extra").forEach(function (tr) { tr.classList.add("shown"); });
        var box = link.closest(".showall");
        if (box) { box.hidden = true; }
        apply();
      });
    });
    page.querySelectorAll("[data-select-shown]").forEach(function (box) {
      on(box, "change", function () {
        var card = document.getElementById(box.getAttribute("data-select-shown"));
        if (!card) { return; }
        card.querySelectorAll("tr").forEach(function (tr) {
          var pick = tr.querySelector("input.decide-pick");
          if (pick && !tr.hidden) { pick.checked = box.checked; }
        });
      });
    });
    // a link to a folded card (a tile, a flash, a bookmark) opens it
    function openHash() {
      var target = location.hash && document.getElementById(location.hash.slice(1));
      if (target && target.tagName === "DETAILS") { target.open = true; }
    }
    window.addEventListener("hashchange", openHash);
    openHash();
    apply();
  }());

  // The case page's header card: it sticks under the top
  // bar by CSS; this condenses it once the page has scrolled past the
  // card's own height. The sentinel's marker is moved that far down the
  // page and watched; when it passes the top bar the card gets
  // 'condensed', and loses it again at the top. The card's flow footprint
  // is kept with a margin so the cards below do not jump, and the rail's
  // sticky offset follows the card's height through --head-h.
  (function () {
    var head = $("head"), marker = $("head-sentinel");
    if (!head || !marker || !("IntersectionObserver" in window)) { return; }
    var root = document.documentElement;
    var topbar = document.querySelector(".topbar");
    var topH = topbar ? topbar.offsetHeight : 0;
    var full = head.offsetHeight;
    head.classList.add("condensed");
    var small = head.offsetHeight;
    head.classList.remove("condensed");
    root.style.setProperty("--topbar-h", topH + "px");
    root.style.setProperty("--head-c", small + "px");
    marker.style.top = full + "px";
    function apply(on) {
      head.classList.toggle("condensed", on);
      head.style.marginBottom = on ? (full - head.offsetHeight + 14) + "px" : "";
      root.style.setProperty("--head-h", head.offsetHeight + "px");
    }
    apply(false);
    new IntersectionObserver(function (entries) {
      var e = entries[entries.length - 1];
      apply(!e.isIntersecting && e.boundingClientRect.top <= topH);
    }, { rootMargin: "-" + topH + "px 0px 0px 0px", threshold: 0 }).observe(marker);
  }());

  // The Cargo card (v33): while a line is being edited its
  // numbers and the totals follow what is typed; Save writes them. The
  // rule is cargo.normalise / routes.totals in Python.
  // 15a: wireCargo() runs again after the case page swaps the card in; a
  // line editor already wired (one kept open through the swap) is left as is.
  function wireCargo() {
    var table = document.querySelector("table.cargo-case");
    if (!table) { return; }
    var DIMS = { cm: 1, "in": 2.54, mm: 0.1, m: 100 }, WEIGHT = { kg: 1, lbs: 0.45359237, t: 1000 };
    function num(v) {
      var m = /^\s*(\d[\d.,]*)/.exec(String(v === null || v === undefined ? "" : v));
      if (!m) { return null; }
      var t = m[1].replace(/[.,]$/, "");
      t = /,\d{3}(\D|$)/.test(t) ? t.replace(/,/g, "") : t.replace(",", ".");
      var x = parseFloat(t);
      return isNaN(x) ? null : x;
    }
    function fmt(v, d) {
      return v === null || v === undefined || isNaN(v) ? "" :
        Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
    }
    // v34: the inputs are found in the row's own edit box, so the same
    // code serves the case page (form="cargo-line-<id>") and the register
    // form (cargo_<i>_<field>). A size with no unit gives no volume; a
    // weight or volume per piece with no pieces is not counted.
    function line(edit) {
      var get = function (k) { var el = edit.querySelector('[data-f="' + k + '"]'); return el ? (el.type === "checkbox" ? el.checked : el.value) : ""; };
      var pieces = num(get("pieces")), f = DIMS[get("dims_unit")] || null;
      var dims = [num(get("dim_l")), num(get("dim_w")), num(get("dim_h"))];
      var eachCbm = f && dims.every(function (x) { return x; }) ? dims[0] * f * dims[1] * f * dims[2] * f / 1e6 : null;
      var stated = num(get("cbm_value")), cbmEach = get("cbm_basis") === "each";
      var cbm = stated !== null ? (cbmEach ? (pieces ? stated * pieces : null) : stated)
        : (eachCbm !== null ? eachCbm * (pieces || 1) : null);
      var w = num(get("weight_value")), wu = get("weight_unit") || "kg", each = get("weight_basis") === "each";
      var kg = w === null || (each && !pieces) ? null : w * (WEIGHT[wu] || 1) * (each ? pieces : 1);
      return { pieces: pieces, kg: kg, cbm: cbm, counted: get("counted") !== false };
    }
    function totals() {
      var table = document.querySelector("table.cargo-case");
      if (!table) { return; }
      var mode = table.getAttribute("data-mode"), t = { pieces: 0, kg: 0, cbm: 0 };
      table.querySelectorAll("tr.c-row").forEach(function (row) {
        if (row.getAttribute("data-counted") !== "1") { return; }
        var times = parseInt(row.getAttribute("data-routes"), 10) || 1;
        t.pieces += num(row.getAttribute("data-pieces")) || 0;
        t.kg += (num(row.getAttribute("data-kg")) || 0) * times;
        t.cbm += (num(row.getAttribute("data-cbm")) || 0) * times;
      });
      var set = function (cls, text) { var el = document.querySelector("#cargo-totals " + cls); if (el) { el.textContent = text; } };
      if (!table.querySelector('tr.c-row[data-counted="1"]')) { return; }     // the typed boxes lead (13c)
      set(".t-pieces", t.pieces || "-");
      set(".t-gross", t.kg ? fmt(t.kg, 1) + " kg" : "-");
      set(".t-volume", t.cbm ? fmt(t.cbm, 3) + " cbm" : "-");
      if (AIR_MODES.indexOf(mode) >= 0) {
        var vk = t.cbm ? t.cbm * 1e6 / 6000 : null;
        set(".t-vol-kg", vk === null ? "-" : fmt(vk, 1) + " kg");
        set(".t-vol-f", vk === null ? "" : fmt(t.cbm, 3) + " x 166.67");
        set(".t-charge", t.kg || vk ? fmt(Math.max(vk || 0, t.kg), 1) + " kg" : "-");
        set(".t-charge-why", t.kg && vk !== null ? (vk > t.kg ? "volumetric is higher" : "gross is higher") : "");
      } else if (mode === "LCL") {
        set(".t-wm", fmt(Math.max(t.cbm, t.kg / 1000), 3));
        set(".t-wm-f", "max(" + fmt(t.cbm, 3) + " cbm, " + fmt(t.kg / 1000, 3) + " t)");
      }
    }
    document.querySelectorAll("tr.c-edit").forEach(function (edit) {
      if (edit.__wired) { return; }
      edit.__wired = true;
      var row = $("cargo-row-" + edit.id.replace("cargo-edit-", ""));
      edit.addEventListener("input", function () { recalc(); });
      edit.addEventListener("change", function () { recalc(); });
      edit.recalc = recalc;
      function recalc() {
        var l = line(edit);
        row.setAttribute("data-kg", l.kg === null ? "" : l.kg);
        row.setAttribute("data-cbm", l.cbm === null ? "" : l.cbm);
        row.setAttribute("data-pieces", l.pieces === null ? "" : l.pieces);
        row.setAttribute("data-counted", l.counted ? "1" : "0");
        row.classList.toggle("off", !l.counted);
        var kg = row.querySelector(".d-kg"), cbm = row.querySelector(".d-cbm");
        if (kg) { kg.textContent = l.kg === null ? "" : fmt(l.kg, 1) + " kg"; }
        if (cbm) { cbm.textContent = l.cbm === null ? "" : fmt(l.cbm, 3) + " cbm"; }
        totals();
      }
    });

    // The register form (v34): 'Remove' leaves an unsaved line out; an
    // answer to a check changes the line's inputs, saved with the case.
    table.querySelectorAll("[data-clear-row]").forEach(function (btn) {
      if (btn.__wired) { return; }
      btn.__wired = true;
      on(btn, "click", function () {
        var key = btn.getAttribute("data-clear-row"), row = $("cargo-row-" + key), edit = $("cargo-edit-" + key);
        if (edit) {
          edit.querySelectorAll("input").forEach(function (el) {
            if (el.type === "checkbox") { return; }
            if (el.type !== "hidden" || /_(deliver_to|dims_text)$/.test(el.name)) { el.value = ""; }
          });
          edit.hidden = true;
        }
        if (row) { row.hidden = true; row.setAttribute("data-counted", "0"); }
        totals();
      });
    });
    function addCode(el, code) {
      if (!el) { return; }
      var codes = el.value ? el.value.split(",") : [];
      if (codes.indexOf(code) < 0) { codes.push(code); }
      el.value = codes.sort().join(",");
    }
    document.querySelectorAll("#cargo-checks [data-answer]").forEach(function (btn) {
      if (btn.__wired) { return; }
      btn.__wired = true;
      on(btn, "click", function () {
        var action = btn.getAttribute("data-answer"), value = btn.getAttribute("data-value");
        var key = btn.getAttribute("data-line"), edit = key === "" ? null : $("cargo-edit-" + key);
        var field = function (k) { return edit ? edit.querySelector('[data-f="' + k + '"]') : null; };
        if (action === "count") {
          field("counted").checked = value === "1"; addCode(field("decided"), "repeat");
        } else if (action === "reading") {
          field("weight_value").value = value; field("weight_reading").value = "chosen";
        } else if (action === "unit") {
          field("dims_unit").value = value;
        } else if (action === "use_size") {
          field("cbm_value").value = ""; addCode(field("decided"), "stated_cbm");
        } else if (action === "keep") {
          addCode(field("decided"), value);
        } else if (action === "keep_case") {
          addCode($("cargo-decided"), value);
        }
        if (edit && edit.recalc) { edit.recalc(); }
        var box = btn.closest(".ck");
        box.classList.remove("ask"); box.classList.add("ok");
        box.querySelector(".ic").textContent = "\u2713";
        box.querySelector(".acts").textContent = btn.textContent + " - saved with the case";
        var count = $("cargo-check-count");
        if (count) {
          var n = document.querySelectorAll("#cargo-checks .ck.ask").length;
          count.textContent = "check " + n; count.hidden = !n;
        }
      });
    });
  }
  wireCargo();

  // 13c (2026-09-30): a number box with no number in it turns red with the
  // reason beside it, and its form is not sent (the server reads '2 a
  // month', '24 months', '25 big bags' the same way: a leading number).
  window.addEventListener("submit", function (e) {
    var f = e.target;
    if (!(f instanceof HTMLFormElement)) { return; }
    var bad = [];
    Array.prototype.forEach.call(f.elements, function (el) {
      if (!el.hasAttribute || !el.hasAttribute("data-num")) { return; }
      var holder = el.closest(".grp") || el, next = holder.nextElementSibling;
      if (next && next.classList.contains("num-err")) { next.remove(); }
      el.classList.remove("bad");
      if (el.value.trim() && !/^\s*\d/.test(el.value)) { bad.push(el); }
    });
    if (!bad.length) { return; }
    e.preventDefault();
    e.stopImmediatePropagation();
    bad.forEach(function (el) {
      el.classList.add("bad");
      var note = document.createElement("em");
      note.className = "err num-err";
      note.textContent = "needs a number";
      var after = el.closest(".grp") || el;
      after.parentNode.insertBefore(note, after.nextSibling);
    });
    var box = bad[0].closest("tr.c-edit, .cargo-add");
    if (box) { box.hidden = false; }
    bad[0].focus();
  }, true);

  // Units typed with the number: '25 t', '7605 lbs' set the unit beside the
  // box; '52 x 42 x 47 in' in the first size box fills the three and the unit.
  var WEIGHT_WORDS = [["lbs", /^(lbs?|pounds?)\b/i], ["t", /^(t|tons?|tonnes?|mt)\b/i], ["kg", /^(kgs?|kilos?|kilograms?)\b/i]];
  var VOLUME_WORDS = [["cft", /^(cft|cu\.?\s*ft\.?|ft3|cubic\s+f(ee|oo)t)\b/i], ["cbm", /^(cbm|m3|cubic\s+met(er|re)s?)\b/i]];
  function unitOf(text, table) {
    var m = /^\s*\d[\d.,]*\s*(.*)$/.exec(text || "");
    if (!m) { return null; }
    for (var i = 0; i < table.length; i++) { if (table[i][1].test(m[1])) { return table[i][0]; } }
    return null;
  }
  function numberOf(text) {
    var m = /^\s*(\d[\d.,]*)/.exec(text || "");
    if (!m) { return null; }
    var t = m[1].replace(/[.,]$/, "");
    t = /,\d{3}(\D|$)/.test(t) ? t.replace(/,/g, "") : t.replace(",", ".");
    var v = parseFloat(t);
    return isNaN(v) ? null : v;
  }
  document.addEventListener("input", function (e) {
    var el = e.target, name = el.name || "";
    if (/(^|_)weight_value$/.test(name)) {
      var unit = unitOf(el.value, WEIGHT_WORDS), sel = el.parentNode.querySelector('[data-f="weight_unit"]');
      if (unit && sel && sel.value !== unit) { sel.value = unit; sel.dispatchEvent(new Event("change", { bubbles: true })); }
    }
    if (/(^|_)dim_l$/.test(name)) {
      var m = /^\s*(\d[\d.,]*)\s*[x\u00d7*]\s*(\d[\d.,]*)\s*[x\u00d7*]\s*(\d[\d.,]*)\s*(cm|mm|m|in(?:ch(?:es)?)?|"|\u201d)?\s*$/i.exec(el.value);
      if (m) {
        var grp = el.parentNode, w = grp.querySelector('[data-f="dim_w"]'), h = grp.querySelector('[data-f="dim_h"]');
        var u = grp.querySelector('[data-f="dims_unit"]');
        el.value = m[1]; if (w) { w.value = m[2]; } if (h) { h.value = m[3]; }
        if (u && m[4]) { u.value = /^(in|"|\u201d)/i.test(m[4]) ? "in" : m[4].toLowerCase(); }
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    if (/(^|_)cbm_value$/.test(name)) {
      var vu = unitOf(el.value, VOLUME_WORDS), vs = el.parentNode.querySelector('[data-f="cbm_unit"]');
      if (vu && vs && vs.value !== vu) { vs.value = vu; vs.dispatchEvent(new Event("change", { bubbles: true })); }
    }
    var qty = el.getAttribute && el.getAttribute("data-qty-value");
    if (qty) {
      var table = qty === "gross" ? WEIGHT_WORDS : VOLUME_WORDS, us = document.querySelector('[data-qty-unit="' + qty + '"]');
      var typed = unitOf(el.value, table);
      if (typed && us) { us.value = typed; }
    }
  }, true);

  // The typed Gross weight and Volume boxes (13c): the conversion under the
  // box ('2,200 lbs = 997.9 kg'), and - with no counted line - the Cargo
  // card's totals follow them as they are typed; nothing typed shows '-'.
  // The boxes are looked up each time (15a: the card is swapped in again).
  function qtyUpdate() {
    var gross = document.querySelector('[data-qty-value="gross"]'), volume = document.querySelector('[data-qty-value="volume"]');
    if (!gross && !volume) { return; }
    var KG = { kg: 1, lbs: 0.45359237, t: 1000 }, CBM = { cbm: 1, cft: 0.0283168 };
    function fmt(v, d) { return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
    function read(input, key, table) {
      if (!input || !input.value.trim()) { return null; }
      var n = numberOf(input.value); if (n === null) { return null; }
      var unit = document.querySelector('[data-qty-unit="' + key + '"]');
      return { n: n, unit: unit ? unit.value : Object.keys(table)[0], base: n * (table[unit ? unit.value : Object.keys(table)[0]] || 1) };
    }
    function set(sel, text) { var el = document.querySelector("#cargo-totals " + sel); if (el) { el.textContent = text; } }
    var g = read(gross, "gross", KG), v = read(volume, "volume", CBM);
    var cg = document.querySelector('[data-qty-conv="gross"]'), cv = document.querySelector('[data-qty-conv="volume"]');
    if (cg) { cg.textContent = g && g.unit !== "kg" ? fmt(g.n, g.n % 1 ? 2 : 0) + " " + g.unit + " = " + fmt(g.base, 1) + " kg" : ""; }
    if (cv) { cv.textContent = v && v.unit !== "cbm" ? fmt(v.n, v.n % 1 ? 2 : 0) + " " + v.unit + " = " + fmt(v.base, 3) + " cbm" : ""; }
    if (document.querySelector('#cargo-lines tr.c-row[data-counted="1"]')) { return; }
    var mode = (document.querySelector("#cargo-lines") || { getAttribute: function () { return ""; } }).getAttribute("data-mode");
    set(".t-gross", g ? fmt(g.base, 1) + " kg" : "-");
    set(".t-volume", v ? fmt(v.base, 3) + " cbm" : "-");
    if (AIR_MODES.indexOf(mode) >= 0) {
      var vk = v ? v.base * 1e6 / 6000 : null;
      set(".t-vol-kg", vk === null ? "-" : fmt(vk, 1) + " kg");
      set(".t-vol-f", vk === null ? "" : fmt(v.base, 3) + " x 166.67");
      var ch = g || vk !== null ? Math.max(g ? g.base : 0, vk || 0) : null;
      set(".t-charge", ch === null ? "-" : fmt(ch, 1) + " kg");
      set(".t-charge-why", g && vk !== null ? (vk > g.base ? "volumetric is higher" : "gross is higher") : "");
    } else if (mode === "LCL") {
      var wm = g || v ? Math.max(v ? v.base : 0, g ? g.base / 1000 : 0) : null;
      set(".t-wm", wm === null ? "-" : fmt(wm, 3));
    }
  }
  ["input", "change"].forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      if (e.target && e.target.hasAttribute && (e.target.hasAttribute("data-qty-value") || e.target.hasAttribute("data-qty-unit"))) { qtyUpdate(); }
    });
  });
  if (document.querySelector('[data-qty-value]')) { qtyUpdate(); }


  // ------------------------------------------ the cargo line editor (15b) ---
  // Beside each number, what it comes to as it is typed ('= 121.9 x 121.9 x
  // 121.9 cm · 1.812 cbm each', '= 589.7 kg', '= 24 months'); while an
  // editor is open the totals strip and the checks follow the typing
  // (/cargo/preview, nothing written) and Cancel puts them back. Read into
  // lines opens the lines the reader finds; Use the size fills a line's
  // editor from the size the mail gives. Nothing is saved until Save line.
  // ---- the line editor's conversions (15b), shared with the register page (15c)
  var CG_KG = { kg: 1, lbs: 0.45359237, t: 1000 }, CG_CM = { cm: 1, "in": 2.54, mm: 0.1, m: 100 }, CG_CFT = 0.0283168;
  function cargoFmt(v, d) { return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function cargoVal(box, f) { var el = box.querySelector('[data-f="' + f + '"]'); return el ? el.value : ""; }
  function cargoConvert(box) {
    var pieces = numberOf(cargoVal(box, "pieces"));
    var size = box.querySelector('[data-conv="size"]');
    if (size) {
      var l = numberOf(cargoVal(box, "dim_l")), w = numberOf(cargoVal(box, "dim_w")), h = numberOf(cargoVal(box, "dim_h")), u = cargoVal(box, "dims_unit");
      if (l && w && h && CG_CM[u]) {
        var f = CG_CM[u], each = l * f * w * f * h * f / 1e6;
        size.textContent = (u !== "cm" ? "= " + cargoFmt(l * f, 1) + " x " + cargoFmt(w * f, 1) + " x " + cargoFmt(h * f, 1) + " cm · " : "= ")
          + cargoFmt(each, 3) + " cbm each" + (pieces && pieces > 1 ? " · " + cargoFmt(each * pieces, 3) + " cbm in all" : "");
      } else {
        size.textContent = "";          // a size without its unit: the picker itself asks (15c)
      }
    }
    var wt = box.querySelector('[data-conv="weight"]');
    if (wt) {
      var v = numberOf(cargoVal(box, "weight_value")), wu = cargoVal(box, "weight_unit") || "kg", basis = cargoVal(box, "weight_basis");
      if (v === null) { wt.textContent = ""; }
      else {
        var kg = v * (CG_KG[wu] || 1);
        if (basis === "each") {
          wt.textContent = pieces ? "= " + pieces + " x " + cargoFmt(kg, 1) + " kg = " + cargoFmt(kg * pieces, 1) + " kg" : "= " + cargoFmt(kg, 1) + " kg each - how many pieces?";
        } else if (basis === "container") {
          var q = numberOf(cargoVal(box, "container_qty"));
          wt.textContent = (wu !== "kg" ? "= " + cargoFmt(kg, 1) + " kg per container" : "") + (q && q > 1 ? " · " + cargoFmt(kg * q, 1) + " kg a shipment" : "");
        } else {
          wt.textContent = wu !== "kg" ? "= " + cargoFmt(kg, 1) + " kg" : "";
        }
      }
    }
    var vol = box.querySelector('[data-conv="volume"]');
    if (vol) {
      var c = numberOf(cargoVal(box, "cbm_value")), cu = cargoVal(box, "cbm_unit");
      vol.textContent = c !== null && cu === "cft" ? "= " + cargoFmt(c * CG_CFT, 3) + " cbm" : "";
    }
    var mo = box.querySelector('[data-conv="months"]');
    if (mo) {
      var m = /^\s*(\d[\d.,]*)\s*(years?|yrs?|y)\b/i.exec(cargoVal(box, "months"));
      mo.textContent = m ? "= " + cargoFmt(numberOf(m[1]) * 12, 0) + " months" : "";
    }
  }
  (function () {
    var table = $("cargo-lines");
    if (!table || !table.hasAttribute("data-preview")) { return; }
    var val = cargoVal, convert = cargoConvert;
    function isCargo(u) { return u && /^cargo-(line-|add|read-)/.test(u.getAttribute("data-unit") || ""); }
    function openCargo() {
      return Array.prototype.filter.call(document.querySelectorAll("[data-unit].editing"), isCargo);
    }
    var saved = null, timer = null;
    function preview(u) {
      var form = u.querySelector("form.u-form") || $(u.getAttribute("data-form"));
      var t = $("cargo-lines");
      if (!form || !t) { return; }
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetch(t.getAttribute("data-preview"), { method: "POST", body: new FormData(form), credentials: "same-origin",
                                                headers: { "Accept": "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (res) { var live = $("cargo-live"); if (res.ok && live) { live.innerHTML = res.html; } }, function () {});
      }, 300);
    }
    document.addEventListener("unit:open", function (e) {
      var u = e.detail.unit;
      if (!isCargo(u)) { return; }
      if (saved === null && $("cargo-live")) { saved = $("cargo-live").innerHTML; }
      u.querySelectorAll(".led").forEach(convert);
      if (u.hasAttribute("data-new")) { preview(u); }
    });
    document.addEventListener("unit:close", function (e) {
      if (!isCargo(e.detail.unit)) { return; }
      if (!openCargo().filter(function (x) { return x !== e.detail.unit; }).length && saved !== null && $("cargo-live")) {
        clearTimeout(timer);
        $("cargo-live").innerHTML = saved;
        saved = null;
      }
    });
    ["input", "change"].forEach(function (evt) {
      document.addEventListener(evt, function (e) {
        var box = e.target.closest ? e.target.closest(".led") : null;
        if (!box) { return; }
        convert(box);
        var u = box.closest("[data-unit]");
        if (u && u.classList.contains("editing")) { preview(u); }
      });
    });
    // Read into lines: the reader's lines as open editors, nothing saved
    document.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("[data-read-lines]") : null;
      if (!btn) { return; }
      var msg = btn.parentNode.querySelector(".read-msg");
      fetch(btn.getAttribute("data-read-lines"), { method: "POST", credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (msg) { msg.textContent = res.message || (res.n + " line" + (res.n === 1 ? "" : "s") + " read - check them and Save line"); }
          if (!res.n) { return; }
          var holder = $("cargo-new-lines");
          holder.insertAdjacentHTML("beforeend", res.html);
          var units = Array.prototype.slice.call(holder.querySelectorAll("[data-unit][data-new]:not(.editing)"));
          document.dispatchEvent(new CustomEvent("units:open", { detail: { units: units, focus: false } }));
          if (units[0]) { units[0].scrollIntoView({ block: "nearest" }); }
        });
    });
    // Use the size: the size the mail gives into the line's editor, the stated volume emptied
    document.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("[data-use-size]") : null;
      if (!btn) { return; }
      var u = document.querySelector('[data-unit="cargo-line-' + btn.getAttribute("data-use-size") + '"]');
      if (!u) { return; }
      document.dispatchEvent(new CustomEvent("units:open", { detail: { units: [u], focus: false } }));
      var parts = (btn.getAttribute("data-value") || "").split(","), box = u.querySelector(".led");
      function set(f, v) { var el = box.querySelector('[data-f="' + f + '"]'); if (el && v !== undefined) { el.value = v; } }
      if (!val(box, "pieces")) { set("pieces", parts[0]); }
      if (!val(box, "pack_word") && parts[1]) { set("pack_word", parts[1]); }
      if (!val(box, "pack_unit") && parts[6]) { set("pack_unit", parts[6]); }
      set("dim_l", parts[2]); set("dim_w", parts[3]); set("dim_h", parts[4]); set("dims_unit", parts[5] || "cm");
      set("cbm_value", "");
      box.querySelector('[data-f="dim_l"]').dispatchEvent(new Event("input", { bubbles: true }));
      u.scrollIntoView({ block: "nearest" });
    });
  }());

  // ------------------------------------------- the register page (15c) ---
  // The case page's cards, all open, one Register at the bottom. Lines and
  // routes are added and removed in the page (+ Add line, + Add route, a
  // route copying route A's origin and service); each number shows what it
  // comes to as it is typed; the totals, the checks, each line's own
  // question (a size's unit) and the rail follow the typing through
  // /enquiries/new/cargo/preview, which writes nothing. An answer to a
  // check changes the line's inputs and is saved with the case. Register
  // waits until every size has its unit.
  (function () {
    var form = $("enquiry-form");
    if (!form || !form.hasAttribute("data-preview")) { return; }
    var lines = $("reg-lines"), tpl = $("reg-line-tpl");
    function letter(n) { var out = ""; while (n > 0) { var r = (n - 1) % 26; out = String.fromCharCode(65 + r) + out; n = Math.floor((n - 1) / 26); } return out; }
    function routeBlocks() { return Array.prototype.slice.call(form.querySelectorAll(".reg-route")); }
    function lineBlocks() { return lines ? Array.prototype.slice.call(lines.querySelectorAll(".reg-line")) : []; }
    function routeField(block, name) { return block.querySelector('[name$="' + name + '"]'); }
    function modeOf() { var m = form.querySelector('select[name="service_mode"]'); return m ? m.value : ""; }

    function syncMode() {
      var card = $("cargo"), fcl = modeOf() === "FCL";
      if (!card) { return; }
      card.classList.toggle("is-fcl", fcl);
      card.querySelectorAll('select[data-f="weight_basis"]').forEach(function (sel) {
        if (fcl && sel.value === "each") { sel.value = "container"; }
        if (!fcl && sel.value === "container") { sel.value = "total"; }
      });
    }
    function ticks() {
      var rs = routeBlocks();
      form.querySelectorAll("[data-reg-ticks]").forEach(function (box) {
        var p = box.getAttribute("data-p"), was = {}, had = box.querySelectorAll('input[type="checkbox"]');
        had.forEach(function (c) { was[c.value] = c.checked; });
        var chosen = had.length || box.getAttribute("data-all") === "1" ? null
          : (box.getAttribute("data-chosen") || "").split(",").filter(Boolean);
        if (rs.length < 2) { box.innerHTML = ""; box.hidden = true; return; }
        var html = '<span class="k">Routes</span><input type="hidden" name="' + p + 'routes_present" value="1">';
        rs.forEach(function (b, i) {
          var key = b.getAttribute("data-route-key");
          var on = key in was ? was[key] : (chosen === null || chosen.indexOf(key) >= 0);
          html += '<label class="tick"><input type="checkbox" name="' + p + 'route" value="' + key + '"' + (on ? " checked" : "") + "> " + letter(i + 1) + "</label>";
        });
        box.innerHTML = html;
        box.hidden = false;
      });
    }
    function place(block, side) {
      // batch 23: a picker's places are 'A|B', read 'A or B'
      var loc = routeField(block, side + "_loc"), country = routeField(block, side + "_country");
      var keys = loc ? loc.value.split("|").filter(Boolean) : [];
      return keys.join(" or ") || (country && country.value) || "";
    }
    function title() {
      var h = $("reg-title"), rs = routeBlocks();
      if (!h || !rs.length) { return; }
      var dests = [];
      rs.forEach(function (b) { var d = place(b, "dest"); if (d && dests.indexOf(d) < 0) { dests.push(d); } });
      h.textContent = dests.length ? (place(rs[0], "origin") || "?") + " to " + dests.join(" \u00b7 ") : h.getAttribute("data-empty");
    }
    function renumber() {
      routeBlocks().forEach(function (b, i) { var l = b.querySelector(".r-letter"); if (l) { l.textContent = letter(i + 1); } });
      lineBlocks().forEach(function (b, i) { var n = b.querySelector(".ln-n"); if (n) { n.textContent = i + 1; } });
      var st = form.querySelector('[data-rail="routes"] .state');
      if (st) { st.textContent = routeBlocks().length; }
      ticks();
      title();
    }
    function railRequest() {
      var li = form.querySelector('[data-rail="request"]'), who = $("requester"), when = $("fld-received_on");
      var party = form.querySelector('[name="new_party_key"]');
      var ok = when && when.value && ((who && who.value) || (party && party.value.trim()));
      if (li) { li.classList.toggle("ok", !!ok); li.querySelector(".mark").textContent = ok ? "\u2713" : ""; }
    }
    function railRegas() {
      var picked = form.querySelector('input[name="status"]:checked'), st = form.querySelector('[data-rail="regas"] .state');
      if (st && picked) { st.textContent = picked.value; }
    }

    // ---- the totals, the checks and the rail as the lines stand
    var timer = null, blocking = false;
    function preview() {
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetch(form.getAttribute("data-preview"), { method: "POST", body: new FormData(form), credentials: "same-origin",
                                                   headers: { "Accept": "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (!res.ok) { return; }
            var live = $("cargo-live");
            if (live) { live.innerHTML = res.html; }
            Object.keys(res.asks || {}).forEach(function (k) {
              var box = form.querySelector('[data-asks="' + k + '"]');
              if (box) { box.innerHTML = res.asks[k]; }
            });
            var count = $("cargo-check-count");
            if (count) { count.textContent = "check " + res.count; count.hidden = !res.count; }
            var li = form.querySelector('[data-rail="cargo"]');
            if (li) {
              li.classList.toggle("ok", !res.rail); li.classList.toggle("miss", !!res.rail);
              li.querySelector(".mark").textContent = res.rail ? "" : "\u2713";
              li.querySelector(".state").textContent = res.rail || "";
            }
            blocking = !!res.blocking;
          }, function () {});
      }, 250);
    }

    // ---- lines
    function addLine(idx) {
      if (!lines || !tpl) { return null; }
      var used = lineBlocks().map(function (b) { return parseInt(b.getAttribute("data-idx"), 10); });
      var i = idx !== undefined ? idx : (used.length ? Math.max.apply(null, used) + 1 : 0);
      lines.insertAdjacentHTML("beforeend", tpl.innerHTML.replace(/__I__/g, i).replace(/__N__/g, used.length + 1));
      var block = lines.lastElementChild;
      syncMode();
      renumber();
      return block;
    }
    function addCode(el, code) {
      if (!el) { return; }
      var codes = el.value ? el.value.split(",") : [];
      if (codes.indexOf(code) < 0) { codes.push(code); }
      el.value = codes.sort().join(",");
    }
    function setUnit(sel, unit) {
      if (!sel) { return; }
      var blank = sel.querySelector('option[value=""]');
      if (blank && unit) { blank.remove(); }
      sel.value = unit;
      sel.classList.toggle("ask", !sel.value);
    }

    // ---- routes: + Add route copies route A's origin and service
    function addRoute(prefill, key) {
      var a = form.querySelector('.reg-route[data-route-key="a"]');
      if (!a) { return null; }
      var used = routeBlocks().map(function (b) { return parseInt(b.getAttribute("data-route-key"), 10); }).filter(function (n) { return !isNaN(n); });
      key = key !== undefined ? String(key) : String((used.length ? Math.max.apply(null, used) : 0) + 1);
      var c = a.cloneNode(true), from = a.querySelectorAll("[name]");
      c.setAttribute("data-route-key", key);
      c.querySelectorAll(".whence, .err").forEach(function (el) { el.remove(); });
      c.querySelectorAll("[name]").forEach(function (el, i) {
        var plain = el.name, src = from[i];
        el.name = "route_" + key + "_" + plain;
        // batch 23: the destination pickers start empty (and their places
        // added or marked); the origin's is route A's
        var fresh = /^(dest_country|dest_loc|new_dest_loc|dest_loc_new|dest_alt|pod|pod_new|pod_mark)$/.test(plain) || plain === "new_origin_loc";
        if (el.type === "checkbox" || el.type === "radio") { return; }
        el.value = fresh ? "" : (src ? src.value : "");
      });
      c.querySelectorAll("[id]").forEach(function (el) { el.id = "r" + key + "-" + el.id; });
      c.querySelectorAll("[data-toggle]").forEach(function (el) { el.setAttribute("data-toggle", "r" + key + "-" + el.getAttribute("data-toggle")); });
      c.querySelectorAll(".inline-new").forEach(function (el) { el.hidden = true; });
      c.querySelectorAll(".pk-addf").forEach(function (el) { el.remove(); });
      c.querySelectorAll(".picker").forEach(function (el) { el.classList.remove("open"); });
      var h = c.querySelector(".route-h");
      if (h) { h.insertAdjacentHTML("beforeend", ' <button type="button" class="link warn" data-remove-route>Remove route</button>'); }
      prefill = prefill || {};
      ["origin_country", "origin_loc", "dest_country", "dest_loc"].forEach(function (k) {
        if (prefill[k] === undefined) { return; }
        var el = routeField(c, "_" + k) || routeField(c, k);
        if (el) { el.value = prefill[k]; }
      });
      var all = routeBlocks();
      all[all.length - 1].insertAdjacentElement("afterend", c);
      if (typeof filterAll === "function") { filterAll(c); }
      if (window.OppPickers) { window.OppPickers.wire(c); }
      renumber();
      applyState(c);                               // S1: its places by its Service
      return c;
    }

    // ---- events
    form.addEventListener("click", function (e) {
      var t = e.target.closest ? e.target.closest("button, a") : null;
      if (!t) { return; }
      if (t.hasAttribute("data-add-line")) {
        var b = addLine();
        if (b) { var first = b.querySelector('[data-f="pieces"]'); if (first) { first.focus(); } }
        preview();
      } else if (t.hasAttribute("data-remove-line")) {
        var line = t.closest(".reg-line");
        if (line) { line.remove(); renumber(); preview(); }
      } else if (t.hasAttribute("data-add-route")) {
        var pre = t.hasAttribute("data-dest") ? { origin_loc: t.getAttribute("data-origin") || undefined,
          dest_loc: t.getAttribute("data-dest"), origin_country: t.getAttribute("data-origin-country") || undefined,
          dest_country: t.getAttribute("data-dest-country") || undefined } : {};
        var r = addRoute(pre);
        if (r) { r.scrollIntoView({ block: "nearest" }); }
        if (t.closest(".other-lanes")) { t.remove(); }
        preview();
      } else if (t.hasAttribute("data-remove-route")) {
        var route = t.closest(".reg-route");
        if (route && route.getAttribute("data-route-key") !== "a") { route.remove(); renumber(); preview(); }
      } else if (t.hasAttribute("data-answer")) {
        var action = t.getAttribute("data-answer"), value = t.getAttribute("data-value"), key = t.getAttribute("data-line");
        var block = key === "" ? null : $("reg-line-" + key);
        var field = function (k) { return block ? block.querySelector('[data-f="' + k + '"]') : null; };
        if (action === "count" && field("counted")) { field("counted").checked = value === "1"; addCode(field("decided"), "repeat"); }
        else if (action === "reading" && field("weight_value")) { field("weight_value").value = value; if (field("weight_reading")) { field("weight_reading").value = "chosen"; } }
        else if (action === "unit") { setUnit(field("dims_unit"), value); }
        else if (action === "use_size" && field("cbm_value")) { field("cbm_value").value = ""; addCode(field("decided"), "stated_cbm"); }
        else if (action === "keep") { addCode(field("decided"), value); }
        else if (action === "keep_case") { addCode($("cargo-decided"), value); }
        if (block) { block.querySelectorAll(".led").forEach(cargoConvert); }
        preview();
      } else if (t.hasAttribute("data-use-size")) {
        var target = $("reg-line-" + t.getAttribute("data-use-size"));
        if (!target) { return; }
        var parts = (t.getAttribute("data-value") || "").split(","), box = target.querySelector(".led");
        var set = function (f, v) { var el = box.querySelector('[data-f="' + f + '"]'); if (el && v !== undefined) { el.value = v; } };
        if (!cargoVal(box, "pieces")) { set("pieces", parts[0]); }
        if (!cargoVal(box, "pack_word") && parts[1]) { set("pack_word", parts[1]); }
        if (!cargoVal(box, "pack_unit") && parts[6]) { set("pack_unit", parts[6]); }
        set("dim_l", parts[2]); set("dim_w", parts[3]); set("dim_h", parts[4]);
        setUnit(box.querySelector('[data-f="dims_unit"]'), parts[5] || "cm");
        set("cbm_value", "");
        cargoConvert(box);
        target.scrollIntoView({ block: "nearest" });
        preview();
      } else if (t.hasAttribute("data-goto-line")) {
        var to = $("reg-line-" + t.getAttribute("data-goto-line"));
        if (to) { to.scrollIntoView({ block: "center" }); var f1 = to.querySelector("input:not([type=hidden])"); if (f1) { f1.focus(); } }
      }
    });
    ["input", "change"].forEach(function (evt) {
      form.addEventListener(evt, function (e) {
        var el = e.target, box = el.closest ? el.closest(".led") : null;
        if (box) { cargoConvert(box); }
        if (el.getAttribute && el.getAttribute("data-f") === "dims_unit") { setUnit(el, el.value); }
        if (el.name === "service_mode") { syncMode(); }
        if (el.closest && el.closest(".reg-route")) { title(); }
        if (el.name === "status") { railRegas(); }
        railRequest();
        if (box || (el.closest && el.closest(".reg-route")) || el.name === "service_mode") { preview(); }
      });
    });

    // ---- Register waits for every size's unit
    form.addEventListener("submit", function (e) {
      var missing = lineBlocks().filter(function (b) {
        var u = b.querySelector('select[data-f="dims_unit"]');
        var dims = ["dim_l", "dim_w", "dim_h"].every(function (f) { var el = b.querySelector('[data-f="' + f + '"]'); return el && el.value.trim(); });
        return u && !u.value && dims;
      });
      if (!missing.length) { return; }
      e.preventDefault();
      e.stopImmediatePropagation();
      missing.forEach(function (b) { var u = b.querySelector('select[data-f="dims_unit"]'); u.classList.add("ask", "bad"); });
      var msg = $("reg-state");
      if (msg) { msg.textContent = "Not registered: choose cm or in for the size of line " + missing[0].querySelector(".ln-n").textContent; msg.classList.add("warn"); }
      missing[0].scrollIntoView({ block: "center" });
    }, true);

    // ---- a draft restored brings its lines and routes back first
    document.addEventListener("draft:fill", function (e) {
      var values = (e.detail && e.detail.values) || {};
      var lineKeys = {}, routeKeys = {};
      Object.keys(values).forEach(function (name) {
        var m = /^cargo_(\d+)_/.exec(name); if (m) { lineKeys[m[1]] = true; }
        var r = /^route_(\d+)_/.exec(name); if (r) { routeKeys[r[1]] = true; }
      });
      Object.keys(routeKeys).forEach(function (k) { if (!form.querySelector('.reg-route[data-route-key="' + k + '"]')) { addRoute({}, k); } });
      Object.keys(lineKeys).forEach(function (k) { if (!$("reg-line-" + k)) { addLine(parseInt(k, 10)); } });
    });
    document.addEventListener("draft:filled", function () {
      form.querySelectorAll(".led").forEach(cargoConvert);
      syncMode(); renumber(); railRequest(); railRegas(); preview();
    });

    form.querySelectorAll(".led").forEach(cargoConvert);
    syncMode();
    renumber();
    railRequest();
  }());

  // ------------------------------------------------ case page v3 (15a) ---
  // Every data unit ([data-unit]: the Request card, each route, each cargo
  // line, each vendor row, Rate used, Special handling, each status action,
  // each timeline event) shows plain text and has its own Edit; Edit turns
  // only that unit into inputs with Save and Cancel inside it. A unit posts
  // only its own form, through fetch, with the JSON contract of 12d and 13c
  // ({ok} or {ok: false, errors}); a refusal keeps the unit open with what
  // was typed and the reason beside the field. Every other write on the
  // page (subjects, vendor rows, follow-ups, Release hold, suggestions,
  // More) goes the same way. After a write the page reads itself again and
  // swaps in every part that is not being edited; a unit in Edit (and an
  // open status action with something typed in it) is carried over as it
  // is, with its focus and typing. No write ever saves another unit.
  // Leaving with a changed unit asks first.
  (function () {
    var main = document.querySelector("main.case-page");
    if (!main || !main.querySelector("[data-unit]")) { return; }
    var casePath = location.pathname;

    function units() { return Array.prototype.slice.call(document.querySelectorAll("[data-unit]")); }
    function unitOf(el) { return el && el.closest ? el.closest("[data-unit]") : null; }
    function unitById(id) { return document.querySelector('[data-unit="' + id + '"]'); }
    function formOf(u) {
      if (!u) { return null; }
      return u.querySelector("form.u-form") || (u.getAttribute("data-form") ? $(u.getAttribute("data-form")) : null);
    }
    function snap(u) {
      var f = formOf(u);
      if (!f) { return ""; }
      var out = [];
      Array.prototype.forEach.call(f.elements, function (el) {
        if (!el.name || el.type === "submit" || el.type === "button") { return; }
        if (el.type === "checkbox" || el.type === "radio") { out.push(el.name + "=" + (el.checked ? el.value : "\u0000")); return; }
        out.push(el.name + "=" + el.value);
      });
      return out.join("\u0001");
    }
    function isOpen(u) { return u.classList.contains("editing"); }
    function isDirty(u) { return isOpen(u) && u.__base !== undefined && snap(u) !== u.__base; }
    function isAct(u) { return u.hasAttribute("data-act-form"); }
    // carried through a refresh: a unit in Edit, a status action only with typing in it
    function kept(u) { return isOpen(u) && (!isAct(u) || isDirty(u)); }
    function cardOf(u) { var c = u.closest("section.card[id]"); return c ? c.id : ""; }

    function clearErrors(u) {
      if (!u) { return; }
      u.querySelectorAll(".js-err").forEach(function (x) { x.remove(); });
      u.querySelectorAll(".bad").forEach(function (x) { x.classList.remove("bad"); });
    }
    function open(u, focus) {
      if (!u || isOpen(u)) { return; }
      if (isAct(u)) {
        var other = units().filter(function (x) { return x !== u && isAct(x) && isOpen(x); })[0];
        if (other && isDirty(other)) {
          // something typed in the open action: it stays, the new one does not open
          var first = other.querySelector(".u-edit input:not([type=hidden]), .u-edit select, .u-edit textarea");
          other.scrollIntoView({ block: "nearest" });
          if (first) { first.focus(); }
          return;
        }
        if (other) { close(other, true); }
      }
      u.classList.add("editing");
      u.querySelectorAll(".u-edit").forEach(function (x) { x.hidden = false; });
      u.querySelectorAll(".u-read").forEach(function (x) { x.hidden = true; });
      u.querySelectorAll(".rate-used").forEach(rateUsedShow);
      u.__base = u.hasAttribute("data-new") ? "" : snap(u);     // a line read from the mail: unsaved from the start
      document.dispatchEvent(new CustomEvent("unit:open", { detail: { unit: u } }));
      if (focus !== false) {
        var first = u.querySelector(".u-edit input:not([type=hidden]):not([disabled]), .u-edit select:not([disabled]), .u-edit textarea");
        if (first) { first.focus(); }
      }
      update();
    }
    function close(u, reset) {
      if (!u) { return; }
      var f = formOf(u);
      if (reset && f) {
        f.reset();
        var edit = u.querySelector("tr.c-edit");
        if (edit && edit.recalc) { edit.recalc(); }
        u.querySelectorAll(".rate-used").forEach(rateUsedShow);
        u.querySelectorAll(".won-route").forEach(wonRoute);
        if (u.querySelector('[data-qty-value]')) { qtyUpdate(); }
      }
      clearErrors(u);
      u.classList.remove("editing", "dirty");
      u.querySelectorAll(".u-edit").forEach(function (x) { x.hidden = true; });
      u.querySelectorAll(".u-read").forEach(function (x) { x.hidden = false; });
      delete u.__base;
      document.dispatchEvent(new CustomEvent("unit:close", { detail: { unit: u, reset: !!reset } }));
      if (reset && u.hasAttribute("data-new")) { u.remove(); }   // a read line cancelled: it goes
      update();
    }
    // another part of the page asks for units to be opened (Read into lines, Use the size)
    document.addEventListener("units:open", function (e) {
      (e.detail.units || []).forEach(function (u) { open(u, e.detail.focus); });
    });

    // the blue outline, the dot on the rail, the bottom bar (sample mark 1)
    function update() {
      var dirty = [];
      units().forEach(function (u) {
        var d = isDirty(u);
        u.classList.toggle("dirty", d);
        if (d) { dirty.push(u); }
      });
      document.querySelectorAll(".acts-row [data-act]").forEach(function (b) {
        var u = unitById("act-" + b.getAttribute("data-act"));
        b.classList.toggle("on", !!(u && isOpen(u)));
      });
      document.querySelectorAll(".rail .sections li.sec").forEach(function (li) {
        var link = li.querySelector("a[href^='#']"), key = link ? link.getAttribute("href").slice(1) : "";
        li.classList.toggle("dirty", dirty.some(function (u) { return cardOf(u) === key; }));
      });
      var bar = $("bb-saved");
      if (bar) {
        if (dirty.length) {
          var names = dirty.map(function (u) { return u.getAttribute("data-unit-name"); });
          bar.textContent = (bar.getAttribute("data-unsaved") || "Not saved yet: {}").replace("{}", names.join(", "));
          bar.classList.add("dirty");
        } else {
          bar.textContent = bar.getAttribute("data-saved") || bar.textContent;
          bar.classList.remove("dirty");
        }
      }
    }

    // Won: an unticked route asks for its lost reason (left empty, it keeps no result)
    function wonRoute(row) {
      var tick = row.querySelector('input[type="checkbox"]'), why = row.querySelector("select");
      if (tick && why) { why.disabled = tick.checked; if (tick.checked) { why.value = ""; } }
    }
    function isoDay(d) {
      return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
    }

    document.addEventListener("click", function (e) {
      var t = e.target.closest ? e.target : null;
      if (!t) { return; }
      var btn = t.closest("[data-edit]");
      if (btn) { open(unitOf(btn)); return; }
      btn = t.closest("[data-open-unit]");
      if (btn) {
        var u = unitById(btn.getAttribute("data-open-unit"));
        if (u) { open(u); u.scrollIntoView({ block: "nearest" }); }
        return;
      }
      btn = t.closest("[data-cancel]");
      if (btn) { close(unitOf(btn), true); return; }
      btn = t.closest(".acts-row [data-act]");
      if (btn) {
        var act = unitById("act-" + btn.getAttribute("data-act"));
        if (act) { if (isOpen(act)) { close(act, true); } else { open(act); } }
        return;
      }
      // On hold's quick picks: +2 weeks, +1 month, End of month
      btn = t.closest(".qk button");
      if (btn) {
        var box = btn.closest("label").querySelector(".hold-until"), d = new Date();
        if (btn.hasAttribute("data-days")) { d.setDate(d.getDate() + parseInt(btn.getAttribute("data-days"), 10)); }
        else if (btn.hasAttribute("data-months")) { d.setMonth(d.getMonth() + parseInt(btn.getAttribute("data-months"), 10)); }
        else { d = new Date(d.getFullYear(), d.getMonth() + 1, 0); if (isoDay(d) === isoDay(new Date())) { d = new Date(d.getFullYear(), d.getMonth() + 2, 0); } }
        if (box) { box.value = isoDay(d); box.dispatchEvent(new Event("input", { bubbles: true })); }
      }
    });
    document.addEventListener("change", function (e) {
      var row = e.target.closest ? e.target.closest(".won-route") : null;
      if (row) { wonRoute(row); }
    });
    ["input", "change"].forEach(function (evt) {
      document.addEventListener(evt, function (e) {
        var u = unitOf(e.target);
        if (u && isOpen(u)) { clearFieldError(e.target); update(); }
      });
    });
    function clearFieldError(el) {
      if (!el.classList || !el.classList.contains("bad")) { return; }
      el.classList.remove("bad");
      var holder = el.closest(".grp") || el, next = holder.nextElementSibling;
      if (next && next.classList.contains("js-err")) { next.remove(); }
    }
    // Enter in a one-line input saves its unit; Esc cancels it - except
    // in a place picker, whose Enter takes a place and whose Esc closes its
    // list (batch 23: Enter never saves the card from there)
    document.addEventListener("keydown", function (e) {
      var u = unitOf(e.target);
      if (!u || !isOpen(u)) { return; }
      if (window.OppPickers && window.OppPickers.owns(e.target, e.key)) { return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(u, true); return; }
      if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && e.target.tagName === "INPUT"
          && !/^(checkbox|radio|button|submit|file)$/.test(e.target.type)) {
        e.preventDefault();
        var f = formOf(u);
        if (f) { if (f.requestSubmit) { f.requestSubmit(); } else { f.submit(); } }
      }
    }, true);

    // ---- writes ----
    function markErrors(f, u, errors) {
      clearErrors(u || f);
      var first = null;
      (errors || []).forEach(function (err) {
        var el = null;
        Array.prototype.forEach.call(f.elements, function (x) {
          if (!el && err.field && x.name && x.type !== "hidden" &&
              (x.name === err.field || x.name.slice(-err.field.length - 1) === "_" + err.field)) { el = x; }
        });
        var note = document.createElement("em");
        note.className = "err js-err";
        note.textContent = el ? err.message : (err.label ? err.label + ": " : "") + err.message;
        if (el) {
          el.classList.add("bad");
          var anchor = el.closest(".grp") || el;
          anchor.parentNode.insertBefore(note, anchor.nextSibling);
        } else {
          var foot = f.querySelector(".foot") || f.querySelector("button[type=submit], button:not([type])");
          if (foot && foot.parentNode) { foot.parentNode.insertBefore(note, foot); } else { f.appendChild(note); }
        }
        first = first || el;
      });
      if (first) { first.focus(); }
    }
    function busy(f, on) {
      Array.prototype.forEach.call(f.querySelectorAll("button"), function (b) { b.disabled = on; });
    }
    function send(f, submitter) {
      var data = new FormData(f);
      if (submitter && submitter.name) { data.set(submitter.name, submitter.value); }
      var u = unitOf(f) || unitOf(submitter);
      busy(f, true);
      return fetch(f.getAttribute("action") || location.href, {
        method: "POST", body: data, credentials: "same-origin",
        headers: { "X-Save-First": "1", "Accept": "application/json" }
      }).then(function (r) {
        var type = r.headers.get("Content-Type") || "";
        if (type.indexOf("application/json") >= 0) {
          return r.json().then(function (res) { return { r: r, res: res }; });
        }
        return r.text().then(function (html) { return { r: r, html: html }; });
      }).then(function (x) {
        busy(f, false);
        if (x.res) {
          if (x.res.ok) { if (u && formOf(u) === f) { close(u, false); } return refresh(null); }
          markErrors(f, u, x.res.errors || [{ label: "Not saved", message: "refused (HTTP " + x.r.status + ")" }]);
          return;
        }
        if (!x.r.ok) {
          markErrors(f, u, [{ label: "Not saved", message: "refused (HTTP " + x.r.status + "); nothing was lost, try again" }]);
          return;
        }
        var url = new URL(x.r.url, location.href);
        if (url.pathname === casePath) {
          if (u && formOf(u) === f) { close(u, false); }
          history.replaceState(null, "", url.pathname + url.search + location.hash);
          return refresh(x.html);
        }
        location.href = x.r.url;                       // another page: go there; leaving asks first
      }, function () {
        busy(f, false);
        markErrors(f, u, [{ label: "Not saved", message: "the app did not answer; nothing was lost, try again" }]);
      });
    }
    // after 13c's check of the number boxes (window, capture): a form it
    // stopped never gets here
    window.addEventListener("submit", function (e) {
      var f = e.target;
      if (!(f instanceof HTMLFormElement) || !main.contains(f)) { return; }
      if ((f.method || "get").toLowerCase() !== "post" || f.hasAttribute("data-nav")) { return; }
      e.preventDefault();
      send(f, e.submitter || null);
    });

    // ---- reading the page again ----
    function selectionOf(el) {
      try { return el && typeof el.selectionStart === "number" ? [el.selectionStart, el.selectionEnd] : null; } catch (err) { return null; }
    }
    function swap(oldEl, newEl, inner) {
      if (!oldEl) { return; }
      if (!newEl) { oldEl.remove(); return; }
      if (inner) { oldEl.innerHTML = newEl.innerHTML; } else { oldEl.replaceWith(document.importNode(newEl, true)); }
    }
    function refresh(html) {
      var got = html ? Promise.resolve(html)
        : fetch(location.pathname + location.search, { credentials: "same-origin" }).then(function (r) { return r.text(); });
      return got.then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        var stackNew = doc.querySelector("main.case-page .stack");
        if (!stackNew) { location.reload(); return; }
        var active = document.activeElement, sel = selectionOf(active);
        // F1: the page's new messages join the toasts under the top bar
        if (window.OppToasts) { window.OppToasts.add(doc); }
        // header (kept as an element: the condensing watches it), rail, banners, bottom bar
        swap($("head"), doc.getElementById("head"), true);
        swap(document.querySelector(".rail-card"), doc.querySelector(".rail-card"));
        swap($("not-saved"), doc.getElementById("not-saved"));
        swap(document.querySelector('[data-part="saved-banner"]'), doc.querySelector('[data-part="saved-banner"]'));
        var more = document.querySelector(".bottombar .more-menu"), moreOpen = more && more.open;
        swap(document.querySelector(".bottombar .inner"), doc.querySelector(".bottombar .inner"), true);
        if (moreOpen && document.querySelector(".bottombar .more-menu")) { document.querySelector(".bottombar .more-menu").open = true; }
        // the cards: new ones, with every unit in Edit carried over as it is
        var stackOld = document.querySelector("main.case-page .stack");
        var fresh = document.importNode(stackNew, true);
        var openDetails = {};
        stackOld.querySelectorAll("details").forEach(function (d, i) { if (d.open) { openDetails[detailsKey(d, i)] = true; } });
        units().forEach(function (u) {
          if (!kept(u)) { return; }
          var twin = fresh.querySelector('[data-unit="' + u.getAttribute("data-unit") + '"]');
          if (twin) { twin.replaceWith(u); }
        });
        fresh.querySelectorAll("details").forEach(function (d, i) { if (openDetails[detailsKey(d, i)]) { d.open = true; } });
        stackOld.replaceWith(fresh);
        wire(fresh);
        if (active && document.contains(active) && document.activeElement !== active) {
          active.focus();
          if (sel) { try { active.setSelectionRange(sel[0], sel[1]); } catch (err) { /* not a text box */ } }
        }
        update();
      });
    }
    function detailsKey(d, i) { return (d.className || "") + ":" + (d.closest("[id]") ? d.closest("[id]").id : "") + ":" + i; }
    // what a swapped-in part needs set up again (the rest is delegated)
    function wire(root) {
      rateUsedAll(root);
      filterAll(root);
      if (window.OppPickers) { window.OppPickers.wire(root); }
      applyStates(root);
      wireCargo();
      if (root.querySelector('[data-qty-value]')) { qtyUpdate(); }
      root.querySelectorAll(".won-route").forEach(wonRoute);
    }
    wire(document);

    // ---- leaving ----
    window.addEventListener("beforeunload", function (e) {
      if (units().some(isDirty)) {
        e.preventDefault();
        e.returnValue = "";
        return "";
      }
    });
    // a test hook for the browser probes: what is open and what is changed
    window.__caseUnits = function () {
      return units().filter(isOpen).map(function (u) { return u.getAttribute("data-unit") + (isDirty(u) ? "*" : ""); });
    };
    update();
  }());
}());
