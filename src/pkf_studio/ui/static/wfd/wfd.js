/* pkf Studio — Workflow Developer component: the stage/step tree and the log console, as a hand-rolled Streamlit
   custom component (postMessage protocol, no build step, offline).

   Protocol: on load post streamlit:componentReady; each Streamlit rerun posts streamlit:render with {args, theme,
   disabled}; we post streamlit:setFrameHeight when the content size changes and streamlit:setComponentValue on clicks.
   Views (args.view): "tree" (args.snapshot, §2 of the plan) and "console" (args.lines, title, follow, height, §3).

   The tree is updated in place (nodes keyed by id) so CSS animations keep running across the 2-second reruns; the
   console appends only the new lines when the previous ones are an unchanged prefix. */
(function () {
  "use strict";

  const root = document.getElementById("root");
  const ICONS = {
    check: '<svg class="i-check" viewBox="0 0 12 12"><path d="M2.5 6.5l2.3 2.3L9.5 3.5"/></svg>',
    x: '<svg class="i-x" viewBox="0 0 12 12"><path d="M3.5 3.5l5 5M8.5 3.5l-5 5"/></svg>',
    dash: '<svg class="i-dash" viewBox="0 0 12 12"><path d="M3 6h6"/></svg>',
    chev: '<svg class="chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5L10.5 8 6 12.5"/></svg>',
    copy: '<svg viewBox="0 0 16 16"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg>',
    follow: '<svg viewBox="0 0 16 16"><path d="M8 3v9M4 8.5L8 12.5l4-4"/></svg>',
  };
  const STATUSES = ["not_started", "running", "waiting", "done", "failed", "skipped"];
  const OPEN_BY_DEFAULT = new Set(["running", "waiting", "failed"]);

  // ------------------------------------------------------------------------------------------- Streamlit protocol
  function post(type, extra) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, extra || {}), "*");
  }
  let lastHeight = -1;
  function setFrameHeight(h) {
    h = Math.ceil(h);
    if (h !== lastHeight && h > 0) {
      lastHeight = h;
      post("streamlit:setFrameHeight", { height: h });
    }
  }
  function setValue(value) {
    post("streamlit:setComponentValue", { value: value, dataType: "json" });
  }
  function applyTheme(theme) {
    if (!theme) return;
    const s = document.documentElement.style;
    if (theme.backgroundColor) s.setProperty("--bg", theme.backgroundColor);
    if (theme.secondaryBackgroundColor) s.setProperty("--bg2", theme.secondaryBackgroundColor);
    if (theme.textColor) s.setProperty("--text", theme.textColor);
    if (theme.primaryColor) s.setProperty("--primary", theme.primaryColor);
    if (theme.font) s.setProperty("--font", theme.font);
    document.documentElement.setAttribute("data-base", theme.base === "dark" ? "dark" : "light");
  }

  // ------------------------------------------------------------------------------------------- helpers
  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function text(node, value) {  // set textContent only when it changed (keeps layout work minimal)
    value = value == null ? "" : String(value);
    if (node.textContent !== value) node.textContent = value;
  }
  function setStatusClass(node, base, status, extra) {
    const want = [base, STATUSES.includes(status) ? status : "not_started"].concat(extra || []).join(" ");
    if (node.className !== want) node.className = want;
  }
  function fmtDuration(ms) {
    if (!(ms >= 0)) return "";
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return m + "m" + (r < 10 ? "0" : "") + r + "s";
    const h = Math.floor(m / 60), mm = m % 60;
    return h + "h" + (mm < 10 ? "0" : "") + mm + "m";
  }
  function stepTime(step) {
    const a = step.started_at ? Date.parse(step.started_at) : NaN;
    if (isNaN(a)) return "";
    if (step.status === "running" || step.status === "waiting") return fmtDuration(Date.now() - a);
    const b = step.ended_at ? Date.parse(step.ended_at) : NaN;
    return isNaN(b) ? "" : fmtDuration(b - a);
  }
  function stageStatus(stage) {
    if (stage.status) return stage.status;
    const st = (stage.steps || []).map(function (x) { return x.status; });
    if (st.includes("running")) return "running";
    if (st.includes("waiting")) return "waiting";
    if (st.includes("failed")) return "failed";
    if (st.length && st.every(function (x) { return x === "done" || x === "skipped"; })) return "done";
    return "not_started";
  }
  function counts(steps) {
    const c = { total: 0, done: 0, running: 0, waiting: 0, failed: 0, skipped: 0, not_started: 0 };
    steps.forEach(function (s) { c.total++; c[STATUSES.includes(s.status) ? s.status : "not_started"]++; });
    return c;
  }
  function setBar(bar, c) {  // segments: done+skipped, running, waiting, failed
    const total = Math.max(c.total, 1);
    const segs = [["seg-done", c.done + c.skipped], ["seg-running", c.running], ["seg-waiting", c.waiting], ["seg-failed", c.failed]];
    segs.forEach(function (s, i) {
      let i_ = bar.children[i];
      if (!i_) { i_ = el("i", s[0]); bar.appendChild(i_); }
      const w = (100 * s[1] / total).toFixed(2) + "%";
      if (i_.style.width !== w) i_.style.width = w;
    });
  }

  // ------------------------------------------------------------------------------------------- tree view
  const tree = {
    built: false,
    signature: "",          // stage ids + step ids: a change means rebuild
    toggles: new Map(),     // stage id -> user's open/closed choice (wins over the default; lives with the iframe)
    stages: new Map(),      // stage id -> {el, head, dot, title, progress, badge, bar, steps: Map(step id -> {...})}
    summary: null,
    selected: null,
    ticker: null,
  };

  function dotHtml() { return ICONS.check + ICONS.x + ICONS.dash; }

  function buildTree(snapshot) {
    root.className = "wfd tree-view";
    root.innerHTML = "";
    tree.stages.clear();

    const sum = el("div", "summary");
    sum.innerHTML = '<div class="row"><div class="pct"><span class="n">0%</span><small>done</small></div><div class="bar"></div>' +
      '<span class="runstate"></span></div><div class="row"><div class="counts"></div></div><div class="message"></div>';
    tree.summary = {
      el: sum, pct: sum.querySelector(".n"), bar: sum.querySelector(".bar"), counts: sum.querySelector(".counts"),
      runstate: sum.querySelector(".runstate"), message: sum.querySelector(".message"),
    };
    root.appendChild(sum);

    (snapshot.stages || []).forEach(function (stage) {
      const card = el("section", "stage");
      card.dataset.stage = stage.id;
      const head = el("button", "stage-head");
      head.type = "button";
      head.setAttribute("aria-expanded", "false");
      const dot = el("span", "dot", dotHtml());
      const title = el("span", "stage-title");
      const progress = el("span", "stage-progress");
      const badge = el("span", "stage-badge");
      head.appendChild(dot); head.appendChild(title); head.appendChild(badge); head.appendChild(progress);
      head.insertAdjacentHTML("beforeend", ICONS.chev);
      const bar = el("div", "stage-bar");
      const body = el("div", "stage-body");
      const list = el("ul", "steps");
      body.appendChild(list);
      card.appendChild(head); card.appendChild(bar); card.appendChild(body);
      head.addEventListener("click", function () {
        const open = !card.classList.contains("open");
        tree.toggles.set(stage.id, open);
        setOpen(card, head, open);
      });

      const steps = new Map();
      (stage.steps || []).forEach(function (step) {
        const row = el("li", "step");
        row.dataset.step = step.id;
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        const sdot = el("span", "dot", dotHtml());
        const sid = el("span", "id");
        const main = el("span", "main");
        const stitle = el("span", "title");
        const kind = el("span", "kind");
        const detail = el("span", "detail");
        main.appendChild(stitle); main.appendChild(kind); main.appendChild(detail);
        const time = el("span", "time");
        row.appendChild(sdot); row.appendChild(sid); row.appendChild(main); row.appendChild(time);
        const pick = function () {
          root.querySelectorAll(".step.selected").forEach(function (n) { n.classList.remove("selected"); });
          row.classList.add("selected");
          tree.selected = step.id;
          setValue(step.id);
        };
        row.addEventListener("click", pick);
        row.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
        });
        list.appendChild(row);
        steps.set(step.id, { el: row, dot: sdot, id: sid, title: stitle, kind: kind, detail: detail, time: time, data: step });
      });

      root.appendChild(card);
      tree.stages.set(stage.id, { el: card, head: head, dot: dot, title: title, progress: progress, badge: badge, bar: bar, steps: steps });
    });
    if (!(snapshot.stages || []).length) root.appendChild(el("div", "empty", "No workflow yet."));
    tree.built = true;
  }

  function setOpen(card, head, open) {
    card.classList.toggle("open", open);
    head.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function updateTree(snapshot) {
    const run = snapshot.run || {};
    const all = [];
    (snapshot.stages || []).forEach(function (stage) {
      const S = tree.stages.get(stage.id);
      const status = stageStatus(stage);
      const c = counts(stage.steps || []);
      all.push.apply(all, stage.steps || []);
      setStatusClass(S.el, "stage", status, S.el.classList.contains("open") ? ["open"] : []);
      setStatusClass(S.dot, "dot", status);
      const m = /^(\S+)\s+(.*)$/.exec(stage.title || "");
      const html = m && m[1] === stage.id ? '<span class="sid">' + esc(m[1]) + "</span>" + esc(m[2]) : esc(stage.title || stage.id);
      if (S.title.innerHTML !== html) S.title.innerHTML = html;
      const prog = Array.isArray(stage.progress) ? stage.progress : [c.done + c.skipped, c.total];
      text(S.progress, prog[0] + "/" + prog[1]);
      setStatusClass(S.badge, "stage-badge", status);
      text(S.badge, status === "not_started" ? "" : status.replace("_", " "));
      setBar(S.bar, c);
      const open = tree.toggles.has(stage.id) ? tree.toggles.get(stage.id) : OPEN_BY_DEFAULT.has(status);
      if (open !== S.el.classList.contains("open")) setOpen(S.el, S.head, open);

      (stage.steps || []).forEach(function (step) {
        const R = S.steps.get(step.id);
        R.data = step;
        const extra = [];
        if (step.optional) extra.push("optional");
        if (step.id === snapshot.selected) extra.push("selected");
        setStatusClass(R.el, "step", step.status, extra);
        setStatusClass(R.dot, "dot", step.status);
        text(R.id, step.id);
        text(R.title, step.title || "");
        text(R.kind, ["gate", "input", "review", "checklist", "action"].includes(step.kind) ? step.kind : "");
        text(R.detail, step.detail || "");
        text(R.time, stepTime(step));
        const tip = [step.id, step.title, step.detail].filter(Boolean).join(" — ");
        if (R.el.title !== tip) R.el.title = tip;
      });
    });
    tree.selected = snapshot.selected || null;

    // summary strip
    const c = counts(all);
    const pct = c.total ? Math.round(100 * (c.done + c.skipped) / c.total) : 0;
    text(tree.summary.pct, pct + "%");
    setBar(tree.summary.bar, c);
    const parts = [["done", c.done], ["running", c.running], ["waiting", c.waiting], ["failed", c.failed], ["not_started", c.not_started]];
    const chtml = parts.filter(function (p) { return p[1] || p[0] === "done"; }).map(function (p) {
      return '<span><i class="k ' + p[0] + '"></i><b>' + p[1] + "</b> " + p[0].replace("_", " ") + "</span>";
    }).join("") + "<span><b>" + c.total + "</b> steps</span>";
    if (tree.summary.counts.innerHTML !== chtml) tree.summary.counts.innerHTML = chtml;
    const rs = run.status || "idle";
    if (tree.summary.runstate.className !== "runstate " + rs) tree.summary.runstate.className = "runstate " + rs;
    text(tree.summary.runstate, rs.replace(/_/g, " "));
    text(tree.summary.message, run.message || "");

    // elapsed time of running/waiting steps ticks every second without a rerun
    const live = all.some(function (s) { return (s.status === "running" || s.status === "waiting") && s.started_at; });
    if (live && !tree.ticker) {
      tree.ticker = setInterval(function () {
        tree.stages.forEach(function (S) { S.steps.forEach(function (R) { text(R.time, stepTime(R.data)); }); });
      }, 1000);
    } else if (!live && tree.ticker) { clearInterval(tree.ticker); tree.ticker = null; }
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function renderTree(args) {
    const snapshot = args.snapshot || {};
    const sig = (snapshot.stages || []).map(function (s) {
      return s.id + ":" + (s.steps || []).map(function (x) { return x.id; }).join(",");
    }).join("|");
    if (!tree.built || sig !== tree.signature) { buildTree(snapshot); tree.signature = sig; }
    if (!(snapshot.stages || []).length) return;
    updateTree(snapshot);
  }

  // ------------------------------------------------------------------------------------------- console view
  const con = {
    built: false,
    keys: [],              // keys of the rendered lines, in order (prefix check for incremental appends)
    filter: "all",
    followUser: null,      // the user's Follow toggle (wins over args.follow once touched)
    followArg: true,
    pausedByScroll: false,
    programmatic: false,
    els: null,
    lines: [],
    query: "",             // 0.8.0: the page's "find in log" text (case-insensitive substring)
    numbers: false,        // 0.8.0: line numbers
  };
  const LEVELS = ["all", "info", "warn", "error", "agent", "tool"];

  function lineKey(l) { return (l.ts || "") + "\u0001" + (l.level || "") + "\u0001" + (l.source || "") + "\u0001" + (l.text || ""); }
  function levelMatches(level, filter) {
    if (filter === "all") return true;
    if (filter === "info") return !level || level === "info" || level === "ok" || level === "user";
    return level === filter;
  }
  function queryMatches(l) {
    if (!con.query) return true;
    return ((l.text == null ? "" : String(l.text)) + " " + (l.source || "")).toLowerCase().indexOf(con.query) >= 0;
  }
  function visible(l) { return levelMatches(l.level, con.filter) && queryMatches(l); }
  function following() { return con.followUser == null ? con.followArg : con.followUser; }

  function buildConsole(args) {
    root.className = "wfd console-view";
    root.innerHTML = "";
    const box = el("div", "console");
    const head = el("div", "console-head");
    const title = el("span", "ctitle");
    const count = el("span", "ccount");
    const chips = el("div", "chips");
    LEVELS.forEach(function (lv) {
      const b = el("button", "chip" + (lv === con.filter ? " on" : ""), lv);
      b.type = "button"; b.dataset.level = lv;
      b.addEventListener("click", function () {
        con.filter = lv;
        chips.querySelectorAll(".chip").forEach(function (x) { x.classList.toggle("on", x.dataset.level === lv); });
        applyFilter();
        scrollToBottom(true);
      });
      chips.appendChild(b);
    });
    const copy = el("button", "cbtn", ICONS.copy + "<span>Copy</span>");
    copy.type = "button"; copy.title = "Copy the visible lines";
    copy.addEventListener("click", function () { copyVisible(copy); });
    const follow = el("button", "cbtn follow", ICONS.follow + "<span>Follow</span>");
    follow.type = "button"; follow.title = "Scroll to new lines as they arrive";
    follow.addEventListener("click", function () {
      con.followUser = !following();
      con.pausedByScroll = false;
      syncFollowButton();
      if (following()) scrollToBottom(true);
    });
    head.appendChild(title); head.appendChild(count); head.appendChild(el("span", "spacer"));
    head.appendChild(chips); head.appendChild(copy); head.appendChild(follow);
    const wrap = el("div", "console-wrap");
    const body = el("div", "console-body");
    const pill = el("button", "newpill", "↓ new lines");
    pill.type = "button";
    pill.addEventListener("click", function () { con.pausedByScroll = false; scrollToBottom(true); syncFollowButton(); });
    body.addEventListener("scroll", function () {
      if (con.programmatic) return;
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 8;
      if (atBottom) { con.pausedByScroll = false; pill.classList.remove("show"); }
      else if (following()) con.pausedByScroll = true;
      syncFollowButton();
    });
    wrap.appendChild(body); wrap.appendChild(pill);
    box.appendChild(head); box.appendChild(wrap);
    root.appendChild(box);
    con.els = { box: box, title: title, count: count, body: body, pill: pill, follow: follow, copy: copy };
    con.built = true;
    con.keys = [];
  }

  function syncFollowButton() {
    const on = following() && !con.pausedByScroll;
    con.els.follow.classList.toggle("on", on);
    con.els.follow.querySelector("span").textContent = following() ? (con.pausedByScroll ? "Paused" : "Follow") : "Follow";
  }

  function makeLine(l, i) {
    const row = el("div", "line " + (l.level || "info"));
    const src = (l.source || "other");
    row.innerHTML = '<span class="ln"></span><span class="ts"></span><span class="src ' + esc(["job", "flow", "assistant"].includes(src) ? src : "other") + '"></span><span class="txt"></span>';
    row.children[0].textContent = String(i + 1);
    row.children[1].textContent = l.ts || "";
    row.children[2].textContent = src;
    row.children[3].textContent = l.text == null ? "" : String(l.text);
    if (!visible(l)) row.classList.add("hidden");
    return row;
  }

  function applyFilter() {
    const rows = con.els.body.children;
    for (let i = 0; i < rows.length; i++) {
      const l = con.lines[i];
      if (!l) continue;
      rows[i].classList.toggle("hidden", !visible(l));
    }
    updateCount();
  }

  // "n lines", or "k of n lines" while a level chip or the find text hides some; a placeholder when nothing matches
  function updateCount() {
    const lines = con.lines || [];
    const filtering = !!con.query || con.filter !== "all";
    const shown = filtering ? lines.filter(visible).length : lines.length;
    text(con.els.count, (filtering ? shown + " of " : "") + lines.length + (lines.length === 1 ? " line" : " lines"));
    let nm = con.els.body.querySelector(".console-nomatch");
    if (lines.length && filtering && !shown) {
      if (!nm) { nm = el("div", "console-empty console-nomatch"); con.els.body.appendChild(nm); }
      text(nm, con.query ? "No line matches \u201c" + con.query + "\u201d." : "No " + con.filter + " lines.");
    } else if (nm) nm.remove();
  }

  function scrollToBottom(force) {
    const body = con.els.body;
    if (!force && (!following() || con.pausedByScroll)) return;
    con.programmatic = true;
    body.scrollTop = body.scrollHeight;
    con.els.pill.classList.remove("show");
    requestAnimationFrame(function () { con.programmatic = false; });
  }

  function copyVisible(btn) {
    const txt = con.lines.filter(visible)
      .map(function (l) { return [l.ts, l.source ? "[" + l.source + "]" : "", l.text].filter(Boolean).join(" "); }).join("\n");
    const done = function (ok) {
      const span = btn.querySelector("span");
      span.textContent = ok ? "Copied" : "Failed";
      setTimeout(function () { span.textContent = "Copy"; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(function () { done(true); }, function () { done(fallbackCopy(txt)); });
    } else done(fallbackCopy(txt));
  }
  function fallbackCopy(txt) {
    const ta = document.createElement("textarea");
    ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function renderConsole(args) {
    const lines = Array.isArray(args.lines) ? args.lines : [];
    const height = Number(args.height) > 0 ? Number(args.height) : 360;
    if (!con.built) buildConsole(args);
    con.followArg = args.follow !== false;
    const q = String(args.query || "").trim().toLowerCase();
    const queryChanged = q !== con.query;
    con.query = q;
    con.numbers = !!args.numbers;
    con.els.box.classList.toggle("numbered", con.numbers);
    // 0.7.0 fill: the page sizes the iframe (CSS, a share of the window); the console fills it
    root.style.height = args.fill ? "100vh" : height + "px";
    text(con.els.title, args.title || "Console");

    const body = con.els.body;
    const keys = lines.map(lineKey);
    const n = con.keys.length;
    const unchangedPrefix = n <= keys.length && n > 0 && con.keys[0] === keys[0] && con.keys[n - 1] === keys[n - 1] &&
      (n < 3 || con.keys[n >> 1] === keys[n >> 1]);
    const nm0 = body.querySelector(".console-nomatch");   // never between lines: rows are matched to lines by index
    if (nm0) nm0.remove();
    const wasAtBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 8;
    let added = 0;
    if (unchangedPrefix) {
      const frag = document.createDocumentFragment();
      for (let i = n; i < lines.length; i++) frag.appendChild(makeLine(lines[i], i));
      added = lines.length - n;
      if (added) body.appendChild(frag);
    } else {
      body.innerHTML = "";
      const frag = document.createDocumentFragment();
      lines.forEach(function (l, i) { frag.appendChild(makeLine(l, i)); });
      body.appendChild(frag);
      added = lines.length;
    }
    con.lines = lines;
    con.keys = keys;
    if (queryChanged && unchangedPrefix) applyFilter();   // the find text changed: re-check the lines already shown
    else updateCount();
    const empty = body.querySelector(".console-empty");
    if (!lines.length && !empty) body.appendChild(el("div", "console-empty", "Nothing yet."));
    else if (lines.length && empty) empty.remove();

    if (added) {
      if (following() && !con.pausedByScroll) scrollToBottom(true);
      else if (!wasAtBottom) con.els.pill.classList.add("show");
    }
    syncFollowButton();
    setFrameHeight(height);
  }

  // ------------------------------------------------------------------------------------------- dispatch
  let view = null;
  function onRender(data) {
    applyTheme(data.theme);
    const args = data.args || {};
    const v = args.view === "console" ? "console" : "tree";
    if (v !== view) { root.innerHTML = ""; tree.built = false; con.built = false; view = v; }
    fill = !!args.fill;
    document.documentElement.classList.toggle("fill", fill);
    if (v === "console") renderConsole(args);
    else { renderTree(args); measure(); }
  }
  let fill = false;
  let raf = 0;
  function measure() {
    if (view !== "tree") return;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(function () { setFrameHeight(root.getBoundingClientRect().height + 6); });
  }
  if (window.ResizeObserver) new ResizeObserver(measure).observe(root);  // expand/collapse transitions resize the frame

  window.addEventListener("message", function (ev) {
    const d = ev.data;
    if (!d || d.type !== "streamlit:render") return;
    try { onRender(d); } catch (e) { console.error("wfd render failed", e); }
  });
  post("streamlit:componentReady", { apiVersion: 1 });
})();
