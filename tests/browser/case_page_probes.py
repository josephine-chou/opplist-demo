"""Case page browser checks (15a, case page v3): headless Edge on 8799 against
a COPY of the live DB - never 8765, never the live file. Not collected by
pytest.

    python tests/browser/case_page_probes.py serve [--fresh]   # 8799: a copy of the live DB
    python tests/browser/case_page_probes.py run [ENQUIRY_ID]  # every probe, one page load each
    python tests/browser/case_page_probes.py shot ENQUIRY_ID cards|actions OUT.png
Stop 8799 after (netstat -ano | grep :8799, then taskkill).

<dir> is OPPLIST_PROBE_DIR, or opplist-probes in the temp folder; the copy
is <dir>/opplist.db and the server's data folder <dir>/data, so nothing
lands in the real data folder. 'run' picks a case with three live routes
that is New or Quoted when no id is given; every probe types invented
values only ('ZZ probe ...').

How it works: ?__probe=<name> on a page makes the server put an error
catcher in <head> and the probe's script after the page's own; the probe
works the real page (the same URL, so the page script's fetches and its
reading of the page are the real ones) and writes what it found into
<pre id="__out">, which msedge --dump-dom prints. A probe that writes
changes the copy, so start it again with --fresh between runs.
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(os.environ.get("OPPLIST_PROBE_DIR") or Path(tempfile.gettempdir()) / "opplist-probes")
HERE.mkdir(exist_ok=True)
DB = HERE / "opplist.db"
LIVE = Path(__file__).resolve().parents[3] / "data" / "opplist.db"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = "http://127.0.0.1:8799"

HEAD = ('<script>window.__errs=[];window.addEventListener("error",function(e){window.__errs.push(e.message+" @"+e.lineno)});'
        'window.__log=function(s){var o=document.getElementById("__out");if(!o){o=document.createElement("pre");'
        'o.id="__out";document.body.prepend(o);}o.textContent+=s+"\\n";};</script>')

COMMON = r"""
function q(s) { return document.querySelector(s); }
function U(id) { return q('[data-unit="' + id + '"]'); }
function routeUnits() { return Array.prototype.slice.call(document.querySelectorAll('[data-unit^="route-"]')).filter(function (u) { return u.getAttribute("data-unit") !== "route-new"; }); }
function wait(cond, ms) {
  return new Promise(function (ok, no) {
    var t0 = Date.now();
    (function tick() { if (cond()) { ok(); } else if (Date.now() - t0 > (ms || 8000)) { no(new Error("timed out")); } else { setTimeout(tick, 100); } })();
  });
}
function flash() { return ((q("body > .flash") || {}).textContent || "").replace(/\s+/g, " ").trim(); }
function bar() { return (q("#bb-saved") || {}).textContent; }
function railDots() { return Array.prototype.map.call(document.querySelectorAll(".rail .sections li.dirty a"), function (a) { return a.getAttribute("href"); }).join(","); }
function asks() { var ev = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(ev); return ev.defaultPrevented; }
function typeIn(el, value) { el.focus(); el.value = value; el.dispatchEvent(new Event("input", { bubbles: true })); }
"""

PROBES = {
    # route B open and changed; every other kind of write; route B stays as typed and unsaved
    "route_b_stays": r"""
var b = routeUnits()[1], bId = b.getAttribute("data-unit");
function B() { return U(bId); }
function routeB() { var u = B(), alt = u.querySelector('[name="dest_alt"]');
  return "routeB open=" + u.classList.contains("editing") + " dirty=" + u.classList.contains("dirty") + " typing kept=" + (alt.value === "ZZ probe address") + " focus=" + (document.activeElement === alt); }
