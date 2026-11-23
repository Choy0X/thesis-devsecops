"""The splash window: a small frameless window that opens first and shows the true progress of starting the app.

The page is one self-contained document (no network, no build step, so it can appear before the interface has been built). Its colours are the design
tokens read from ``app/frontend/src/index.css``, so the splash and the app can never drift apart. The launcher pushes ``Startup.snapshot()`` into
``window.splash.update`` a few times a second; a failed step shows its message in plain words, where the log is, and Retry and Quit buttons.
"""
from __future__ import annotations

import json
import pathlib
import re

CSS_PATH = pathlib.Path(__file__).resolve().parent / "frontend" / "src" / "index.css"
TITLE = "AI Vulnerability Framework"


def tokens() -> str:
    """The body of the ``:root`` token block of the interface (colours, shape, motion)."""
    text = CSS_PATH.read_text(encoding="utf-8")
    block = re.search(r":root,\s*\[data-theme=\"dark\"\]\s*\{(.*?)\n\}", text, re.S)
    return block.group(1) if block else ""


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
:root { __TOKENS__ }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--fg); font: 13px/1.5 "Inter Variable", Inter, "Segoe UI", system-ui, sans-serif; overflow: hidden; user-select: none; }
.frame { position: relative; height: 100%; margin: 0; padding: 26px 30px 22px; display: flex; flex-direction: column; border: 1px solid var(--border-strong); }
.frame::before, .frame::after { content: ""; position: absolute; width: 14px; height: 14px; border: 0 solid var(--brand); }
.frame::before { top: -1px; left: -1px; border-top-width: 2px; border-left-width: 2px; }
.frame::after { right: -1px; bottom: -1px; border-bottom-width: 2px; border-right-width: 2px; }
.mark { display: flex; align-items: center; gap: 12px; }
.mark b { display: block; font: 600 11px/1 ui-monospace, "JetBrains Mono", Consolas, monospace; letter-spacing: .22em; text-transform: uppercase; }
.mark span { display: block; margin-top: 5px; color: var(--faint); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }
.glyph { width: 30px; height: 30px; background: var(--brand); display: grid; place-items: center; color: var(--on-brand); font: 700 15px ui-monospace, Consolas, monospace; }
.log { margin: 22px 0 14px; flex: 1; overflow: hidden; font: 12px/1.7 ui-monospace, "JetBrains Mono", Consolas, monospace; color: var(--muted); }
.log div { display: flex; gap: 10px; white-space: nowrap; }
.log .t { width: 14px; text-align: center; color: var(--faint); }
.log .done .t { color: var(--ok); } .log .running { color: var(--fg); } .log .running .t { color: var(--brand-strong); } .log .failed { color: var(--crit-soft); } .log .failed .t { color: var(--crit); }
.log .d { color: var(--faint); overflow: hidden; text-overflow: ellipsis; }
.bar { display: flex; gap: 3px; height: 10px; }
.bar i { flex: 1; background: var(--surface-3); transform: skewX(-18deg); }
.bar i.on { background: var(--brand-strong); }
.now { display: flex; justify-content: space-between; margin-top: 10px; color: var(--muted); font-size: 12px; }
.fail { margin-top: 12px; padding: 10px 12px; border: 1px solid var(--crit-line); background: var(--crit-tint); display: none; }
.fail p { margin: 0 0 4px; } .fail .log-path { color: var(--faint); font: 11px ui-monospace, Consolas, monospace; word-break: break-all; }
.fail .row { display: flex; gap: 8px; margin-top: 10px; }
button { font: inherit; height: 30px; padding: 0 14px; background: var(--surface-2); color: var(--fg); border: 1px solid var(--border-strong); cursor: pointer; }
button.primary { background: var(--brand); border-color: var(--brand); color: var(--on-brand); font-weight: 600; }
button:focus-visible { outline: 2px solid var(--brand-strong); outline-offset: 2px; }
.cursor { display: inline-block; width: 7px; height: 12px; background: var(--brand-strong); vertical-align: -1px; animation: blink 1.1s steps(2, start) infinite; }
@keyframes blink { to { visibility: hidden; } }
@media (prefers-reduced-motion: reduce) { .cursor { animation: none; } }
</style></head>
<body><div class="frame">
<div class="mark"><div class="glyph">V</div><div><b>Vuln Framework</b><span>detect · predict · patch</span></div></div>
<div class="log" id="log" role="log" aria-live="polite"></div>
<div class="bar" id="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="Starting"></div>
<div class="now"><span id="now">Starting<span class="cursor"></span></span><span id="pct">0%</span></div>
<div class="fail" id="fail" role="alert"><p id="msg"></p><div class="log-path" id="logpath"></div>
<div class="row"><button class="primary" id="retry">Try again</button><button id="quit">Quit</button></div></div>
</div>
<script>
(function () {
  var SEGMENTS = 28, bar = document.getElementById("bar");
  for (var i = 0; i < SEGMENTS; i++) bar.appendChild(document.createElement("i"));
  var GLYPH = { pending: "·", running: ">", done: "+", failed: "x", skipped: "-" };
  function call(name) { try { if (window.ipc && window.ipc.SplashIPC) window.ipc.SplashIPC[name](); } catch (e) { /* the buttons do nothing without the shell */ } }
  document.getElementById("retry").onclick = function () { call("retry"); };
  document.getElementById("quit").onclick = function () { call("quit"); };
  window.splash = {
    update: function (s) {
      var log = document.getElementById("log"); log.innerHTML = "";
      s.steps.forEach(function (st) {
        if (st.state === "pending") return;
        var row = document.createElement("div"); row.className = st.state;
        var t = document.createElement("span"); t.className = "t"; t.textContent = GLYPH[st.state] || "·";
        var l = document.createElement("span"); l.textContent = st.label;
        row.appendChild(t); row.appendChild(l);
        if (st.detail) { var d = document.createElement("span"); d.className = "d"; d.textContent = "· " + st.detail; row.appendChild(d); }
        log.appendChild(row);
      });
      var pct = Math.round(s.fraction * 100), on = Math.round(s.fraction * SEGMENTS);
      Array.prototype.forEach.call(bar.children, function (el, i) { el.className = i < on ? "on" : ""; });
      bar.setAttribute("aria-valuenow", String(pct));
      document.getElementById("pct").textContent = pct + "%";
      document.getElementById("now").firstChild.textContent = s.failure ? "Could not start" : (s.current || (s.ready ? "Ready" : "Starting"));
      var fail = document.getElementById("fail");
      if (s.failure) {
        document.getElementById("msg").textContent = s.failure.step + ": " + s.failure.message;
        document.getElementById("logpath").textContent = s.failure.log ? "Details: " + s.failure.log : "";
        fail.style.display = "block";
      } else fail.style.display = "none";
    }
  };
})();
</script></body></html>
"""


def html() -> str:
    """The splash document with the design tokens inlined."""
    return PAGE.replace("__TOKENS__", tokens()).replace("__TITLE__", TITLE)


def update_script(snapshot: dict) -> str:
    """The JavaScript that pushes a startup snapshot into the page."""
    return "window.splash && window.splash.update(" + json.dumps(snapshot) + ");"