window.__noReload = "still the same page";
var headBefore = q("#head h1").textContent.replace(/\s+/g, " "), railBefore = q(".rail-card").textContent.replace(/\s+/g, " ");
var tlBefore = document.querySelectorAll("#status .timeline li").length;
B().querySelector("[data-edit]").click();
typeIn(B().querySelector('[name="dest_alt"]'), "ZZ probe address");
__log("1 " + routeB() + " | bar: " + bar() + " | rail dot: " + railDots() + " | leaving asks: " + asks());
U("request").querySelector("[data-edit]").click();
typeIn(U("request").querySelector('[name="end_customer"]'), "ZZ probe customer");
B().querySelector('[name="dest_alt"]').focus();
U("request").querySelector("form").requestSubmit();
wait(function () { return !U("request").classList.contains("editing") && /ZZ probe customer/.test(U("request").textContent); }).then(function () {
  __log("2 after the Request card's Save: " + routeB() + " | flash: " + flash());
  var act = q('.acts-row [data-act="quoted"]') ? "quoted" : "requoted";
  q('.acts-row [data-act="' + act + '"]').click();
  U("act-" + act).querySelector('[name="note"]').value = "ZZ probe quote";
  var n = document.querySelectorAll("#status .timeline li").length;
  U("act-" + act).querySelector("form").requestSubmit();
  return wait(function () { return document.querySelectorAll("#status .timeline li").length > n; }).then(function () {
    __log("3 after recording " + act + ": " + routeB() + " | flash: " + flash());
  });
}).then(function () {
  q("#subject-add input[name=subject]").value = "ZZ probe subject RE: PALLETS // SAMPLE";
  var n = document.querySelectorAll("#subjects li").length;
  q("#subject-add").requestSubmit();
  return wait(function () { return document.querySelectorAll("#subjects li").length > n; }).then(function () {
    __log("4 after Add subject: " + routeB() + " | flash: " + flash());
  });
}).then(function () {
  var f = q("form.add-vendor"), n = document.querySelectorAll("#vendors tbody.unit").length;
  f.querySelector('[name="new_vendor_key"]').value = "ZZ Probe Vendor";
  f.requestSubmit();
  return wait(function () { return document.querySelectorAll("#vendors tbody.unit").length > n; }).then(function () {
    __log("5 after logging a vendor: " + routeB());
  });
}).then(function () {
  q('.acts-row [data-act="hold"]').click();
  var f = U("act-hold").querySelector("form");
  f.querySelector('[name="hold_reason"]').value = "ZZ probe hold";
  f.querySelector(".qk [data-days]").click();
  f.requestSubmit();
  return wait(function () { return !!q("#status .current-status .hold"); }).then(function () {
    q("#status .current-status form").requestSubmit();
    return wait(function () { return !q("#status .current-status .hold"); });
  }).then(function () { __log("6 after On hold and Release hold: " + routeB() + " | flash: " + flash()); });
}).then(function () {
  return fetch(location.pathname).then(function (r) { return r.text(); }).then(function (t) {
    __log("7 route B written by any of these: " + (t.indexOf("ZZ probe address") >= 0));
    __log("8 no reload: " + (window.__noReload === "still the same page") + " | header changed: " + (q("#head h1").textContent.replace(/\s+/g, " ") !== headBefore)
      + " | rail changed: " + (q(".rail-card").textContent.replace(/\s+/g, " ") !== railBefore) + " | timeline +" + (document.querySelectorAll("#status .timeline li").length - tlBefore));
    __log("9 bar: " + bar() + " | rail dot: " + railDots() + " | leaving asks: " + asks());
    B().querySelector("[data-cancel]").click();
    __log("10 after route B's Cancel: open=" + B().classList.contains("editing") + " back to saved=" + (B().querySelector('[name="dest_alt"]').value !== "ZZ probe address") + " | leaving asks: " + asks() + " | bar: " + bar());
    __log("errors: " + JSON.stringify(window.__errs));
  });
}).catch(function (e) { __log("FAILED " + e + " | errors " + JSON.stringify(window.__errs)); });
""",
    # a refused Request save keeps the unit open, the typing and the reason beside the field
    "refused_request": r"""
var u = U("request");
u.querySelector("[data-edit]").click();
typeIn(u.querySelector('[name="end_customer"]'), "ZZ probe refused customer");
var sel = u.querySelector('[name="handled_by"]'), opt = document.createElement("option");
opt.value = "NOWHERE"; opt.textContent = "NOWHERE"; sel.appendChild(opt); sel.value = "NOWHERE";
sel.dispatchEvent(new Event("change", { bubbles: true }));
u.querySelector("form").requestSubmit();
wait(function () { return !!u.querySelector(".js-err"); }).then(function () {
  var err = u.querySelector(".js-err");
  __log("open=" + u.classList.contains("editing") + " typing kept=" + (u.querySelector('[name="end_customer"]').value === "ZZ probe refused customer")
    + " | reason: " + err.textContent + " | beside: " + (err.previousElementSibling && err.previousElementSibling.name));
  return fetch(location.pathname).then(function (r) { return r.text(); }).then(function (t) {
    __log("written: " + (t.indexOf("ZZ probe refused customer") >= 0) + " | errors: " + JSON.stringify(window.__errs));
  });
}).catch(function (e) { __log("FAILED " + e); });
""",
    # one action open at a time; pressing it again closes it; Esc cancels a unit; Enter saves one
    "keys_and_actions": r"""
var first = q(".acts-row [data-act]"), second = document.querySelectorAll(".acts-row [data-act]")[1];
first.click(); var a = U("act-" + first.getAttribute("data-act"));
second.click(); var b2 = U("act-" + second.getAttribute("data-act"));
__log("second opens, first closes: " + (!a.classList.contains("editing") && b2.classList.contains("editing")));
second.click();
__log("pressed again closes: " + !b2.classList.contains("editing"));
var u = U("request"); u.querySelector("[data-edit]").click();
var ec = u.querySelector('[name="end_customer"]'); typeIn(ec, "ZZ probe esc");
ec.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
__log("Esc cancels: open=" + u.classList.contains("editing") + " typing gone=" + (ec.value !== "ZZ probe esc"));
u.querySelector("[data-edit]").click(); typeIn(ec, "ZZ probe enter customer");
ec.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
wait(function () { return !U("request").classList.contains("editing") && /ZZ probe enter customer/.test(U("request").textContent); }).then(function () {
  __log("Enter saves the unit: yes | leaving asks: " + asks() + " | errors: " + JSON.stringify(window.__errs));
}).catch(function (e) { __log("FAILED " + e); });
""",
    # screenshots: route B open and changed; the Won form open
    "shot_cards": r"""
var b = routeUnits()[1]; b.querySelector("[data-edit]").click();
var alt = b.querySelector('[name="dest_alt"]');
alt.value = alt.value + " (gate 2)"; alt.dispatchEvent(new Event("input", { bubbles: true }));
if (document.activeElement) { document.activeElement.blur(); } window.scrollTo(0, 0);
""",
    "shot_actions": r"""
q('.acts-row [data-act="won"]').click();

""",
}


def copy_live():
    for p in HERE.glob("opplist.db*"):
        p.unlink()
    src = sqlite3.connect("file:{}?mode=ro".format(LIVE.as_posix()), uri=True)
    dst = sqlite3.connect(str(DB))
    src.backup(dst)
    dst.close()
    src.close()


def serve():
    if "--fresh" in sys.argv or not DB.exists():
        copy_live()
    os.environ["OPPLIST_DATA_DIR"] = str(HERE / "data")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from flask import request
    from opplist.web import create_app
    app = create_app(str(DB))

    @app.after_request
    def inject(resp):
        name = request.args.get("__probe")
        if not name or name not in PROBES or not resp.content_type.startswith("text/html"):
            return resp
        script = ("<script>window.addEventListener('load',function(){setTimeout(function(){try{" + COMMON + PROBES[name]
                  + "}catch(e){__log('probe error: '+e);}},300);});</script>")
        text = resp.get_data(as_text=True).replace("<head>", "<head>" + HEAD, 1).replace("</body>", script + "</body>", 1)
        resp.set_data(text)
        return resp

    app.run(host="127.0.0.1", port=8799, threaded=True)


def pick_case():
    conn = sqlite3.connect(str(DB))
    row = conn.execute("SELECT e.id FROM enquiry e WHERE e.status IN ('New','Quoted') AND e.hold_reason IS NULL "
                       "AND (SELECT COUNT(*) FROM enquiry_route r WHERE r.enquiry_id = e.id AND r.voided = 0) = 3 "
                       "ORDER BY e.id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0]


def dump(url, budget=60000):
    out = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--user-data-dir=" + str(HERE / "edge"),
                          "--virtual-time-budget={}".format(budget), "--dump-dom", url],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180).stdout
    found = re.search(r'<pre id="__out">(.*?)</pre>', out, re.S)
    return found.group(1) if found else "(no output)"


def run():
    case = int(sys.argv[2]) if len(sys.argv) > 2 else pick_case()
    for name in ("route_b_stays", "refused_request", "keys_and_actions"):
        print("== {} (enquiry {})".format(name, case))
        print(dump("{}/enquiries/{}?__probe={}".format(BASE, case, name)))


def shot():
    case, which, out = int(sys.argv[2]), sys.argv[3], str(Path(sys.argv[4]).resolve())
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--user-data-dir=" + str(HERE / "edge"),
                    "--window-size=1440,{}".format(2300 if which == "cards" else 1300), "--virtual-time-budget=8000", "--screenshot=" + out,
                    "{}/enquiries/{}?__probe=shot_{}".format(BASE, case, which)], timeout=120)
    print(out)


if __name__ == "__main__":
    {"serve": serve, "run": run, "shot": shot}[sys.argv[1]]()
