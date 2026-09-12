"""The procurement-intelligence dashboard (single self-contained HTML page).

Kept in its own module so tracker.py stays focused on scraping/serving. The
page is a template with __X_JSON__ placeholders filled by
tracker.build_dashboard_page(): DATA, SECTIONS, CONTRACTORS, ANALYTICS, NOTIFS,
STATUS, AWARDS, OVERVIEW.

Design: restrained, professional, navy brand (#1a3e6e) on neutral slate.
Answers the five questions -- what's open, what changed, what was awarded to
whom for how much, who the winners are, and how fresh/trustworthy the data is.
Monetary amounts arrive as exact decimal strings and are formatted only for
display; estimated value and awarded value are always kept distinct; unknown
amounts render as "Unknown", never 0.
"""

DASHBOARD_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tender Watch — Maharashtra Procurement Intelligence</title>
<style>
:root{
  --navy:#1a3e6e; --navy-700:#15325a; --navy-50:rgba(255,255,255,.45);
  --ink:#152238; --ink-2:#334155; --muted:#516074; --faint:#8395ab;
  --bg:#eaf0f8;
  /* Liquid glass surfaces */
  --glass:rgba(255,255,255,.52); --glass-2:rgba(255,255,255,.72);
  --glass-soft:rgba(255,255,255,.34);
  --glass-brd:rgba(255,255,255,.7); --card:var(--glass);
  --line:rgba(120,140,170,.28); --line-2:rgba(120,140,170,.45);
  --blur:saturate(185%) blur(18px);
  --ok:#0f7a3d; --ok-bg:rgba(220,252,231,.75); --warn:#a35a08; --warn-bg:rgba(254,243,199,.8);
  --urgent:#b3211b; --urgent-bg:rgba(254,226,226,.8); --info:#1d4ed8; --info-bg:rgba(219,234,254,.8);
  --award:#6d28d9; --award-bg:rgba(237,233,254,.85); --stale:#9a3412; --stale-bg:rgba(255,237,213,.85);
  --radius:16px;
  --shadow:0 1px 2px rgba(15,23,42,.05),0 2px 10px rgba(15,23,42,.06);
  --shadow-lg:0 20px 50px rgba(15,23,42,.22);
  --hair:inset 0 1px 0 rgba(255,255,255,.55);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color:var(--ink); font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased;
  background:
    radial-gradient(1100px 620px at 8% -8%, rgba(56,132,255,.22), transparent 60%),
    radial-gradient(1000px 700px at 108% 4%, rgba(139,92,246,.18), transparent 55%),
    radial-gradient(900px 640px at 52% 116%, rgba(20,184,166,.16), transparent 55%),
    linear-gradient(160deg,#eef3fb 0%,#e9eef8 45%,#f1ecfb 100%);
  background-attachment:fixed; min-height:100vh;}
a{color:var(--navy); text-decoration:none}
a:hover{text-decoration:underline}
.num{font-variant-numeric:tabular-nums; font-feature-settings:"tnum"}
button{font-family:inherit; cursor:pointer}
:focus-visible{outline:2px solid var(--navy); outline-offset:2px; border-radius:6px}

/* ---- Top bar ---- */
.topbar{position:sticky; top:0; z-index:40; color:#fff;
  background:linear-gradient(180deg, rgba(21,50,90,.82), rgba(21,50,90,.70));
  -webkit-backdrop-filter:var(--blur); backdrop-filter:var(--blur);
  display:flex; align-items:center; gap:16px; padding:10px 18px;
  border-bottom:1px solid rgba(255,255,255,.14);
  box-shadow:0 8px 30px rgba(15,23,42,.18);}
.brand{display:flex; align-items:center; gap:10px; font-weight:700; letter-spacing:.2px}
.brand .mark{width:26px;height:26px;border-radius:7px;background:#fff;color:var(--navy);
  display:grid;place-items:center;font-weight:800;font-size:15px}
.brand small{font-weight:500;opacity:.8;font-size:11px;display:block;margin-top:-2px}
.topbar .spacer{flex:1}
.fresh{display:flex;align-items:center;gap:9px;background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.16);padding:6px 11px;border-radius:999px;
  cursor:pointer;color:#fff;font-size:12.5px}
.fresh:hover{background:rgba(255,255,255,.16)}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.dot.up{background:#4ade80}.dot.partial{background:#fbbf24}
.dot.stale{background:#fb923c}.dot.failed{background:#f87171}.dot.unknown{background:#cbd5e1}
.icon-btn{position:relative;background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.16);
  color:#fff;width:38px;height:38px;border-radius:10px;display:grid;place-items:center;font-size:17px}
.icon-btn:hover{background:rgba(255,255,255,.18)}
.badge-count{position:absolute;top:-6px;right:-6px;background:#ef4444;color:#fff;
  border-radius:999px;min-width:18px;height:18px;padding:0 5px;font-size:11px;font-weight:700;
  display:grid;place-items:center;border:2px solid var(--navy)}
.btn{background:var(--glass-2);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  color:var(--navy);border:1px solid var(--glass-brd);border-radius:11px;
  padding:8px 13px;font-weight:600;font-size:13px;display:inline-flex;align-items:center;gap:7px;
  box-shadow:var(--hair)}
.btn:hover{background:rgba(255,255,255,.92)}
.btn.ghost{background:rgba(255,255,255,.10);color:#fff;border-color:rgba(255,255,255,.18)}
.btn.ghost:hover{background:rgba(255,255,255,.18)}
.btn[disabled]{opacity:.6;cursor:default}

/* ---- Data status panel ---- */
.status-panel{position:absolute;top:64px;left:18px;right:18px;max-width:720px;
  background:var(--glass-2);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border:1px solid var(--glass-brd);border-radius:var(--radius);box-shadow:var(--shadow-lg),var(--hair);
  padding:16px 18px;z-index:60;display:none;color:var(--ink)}
.status-panel.open{display:block}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:10px}
.status-grid .cell{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.status-grid .cell .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.status-grid .cell .v{font-weight:600;margin-top:3px}
.src-table{width:100%;border-collapse:collapse;margin-top:12px;font-size:12.5px}
.src-table th,.src-table td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
.src-table th{color:var(--muted);font-weight:600}

/* ---- Layout ---- */
.shell{display:flex;min-height:calc(100vh - 58px)}
.nav{width:210px;flex:none;background:var(--glass-soft);
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border-right:1px solid var(--line);padding:14px 10px;
  position:sticky;top:58px;height:calc(100vh - 58px);overflow:auto}
.nav a{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;color:var(--ink-2);
  font-weight:600;font-size:13.5px;margin-bottom:2px}
.nav a:hover{background:var(--bg);text-decoration:none}
.nav a.active{background:var(--navy);color:#fff}
.nav a .ico{width:18px;text-align:center}
.nav a .tag{margin-left:auto;background:var(--bg);color:var(--muted);border-radius:999px;
  padding:1px 8px;font-size:11px;font-weight:700}
.nav a.active .tag{background:rgba(255,255,255,.2);color:#fff}
.nav .navlabel{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);
  padding:12px 12px 6px;font-weight:700}
main{flex:1;min-width:0;padding:22px 26px 60px}
.page{display:none}.page.active{display:block}
h1{font-size:20px;margin:0 0 3px;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px}

/* ---- Tiles ---- */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:22px}
.tile{background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border:1px solid var(--glass-brd);border-radius:var(--radius);padding:15px 16px;
  box-shadow:var(--shadow),var(--hair);position:relative}
.tile .label{font-size:12.5px;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:6px}
.tile .val{font-size:26px;font-weight:750;margin-top:6px;letter-spacing:-.02em;color:var(--navy)}
.tile .period{font-size:11px;color:var(--faint);margin-top:5px;text-transform:uppercase;letter-spacing:.04em}
.help{width:15px;height:15px;border-radius:50%;background:var(--bg);color:var(--muted);border:1px solid var(--line);
  font-size:10px;display:inline-grid;place-items:center;cursor:help;font-weight:700}
.tile.clickable{cursor:pointer}.tile.clickable:hover{border-color:var(--line-2);box-shadow:var(--shadow-lg)}

/* ---- Cards / sections ---- */
.card{background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border:1px solid var(--glass-brd);border-radius:var(--radius);box-shadow:var(--shadow),var(--hair);
  margin-bottom:20px;overflow:hidden}
.card > .head{display:flex;align-items:center;gap:10px;padding:13px 16px;border-bottom:1px solid var(--line)}
.card > .head h2{font-size:14.5px;margin:0;font-weight:700}
.card > .head .desc{color:var(--muted);font-size:12px;font-weight:500}
.card > .head .spacer{flex:1}
.card > .body{padding:14px 16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}

/* ---- Tables ---- */
.tablewrap{overflow-x:auto}
table.data{width:100%;border-collapse:collapse;font-size:13px}
table.data th,table.data td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);
  vertical-align:top}
table.data th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;
  letter-spacing:.03em;white-space:nowrap;cursor:pointer;user-select:none;position:sticky;top:0;
  background:var(--glass-2);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
table.data th.no-sort{cursor:default}
table.data tbody tr{cursor:pointer}
table.data tbody tr:hover{background:var(--navy-50)}
table.data td.r,table.data th.r{text-align:right;font-variant-numeric:tabular-nums}
.t-title{font-weight:600;color:var(--ink);max-width:420px}
.t-sub{color:var(--muted);font-size:11.5px;margin-top:2px}
.sortcaret{opacity:.4;font-size:10px}
th.sorted .sortcaret{opacity:1}

/* ---- Badges ---- */
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;
  font-size:11.5px;font-weight:600;white-space:nowrap}
.badge.live{background:var(--ok-bg);color:var(--ok)}
.badge.soon{background:var(--warn-bg);color:var(--warn)}
.badge.urgent{background:var(--urgent-bg);color:var(--urgent)}
.badge.closed{background:#e2e8f0;color:#475569}
.badge.awarded{background:var(--award-bg);color:var(--award)}
.badge.kind-procurement{background:var(--ok-bg);color:var(--ok)}
.badge.kind-historical_import{background:var(--info-bg);color:var(--info)}
.badge.kind-data_correction{background:var(--warn-bg);color:var(--warn)}
.badge.kind-source_health{background:var(--stale-bg);color:var(--stale)}
.chip{display:inline-block;background:var(--bg);border:1px solid var(--line);color:var(--ink-2);
  border-radius:7px;padding:2px 8px;font-size:11.5px;margin:2px 4px 2px 0}
.unknown{color:var(--faint);font-style:italic}

/* ---- Filters ---- */
.filters{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:16px;align-items:center}
.filters input[type=search],.filters select{border:1px solid var(--glass-brd);border-radius:11px;padding:8px 12px;
  font-size:13px;background:var(--glass-2);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  color:var(--ink)}
.filters input[type=search]{min-width:250px}
.filters .count{color:var(--muted);font-size:12.5px;margin-left:auto}

/* ---- Star / watch ---- */
.star{background:none;border:none;color:var(--faint);font-size:16px;padding:2px 4px;line-height:1}
.star.on{color:#f59e0b}
.star:hover{color:#f59e0b}

/* ---- Bars (analytics) ---- */
.bar-row{display:grid;grid-template-columns:180px 1fr 110px;gap:10px;align-items:center;margin:7px 0;font-size:12.5px}
.bar-row .lbl{color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{background:var(--bg);border-radius:6px;height:16px;overflow:hidden;border:1px solid var(--line);display:block}
.bar-fill{display:block;height:100%;background:var(--navy);border-radius:6px 0 0 6px;min-width:0}
.bar-row .val{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.scale{display:flex;justify-content:space-between;color:var(--faint);font-size:10.5px;margin:2px 0 8px;
  padding-left:190px}
@media(max-width:720px){.bar-row{grid-template-columns:120px 1fr 80px}.scale{padding-left:130px}}
.callout{background:var(--warn-bg);border:1px solid #fde68a;color:#7c5300;border-radius:9px;
  padding:11px 14px;font-size:12.5px;margin-bottom:14px}
.callout b{color:#663f00}

/* ---- Slide-over ---- */
.overlay{position:fixed;inset:0;background:rgba(15,23,42,.30);
  -webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);z-index:70;display:none}
.overlay.open{display:block}
.slideover{position:fixed;top:0;right:0;height:100%;width:min(560px,94vw);
  background:rgba(255,255,255,.82);-webkit-backdrop-filter:saturate(180%) blur(26px);
  backdrop-filter:saturate(180%) blur(26px);border-left:1px solid var(--glass-brd);
  z-index:80;box-shadow:var(--shadow-lg);transform:translateX(100%);transition:transform .22s ease;
  display:flex;flex-direction:column}
.slideover.open{transform:translateX(0)}
.slideover .so-head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:flex-start}
.slideover .so-head h3{margin:0;font-size:16px;font-weight:700;flex:1}
.slideover .so-body{overflow:auto;padding:16px 18px;flex:1}
.so-close{background:var(--bg);border:1px solid var(--line);border-radius:8px;width:34px;height:34px;font-size:16px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;font-size:13px;margin:8px 0}
.kv .k{color:var(--muted)}
.section-title{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--faint);
  font-weight:700;margin:18px 0 6px;border-top:1px solid var(--line);padding-top:12px}
.timeline{list-style:none;margin:8px 0;padding:0}
.timeline li{position:relative;padding:0 0 14px 20px;border-left:2px solid var(--line);margin-left:5px}
.timeline li:last-child{border-left-color:transparent}
.timeline li .tdot{position:absolute;left:-7px;top:2px;width:12px;height:12px;border-radius:50%;
  background:var(--navy);border:2px solid #fff}
.timeline li .tt{font-weight:600}
.timeline li .td{color:var(--muted);font-size:12px}
.so-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}

/* ---- Inbox drawer ---- */
.drawer{position:fixed;top:0;right:0;height:100%;width:min(420px,94vw);
  background:rgba(255,255,255,.82);-webkit-backdrop-filter:saturate(180%) blur(26px);
  backdrop-filter:saturate(180%) blur(26px);border-left:1px solid var(--glass-brd);
  z-index:80;box-shadow:var(--shadow-lg);transform:translateX(100%);transition:transform .2s ease;
  display:flex;flex-direction:column}
.drawer.open{transform:translateX(0)}
.drawer .dh{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.drawer .dh h3{margin:0;font-size:15px;flex:1}
.drawer .dtools{display:flex;gap:6px;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line)}
.drawer .dtools .seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.drawer .dtools .seg button{border:none;background:var(--card);padding:6px 10px;font-size:12px;color:var(--ink-2)}
.drawer .dtools .seg button.on{background:var(--navy);color:#fff}
.drawer .dlist{overflow:auto;flex:1}
.notif{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:10px;cursor:pointer}
.notif:hover{background:var(--bg)}
.notif.unread{background:var(--navy-50)}
.notif .nd{flex:1;min-width:0}
.notif .nt{font-weight:600;font-size:13px;margin-bottom:2px}
.notif .nm{color:var(--ink-2);font-size:12px;white-space:pre-line}
.notif .nmeta{color:var(--faint);font-size:11px;margin-top:5px}
.unread-mark{width:8px;height:8px;border-radius:50%;background:var(--info);flex:none;margin-top:6px}
.empty{color:var(--muted);text-align:center;padding:40px 20px;font-size:13px}
.linklike{background:none;border:none;color:var(--navy);font-size:12px;padding:0;font-weight:600}

@media(max-width:760px){
  .nav{position:fixed;left:0;top:58px;transform:translateX(-100%);transition:transform .2s;z-index:50}
  .nav.open{transform:translateX(0)}
  main{padding:16px}
  .brand small{display:none}
}
.navtoggle{display:none}
@media(max-width:760px){.navtoggle{display:grid}}
.tooltip{position:relative}
.tooltip:hover .tip{display:block}
.tip{display:none;position:absolute;top:130%;left:0;z-index:20;background:var(--ink);color:#fff;
  padding:8px 10px;border-radius:8px;font-size:11.5px;font-weight:500;width:240px;line-height:1.4;box-shadow:var(--shadow-lg)}
</style>
</head>
<body>
<div class="topbar">
  <button class="icon-btn navtoggle" id="navToggle" aria-label="Menu">☰</button>
  <div class="brand"><span class="mark">TW</span>
    <span>Tender Watch<small>Maharashtra procurement intelligence</small></span></div>
  <div class="spacer"></div>
  <button class="fresh" id="freshPill" aria-label="Data freshness — open status">
    <span class="dot unknown" id="freshDot"></span>
    <span id="freshText">Checking…</span>
  </button>
  <button class="btn ghost" id="refreshBtn" aria-label="Refresh data">
    <span id="refreshIco">↻</span><span id="refreshLbl">Refresh</span></button>
  <button class="icon-btn" id="bellBtn" aria-label="Notifications">🔔
    <span class="badge-count" id="bellCount" style="display:none">0</span></button>
</div>

<div class="status-panel" id="statusPanel" role="dialog" aria-label="Data status"></div>

<div class="shell">
  <nav class="nav" id="nav">
    <a data-page="overview" class="active"><span class="ico">▤</span>Overview</a>
    <a data-page="tenders"><span class="ico">▦</span>Tenders<span class="tag" id="navTenders">0</span></a>
    <a data-page="awards"><span class="ico">✓</span>Awards<span class="tag" id="navAwards">0</span></a>
    <a data-page="contractors"><span class="ico">◧</span>Contractors<span class="tag" id="navContractors">0</span></a>
    <a data-page="analytics"><span class="ico">◔</span>Analytics</a>
    <div class="navlabel">Personal</div>
    <a data-page="watchlist"><span class="ico">★</span>Watchlist<span class="tag" id="navWatch">0</span></a>
    <a data-page="inbox"><span class="ico">✉</span>Inbox<span class="tag" id="navInbox">0</span></a>
    <div class="navlabel">Tools</div>
    <a href="/unlock" target="_blank"><span class="ico">⇪</span>Import awards</a>
  </nav>

  <main>
    <section class="page active" id="page-overview"></section>
    <section class="page" id="page-tenders"></section>
    <section class="page" id="page-awards"></section>
    <section class="page" id="page-contractors"></section>
    <section class="page" id="page-analytics"></section>
    <section class="page" id="page-watchlist"></section>
    <section class="page" id="page-inbox"></section>
  </main>
</div>

<div class="overlay" id="overlay"></div>
<aside class="slideover" id="slideover" role="dialog" aria-label="Tender detail">
  <div class="so-head"><h3 id="soTitle">Detail</h3>
    <button class="so-close" id="soClose" aria-label="Close">✕</button></div>
  <div class="so-body" id="soBody"></div>
</aside>
<aside class="drawer" id="drawer" role="dialog" aria-label="Notifications">
  <div class="dh"><h3>Notifications</h3>
    <button class="linklike" id="markAllRead">Mark all read</button>
    <button class="so-close" id="drawerClose" aria-label="Close">✕</button></div>
  <div class="dtools">
    <div class="seg" id="inboxUnreadSeg">
      <button data-f="unread" class="on">Unread</button>
      <button data-f="all">All</button></div>
    <select id="inboxKind">
      <option value="">All types</option>
      <option value="procurement">Awards</option>
      <option value="historical_import">Historical</option>
      <option value="data_correction">Corrections</option>
    </select>
    <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)">
      <input type="checkbox" id="inboxWatched"> Watched only</label>
  </div>
  <div class="dlist" id="drawerList"></div>
</aside>

<script>
var DATA = __DATA_JSON__;
var SECTIONS = __SECTIONS_JSON__;
var CONTRACTORS = __CONTRACTORS_JSON__;
var ANALYTICS = __ANALYTICS_JSON__;
var NOTIFS = __NOTIFS_JSON__;
var STATUS = __STATUS_JSON__;
var AWARDS = __AWARDS_JSON__;
var OVERVIEW = __OVERVIEW_JSON__;

/* ---------- small helpers ---------- */
function $(s,r){return (r||document).querySelector(s)}
function $all(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function el(html){var d=document.createElement('div');d.innerHTML=html.trim();return d.firstChild;}
function money(fmt){return fmt?('₹'+fmt):'<span class="unknown">Unknown</span>';}
function shortId(t){return t.title && t.title!==t.id ? esc(t.title) : esc(t.id);}

/* localStorage-backed per-viewer state (documented as per-browser). */
var LS={
  get:function(k,d){try{var v=localStorage.getItem('tw_'+k);return v?JSON.parse(v):d;}catch(e){return d;}},
  set:function(k,v){try{localStorage.setItem('tw_'+k,JSON.stringify(v));}catch(e){}}
};
var watchT=LS.get('watch_tenders',{});         // id -> true
var watchC=LS.get('watch_contractors',{});      // key -> true
var readN =LS.get('read_notifs',{});            // id -> true
var lastVisit=LS.get('last_visit',0);
function saveWatch(){LS.set('watch_tenders',watchT);LS.set('watch_contractors',watchC);renderNavCounts();}

/* ---------- freshness top bar ---------- */
function timeAgo(iso){
  if(!iso) return '';
  var t=Date.parse(iso); if(isNaN(t)) return '';
  var s=Math.floor((Date.now()-t)/1000);
  if(s<0) s=0;
  if(s<60) return 'just now';
  var m=Math.floor(s/60); if(m<60) return m+' minute'+(m!=1?'s':'')+' ago';
  var h=Math.floor(m/60); if(h<24) return h+' hour'+(h!=1?'s':'')+' ago';
  var d=Math.floor(h/24); return d+' day'+(d!=1?'s':'')+' ago';
}
var STATUS_LABEL={up_to_date:'Up to date',partial:'Partial',stale:'Stale',
  failed:'Collection failed',unknown:'Unknown'};
function renderFresh(){
  var st=STATUS.overall_status||'unknown';
  var cls={up_to_date:'up',partial:'partial',stale:'stale',failed:'failed',unknown:'unknown'}[st]||'unknown';
  $('#freshDot').className='dot '+cls;
  var ago=timeAgo(STATUS.last_source_collection);
  $('#freshText').innerHTML=(STATUS_LABEL[st]||'Unknown')+(ago?(' · Checked '+ago):'');
}
function renderStatusPanel(){
  var s=STATUS, srcs=s.sources||[];
  var next=s.next_scheduled_run?('Every '+s.cadence_minutes+' min (next ~'+
     (timeAgoFuture(s.next_scheduled_run))+')'):'—';
  var html='<div style="display:flex;align-items:center;gap:10px">'+
    '<span class="dot '+({up_to_date:'up',partial:'partial',stale:'stale',failed:'failed',unknown:'unknown'}[s.overall_status]||'unknown')+'"></span>'+
    '<b>'+(STATUS_LABEL[s.overall_status]||'Unknown')+'</b>'+
    '<span style="color:var(--muted);font-size:12px">Source collection is the portal scrape; processing is the pipeline run.</span></div>'+
    '<div class="status-grid">'+
      cell('Last successful collection', s.last_source_collection_label||'never')+
      cell('Relative age', timeAgo(s.last_source_collection)||'—')+
      cell('Last attempt', fmtIso(s.last_attempt))+
      cell('Last material change', fmtIso(s.last_material_change))+
      cell('Next scheduled run', next)+
    '</div>';
  if(srcs.length){
    html+='<table class="src-table"><thead><tr><th>Source</th><th>Last run</th>'+
      '<th class="r">New</th><th class="r">Updated</th><th>Status</th></tr></thead><tbody>';
    srcs.forEach(function(x){
      html+='<tr><td>'+esc(x.portal)+'</td><td>'+esc(x.last_run_label||'—')+'</td>'+
        '<td class="r num">'+x.new+'</td><td class="r num">'+x.updated+'</td>'+
        '<td><span class="badge '+(x.status==='ok'?'live':'urgent')+'">'+esc(x.status)+
        (x.errors?(' · '+x.errors+' err'):'')+'</span></td></tr>';
    });
    html+='</tbody></table>';
  }
  html+='<p style="color:var(--muted);font-size:11.5px;margin:12px 0 0">Reload shows the '+
    'last committed snapshot; it does not re-scrape. Use Refresh to fetch fresh live tenders.</p>';
  $('#statusPanel').innerHTML=html;
}
function cell(k,v){return '<div class="cell"><div class="k">'+esc(k)+'</div><div class="v">'+esc(v)+'</div></div>';}
function fmtIso(iso){if(!iso) return '—';var d=new Date(iso);if(isNaN(d)) return esc(iso);
  return d.toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',
    hour:'2-digit',minute:'2-digit',hour12:false})+' IST';}
function timeAgoFuture(iso){var t=Date.parse(iso);if(isNaN(t)) return '';var s=Math.floor((t-Date.now())/1000);
  if(s<=0) return 'due';var m=Math.ceil(s/60);return 'in '+m+' min';}

/* ---------- navigation ---------- */
function show(page){
  $all('.nav a[data-page]').forEach(function(a){a.classList.toggle('active',a.dataset.page===page);});
  $all('.page').forEach(function(p){p.classList.remove('active');});
  var node=$('#page-'+page); if(node){node.classList.add('active');}
  if(page==='overview') renderOverview();
  if(page==='tenders') renderTenders();
  if(page==='awards') renderAwards();
  if(page==='contractors') renderContractors();
  if(page==='analytics') renderAnalytics();
  if(page==='watchlist') renderWatchlist();
  if(page==='inbox') renderInboxPage();
  if(location.hash!=='#'+page) history.replaceState(null,'','#'+page);
  $('#nav').classList.remove('open');
  window.scrollTo(0,0);
}
$all('.nav a[data-page]').forEach(function(a){a.addEventListener('click',function(e){
  e.preventDefault();show(a.dataset.page);});});
$('#navToggle').addEventListener('click',function(){$('#nav').classList.toggle('open');});

function renderNavCounts(){
  $('#navTenders').textContent=DATA.tenders.filter(function(t){return t.live;}).length;
  $('#navAwards').textContent=AWARDS.length;
  $('#navContractors').textContent=(CONTRACTORS.contractors||[]).length;
  var w=Object.keys(watchT).length+Object.keys(watchC).length;
  $('#navWatch').textContent=w;
  var unread=(NOTIFS.notifications||[]).filter(function(n){return !readN[n.id];}).length;
  $('#navInbox').textContent=unread;
  $('#bellCount').textContent=unread;
  $('#bellCount').style.display=unread?'grid':'none';
}

/* ---------- status badge helper ---------- */
function stBadge(t){
  var cls=t.st, lbl=t.stLabel;
  return '<span class="badge '+esc(cls)+'">'+esc(lbl)+'</span>';
}
function starBtn(kind,id,on){
  return '<button class="star '+(on?'on':'')+'" data-star="'+kind+'" data-id="'+esc(id)+
    '" title="Watch" aria-label="Watch">'+(on?'★':'☆')+'</button>';
}

/* ---------- OVERVIEW ---------- */
function renderOverview(){
  var o=OVERVIEW, p=$('#page-overview');
  var tiles=o.tiles.map(function(t){
    return '<div class="tile"><div class="label">'+esc(t.label)+
      '<span class="help tooltip">?<span class="tip">'+esc(t.definition)+
      '</span></span></div><div class="val num">'+esc(t.value)+
      '</div><div class="period">'+esc(t.period)+'</div></div>';
  }).join('');
  /* since last visit */
  var newAwards=(NOTIFS.notifications||[]).filter(function(n){
    return n.kind==='procurement' && Date.parse(n.detected_at)>lastVisit;});
  var watchedHits=(NOTIFS.notifications||[]).filter(function(n){
    return (watchT[n.tender_id]||watchC[n.contractorKey])&&Date.parse(n.detected_at)>lastVisit;});
  var since='';
  if(lastVisit){
    since='<div class="callout"><b>Since your last visit</b> ('+fmtIso(new Date(lastVisit).toISOString())+
      '): '+newAwards.length+' new award notice'+(newAwards.length!=1?'s':'')+', '+
      watchedHits.length+' affecting your watchlist.</div>';
  }
  var closing=o.closing_soon.map(function(t){
    return '<tr data-open="'+esc(t.id)+'"><td class="t-title">'+shortId(t)+
      '<div class="t-sub">'+esc(t.org||'')+'</div></td>'+
      '<td>'+stBadge(t)+'</td><td class="r">'+esc(t.closing||'—')+'</td></tr>';
  }).join('')||'<tr><td colspan="3" class="empty">No deadlines within 7 days.</td></tr>';
  var recent=o.recent_awards.map(function(a){
    return '<tr data-open="'+esc(a.id)+'"><td class="t-title">'+esc(a.title||a.id)+
      '<div class="t-sub">'+esc(a.contractor)+'</div></td>'+
      '<td class="r">'+money(a.awardValueFmt&&a.awardValueFmt!=='Unknown'?a.awardValueFmt:'')+'</td>'+
      '<td class="r">'+esc(a.contractDate||'—')+'</td></tr>';
  }).join('')||'<tr><td colspan="3" class="empty">No awards on record yet.</td></tr>';

  p.innerHTML='<h1>Overview</h1><p class="sub">Your procurement decision homepage. '+
    'Figures are drawn from the last committed data snapshot.</p>'+
    since+'<div class="tiles">'+tiles+'</div>'+
    '<div class="grid2">'+
      '<div class="card"><div class="head"><h2>Closing within 7 days</h2>'+
        '<div class="spacer"></div><button class="linklike" data-goto="tenders">All tenders →</button></div>'+
        '<div class="tablewrap"><table class="data"><thead><tr><th class="no-sort">Tender</th>'+
        '<th class="no-sort">Status</th><th class="no-sort r">Closes</th></tr></thead><tbody>'+closing+
        '</tbody></table></div></div>'+
      '<div class="card"><div class="head"><h2>Recently detected awards</h2>'+
        '<div class="spacer"></div><button class="linklike" data-goto="awards">All awards →</button></div>'+
        '<div class="tablewrap"><table class="data"><thead><tr><th class="no-sort">Work / winner</th>'+
        '<th class="no-sort r">Award value</th><th class="no-sort r">Contract date</th></tr></thead><tbody>'+
        recent+'</tbody></table></div></div>'+
    '</div>';
  wireOpens(p); wireGoto(p);
}

/* ---------- TENDERS (open opportunities only) ---------- */
var tState={q:'',source:'',city:'',status:'',sort:'publishedTs',dir:-1};
function openTenders(){return DATA.tenders.filter(function(t){return t.live;});}
function renderTenders(){
  var p=$('#page-tenders');
  if(!$('#tFilters',p)){
    p.innerHTML='<h1>Open tenders</h1><p class="sub">Opportunities currently open for bidding on the '+
      'portal. Estimated value is the pre-bid estimate (not the award). Awarded and closed tenders move '+
      'to the Awards view. Click a row for full details and history.</p>'+
      '<div class="filters" id="tFilters">'+
      '<input type="search" id="tSearch" placeholder="Search title, id, department…">'+
      '<select id="tSource"><option value="">All sources</option>'+
        DATA.sources.map(function(s){return '<option>'+esc(s)+'</option>';}).join('')+'</select>'+
      '<select id="tCity"><option value="">All districts</option>'+
        DATA.cities.map(function(c){return '<option>'+esc(c)+'</option>';}).join('')+'</select>'+
      '<select id="tStatus"><option value="">Any deadline</option>'+
        '<option value="live">Open</option><option value="soon">Closing soon</option>'+
        '<option value="urgent">Urgent (48h)</option></select>'+
      '<span class="count" id="tCount"></span></div>'+
      '<div class="card"><div class="tablewrap"><table class="data" id="tTable"></table></div></div>';
    $('#tSearch',p).addEventListener('input',function(){tState.q=this.value.toLowerCase();drawTenders();});
    $('#tSource',p).addEventListener('change',function(){tState.source=this.value;drawTenders();});
    $('#tCity',p).addEventListener('change',function(){tState.city=this.value;drawTenders();});
    $('#tStatus',p).addEventListener('change',function(){tState.status=this.value;drawTenders();});
  }
  drawTenders();
}
function tendersFiltered(){
  return openTenders().filter(function(t){
    if(tState.source && t.sources.indexOf(tState.source)<0) return false;
    if(tState.city && t.cityGroup!==tState.city) return false;
    if(tState.status && t.st!==tState.status) return false;
    if(tState.q){
      var hay=(t.title+' '+t.id+' '+t.org+' '+(t.ref||'')).toLowerCase();
      if(hay.indexOf(tState.q)<0) return false;
    }
    return true;
  });
}
var T_COLS=[['','',0],['Tender','title',0],['Ref no','ref',0],['District','cityGroup',0],
  ['Estimate','valueNum',1],['Published','publishedTs',1],['Closes','closingTs',1],
  ['Opening','openingTs',1],['Status','st',0]];
function drawTenders(){
  var rows=tendersFiltered();
  rows.sort(function(a,b){var k=tState.sort,va=a[k],vb=b[k];
    if(typeof va==='string'){va=(va||'').toLowerCase();vb=(vb||'').toLowerCase();}
    return (va<vb?-1:va>vb?1:0)*tState.dir;});
  var head='<thead><tr>'+T_COLS.map(function(c,i){
    var sorted=tState.sort===c[1]&&c[1];
    return '<th class="'+(c[2]?'r ':'')+(c[1]?'':'no-sort ')+(sorted?'sorted':'')+'" data-col="'+esc(c[1])+'">'+
      esc(c[0])+(c[1]?' <span class="sortcaret">'+(sorted?(tState.dir>0?'▲':'▼'):'↕')+'</span>':'')+'</th>';
  }).join('')+'</tr></thead>';
  var body=rows.map(function(t){
    return '<tr data-open="'+esc(t.id)+'"><td>'+starBtn('t',t.id,!!watchT[t.id])+'</td>'+
      '<td class="t-title">'+shortId(t)+
        '<div class="t-sub">'+esc(t.org||'')+' · '+t.sources.map(esc).join(', ')+'</div></td>'+
      '<td>'+esc(t.ref||'—')+'</td>'+
      '<td>'+esc(t.cityGroup||'')+'</td>'+
      '<td class="r">'+money(t.valueFmt)+'</td>'+
      '<td class="r">'+esc(t.published||'—')+'</td>'+
      '<td class="r">'+esc(t.closing||'—')+'</td>'+
      '<td class="r">'+esc(t.opening||'—')+'</td>'+
      '<td>'+stBadge(t)+'</td></tr>';
  }).join('')||'<tr><td colspan="9" class="empty">No open tenders match these filters.</td></tr>';
  $('#tTable').innerHTML=head+'<tbody>'+body+'</tbody>';
  $('#tCount').textContent=rows.length+' of '+openTenders().length+' open tenders';
  $all('#tTable th[data-col]').forEach(function(th){
    if(!th.dataset.col) return;
    th.addEventListener('click',function(){
      if(tState.sort===th.dataset.col) tState.dir*=-1; else {tState.sort=th.dataset.col;tState.dir=1;}
      drawTenders();
    });
  });
  wireOpens($('#page-tenders')); wireStars($('#page-tenders'));
}

/* ---------- AWARDS ---------- */
var aState={q:'',sort:'contractDateTs',dir:-1};
function renderAwards(){
  var p=$('#page-awards');
  if(!$('#aFilters',p)){
    p.innerHTML='<h1>Awards</h1><p class="sub">Confirmed Awards of Contract imported from the portal. '+
      'Award value is the contract amount; the estimate is shown separately. Amounts are exact to the '+
      'source (decimals preserved).</p>'+
      '<div class="filters" id="aFilters">'+
      '<input type="search" id="aSearch" placeholder="Search work, winner, department…">'+
      '<span class="count" id="aCount"></span></div>'+
      '<div class="card"><div class="tablewrap"><table class="data" id="aTable"></table></div></div>';
    $('#aSearch',p).addEventListener('input',function(){aState.q=this.value.toLowerCase();drawAwards();});
  }
  drawAwards();
}
var A_COLS=[['Work','title',0],['Winner','contractor',0],['Award value','awardValueNum',1],
  ['Estimate','estimate',1],['Contract date','contractDateTs',1],['District','city',0]];
function drawAwards(){
  var rows=AWARDS.filter(function(a){
    if(!aState.q) return true;
    return (a.title+' '+a.contractor+' '+a.org+' '+a.id).toLowerCase().indexOf(aState.q)>=0;
  });
  rows.sort(function(a,b){var k=aState.sort,va=a[k],vb=b[k];
    if(typeof va==='string'){va=(va||'').toLowerCase();vb=(vb||'').toLowerCase();}
    return (va<vb?-1:va>vb?1:0)*aState.dir;});
  var head='<thead><tr>'+A_COLS.map(function(c){
    var sorted=aState.sort===c[1];
    return '<th class="'+(c[2]?'r ':'')+(sorted?'sorted':'')+'" data-col="'+esc(c[1])+'">'+esc(c[0])+
      ' <span class="sortcaret">'+(sorted?(aState.dir>0?'▲':'▼'):'↕')+'</span></th>';
  }).join('')+'</tr></thead>';
  var body=rows.map(function(a){
    return '<tr data-open="'+esc(a.id)+'"><td class="t-title">'+esc(a.title||a.id)+
        '<div class="t-sub">'+esc(a.org||'')+'</div></td>'+
      '<td><a href="#" data-contractor="'+esc(a.contractorKey)+'">'+esc(a.contractor)+'</a></td>'+
      '<td class="r">'+money(a.awardValueFmt&&a.awardValueFmt!=='Unknown'?a.awardValueFmt:'')+'</td>'+
      '<td class="r">'+money(a.estimateFmt&&a.estimateFmt!=='Unknown'?a.estimateFmt:'')+'</td>'+
      '<td class="r">'+esc(a.contractDate||'—')+'</td>'+
      '<td>'+esc(a.city||'')+'</td></tr>';
  }).join('')||'<tr><td colspan="6" class="empty">No awards match.</td></tr>';
  $('#aTable').innerHTML=head+'<tbody>'+body+'</tbody>';
  $('#aCount').textContent=rows.length+' of '+AWARDS.length+' awards';
  $all('#aTable th[data-col]').forEach(function(th){th.addEventListener('click',function(){
    if(aState.sort===th.dataset.col) aState.dir*=-1; else {aState.sort=th.dataset.col;aState.dir=1;}
    drawAwards();});});
  $all('#aTable a[data-contractor]').forEach(function(a){a.addEventListener('click',function(e){
    e.preventDefault();e.stopPropagation();openContractor(a.dataset.contractor);});});
  wireOpens($('#page-awards'));
}

/* ---------- CONTRACTORS ---------- */
var cState={q:'',selected:null};
function renderContractors(){
  var p=$('#page-contractors');
  var list=CONTRACTORS.contractors||[];
  var st=CONTRACTORS.stats||{};
  p.innerHTML='<h1>Contractors</h1><p class="sub">Firms winning the tracked contracts, built from imported '+
    'Award of Contract pages. Ranked by known awarded value <b>across tenders we monitor</b> — not a '+
    'statewide total. Average is over contracts with a known amount.</p>'+
    '<div class="tiles">'+
      tile('Contractors',(st.contractors||0))+
      tile('Contracts',(st.contracts||0))+
      tile('Known awarded value','₹'+(st.valueFmt||'0'))+
      tile('With verified records',(st.enriched||0))+
    '</div>'+
    '<div class="grid2"><div>'+
      '<div class="filters"><input type="search" id="cSearch" placeholder="Search contractor…">'+
        '<span class="count" id="cCount"></span></div>'+
      '<div class="card"><div class="tablewrap"><table class="data" id="cTable"></table></div></div></div>'+
      '<div id="cProfile"></div></div>';
  $('#cSearch',p).addEventListener('input',function(){cState.q=this.value.toLowerCase();drawContractorList();});
  drawContractorList();
  if(cState.selected) openContractor(cState.selected);
  else $('#cProfile').innerHTML='<div class="card"><div class="body empty">'+
    'Select a contractor to see their profile, contracts and evidence.</div></div>';
}
function tile(l,v){return '<div class="tile"><div class="label">'+esc(l)+
  '</div><div class="val num">'+esc(v)+'</div></div>';}
function drawContractorList(){
  var list=(CONTRACTORS.contractors||[]).filter(function(c){
    return !cState.q || (c.name.toLowerCase().indexOf(cState.q)>=0);});
  var body=list.map(function(c){
    return '<tr data-contractor="'+esc(c.key)+'"><td>'+starBtn('c',c.key,!!watchC[c.key])+'</td>'+
      '<td class="t-title">'+esc(c.name)+'<div class="t-sub">'+(c.departments[0]?esc(c.departments[0]):'')+
      (c.cities[0]?(' · '+esc(c.cities[0])):'')+'</div></td>'+
      '<td class="r num">'+c.count+'</td>'+
      '<td class="r">'+money(c.valueFmt)+'</td></tr>';
  }).join('')||'<tr><td colspan="4" class="empty">No contractors match.</td></tr>';
  $('#cTable').innerHTML='<thead><tr><th class="no-sort"></th><th class="no-sort">Contractor</th>'+
    '<th class="no-sort r">Contracts</th><th class="no-sort r">Known value</th></tr></thead><tbody>'+body+'</tbody>';
  $('#cCount').textContent=list.length+' contractors';
  $all('#cTable tr[data-contractor]').forEach(function(tr){tr.addEventListener('click',function(e){
    if(e.target.closest('.star')) return; openContractor(tr.dataset.contractor);});});
  wireStars($('#page-contractors'));
}
function openContractor(key){
  cState.selected=key;
  if(!$('#page-contractors').classList.contains('active')){show('contractors');return;}
  var c=(CONTRACTORS.contractors||[]).filter(function(x){return x.key===key;})[0];
  var box=$('#cProfile'); if(!c){box.innerHTML='';return;}
  var en=c.enrichment;
  var enHtml='';
  if(en){
    if(en.verified){
      enHtml='<div class="section-title">Verified public records</div><div class="kv">'+
        (en.entity_type?('<div class="k">Entity type</div><div>'+esc(en.entity_type)+'</div>'):'')+
        (en.gstin?('<div class="k">GSTIN</div><div class="num">'+esc(en.gstin)+'</div>'):'')+
        (en.udyam?('<div class="k">Udyam</div><div class="num">'+esc(en.udyam)+'</div>'):'')+
        (en.location?('<div class="k">Business location</div><div>'+esc(en.location)+'</div>'):'')+
        (en.website?('<div class="k">Website</div><div><a href="'+esc(en.website)+'" target="_blank" rel="noopener">'+esc(en.website)+'</a></div>'):'')+
        (en.source?('<div class="k">Source</div><div>'+esc(en.source)+'</div>'):'')+
        '</div>';
    } else if(en.summary){
      enHtml='<div class="section-title">Research note</div><p style="color:var(--muted);font-size:12.5px">'+
        esc(en.summary)+'</p>';
    }
  }
  var q=encodeURIComponent(c.name+' Maharashtra contractor');
  var ext='<div class="section-title">External research</div><div class="so-actions">'+
    '<a class="btn" target="_blank" rel="noopener" href="https://www.google.com/search?q='+q+'">Google</a>'+
    '<a class="btn" target="_blank" rel="noopener" href="https://www.zaubacorp.com/companysearch/'+
      encodeURIComponent(c.name)+'">ZaubaCorp</a></div>';
  var contracts=c.contracts.map(function(x){
    return '<tr data-open="'+esc(x.id)+'"><td class="t-title">'+esc(x.title||x.id)+
      '<div class="t-sub">'+esc(x.org||'')+(x.city?(' · '+esc(x.city)):'')+'</div></td>'+
      '<td class="r">'+(x.valueKnown?money(x.valueFmt):'<span class="unknown">Unknown</span>')+'</td>'+
      '<td class="r">'+esc(x.date||'—')+'</td></tr>';
  }).join('');
  box.innerHTML='<div class="card"><div class="head"><h2>'+esc(c.name)+'</h2>'+
    '<div class="spacer"></div>'+starBtn('c',c.key,!!watchC[c.key])+'</div><div class="body">'+
    '<div class="kv">'+
      '<div class="k">Tracked contracts</div><div>'+c.count+'</div>'+
      '<div class="k">Known awarded value</div><div>'+money(c.valueFmt)+'</div>'+
      '<div class="k">Average (known only)</div><div>'+(c.avgFmt?money(c.avgFmt):'<span class="unknown">n/a</span>')+
        ' <span style="color:var(--faint)">('+c.knownValueCount+' of '+c.count+' with amounts)</span></div>'+
      '<div class="k">Latest award</div><div>'+esc(c.lastDate||'—')+'</div>'+
      '<div class="k">Departments</div><div>'+(c.departments.map(function(d){return '<span class="chip">'+esc(d)+'</span>';}).join('')||'—')+'</div>'+
      '<div class="k">Work locations</div><div>'+(c.cities.map(function(d){return '<span class="chip">'+esc(d)+'</span>';}).join('')||'—')+'</div>'+
    '</div>'+
    (c.competitors&&c.competitors.length?('<div class="section-title">Also seen bidding with</div><div>'+
      c.competitors.slice(0,10).map(function(x){return '<span class="chip">'+esc(x.name)+' ('+x.count+')</span>';}).join('')+
      '<p style="color:var(--faint);font-size:11px;margin:6px 0 0">Only where full bid lists were observed; sparse for winner-only imports.</p></div>'):'')+
    enHtml+ext+
    '<div class="section-title">Contracts</div><div class="tablewrap"><table class="data">'+
      '<thead><tr><th class="no-sort">Work</th><th class="no-sort r">Award value</th>'+
      '<th class="no-sort r">Date</th></tr></thead><tbody>'+contracts+'</tbody></table></div>'+
    '</div></div>';
  wireOpens(box); wireStars(box);
}

/* ---------- ANALYTICS ---------- */
function bars(items,labelKey,valKey,fmt,max){
  var mx=max||Math.max.apply(null,items.map(function(i){return +i[valKey]||0;}).concat([1]));
  return items.map(function(i){
    var v=+i[valKey]||0, pct=mx>0?(v/mx*100):0;
    return '<div class="bar-row"><span class="lbl" title="'+esc(i[labelKey])+'">'+esc(i[labelKey])+'</span>'+
      '<span class="bar-track"><span class="bar-fill" style="width:'+pct.toFixed(1)+'%"></span></span>'+
      '<span class="val">'+esc(fmt?fmt(i):v)+'</span></div>';
  }).join('');
}
function renderAnalytics(){
  var p=$('#page-analytics'), a=ANALYTICS;
  if(!a||!a.summary){p.innerHTML='<h1>Analytics</h1><p class="sub">No analytics available yet.</p>';return;}
  var s=a.summary, rec=a.reconciliation||{}, bid=a.bidding||{};
  var vb=a.value_bands||[];
  var tiles='<div class="tiles">'+
    '<div class="tile"><div class="label">Floated (estimated) value<span class="help tooltip">?'+
      '<span class="tip">Sum of pre-bid estimated values across tracked tenders with a known estimate ('+
      (s.tenders_with_known_estimate||0)+'). Not the award total.</span></span></div>'+
      '<div class="val num">₹'+esc(s.total_floated_value_fmt||'0')+'</div><div class="period">estimate</div></div>'+
    '<div class="tile"><div class="label">Awarded (contract) value<span class="help tooltip">?'+
      '<span class="tip">Sum of awarded contract values across '+(s.awards_with_known_value||0)+
      ' awards with a known amount. A different measure from floated value; not compared 1:1.</span></span></div>'+
      '<div class="val num">₹'+esc(s.total_awarded_value_fmt||'0')+'</div><div class="period">awarded</div></div>'+
    '<div class="tile"><div class="label">Tracked tenders<span class="help tooltip">?<span class="tip">'+
      esc(rec.definition||'')+'</span></span></div><div class="val num">'+(rec.canonical_tenders||s.counts.tenders)+
      '</div><div class="period">'+(rec.live||0)+' live · '+(rec.awarded||0)+' awarded</div></div>'+
    '<div class="tile"><div class="label">Awards on record</div><div class="val num">'+(s.counts.awards||0)+
      '</div><div class="period">'+(rec.contractors||0)+' contractors</div></div>'+
    '</div>';

  var biddingCard='<div class="card"><div class="head"><h2>Bidding competition</h2>'+
    '<div class="desc">measurable only with full bidder lists</div></div><div class="body">'+
    '<div class="callout">Single-bidder rate is <b>not measurable</b> from the current data: the portal\'s '+
    'Award-of-Contract page lists only the winner. '+(bid.winner_only||0)+' of '+(bid.awards_total||0)+
    ' awards are winner-only, so a bidder count of 1 is not evidence of one participant.</div>'+
    '<div class="kv"><div class="k">Awards total</div><div>'+(bid.awards_total||0)+'</div>'+
    '<div class="k">With full bidder list</div><div>'+(bid.with_full_bidder_list||0)+'</div>'+
    '<div class="k">Single-bidder rate</div><div>'+(bid.single_bidder_rate==null?
      '<span class="unknown">Insufficient data</span>':(pct(bid.single_bidder_rate)+' over '+bid.single_bidder_measurable)) +
    '</div></div></div></div>';

  var districts=(a.by_district||[]).slice(0,12);
  var districtCard='<div class="card"><div class="head"><h2>Estimated value by district</h2>'+
    '<div class="desc">floated estimate, top 12</div></div><div class="body">'+
    (districts.length?bars(districts,'district','floated_inr',function(i){return '₹'+i.floated_fmt;}):'<div class="empty">No data</div>')+
    '</div></div>';

  var schemes=(a.by_funding_scheme||[]);
  var schemeCard='<div class="card"><div class="head"><h2>Tenders by funding scheme</h2>'+
    '<div class="desc">unclassified kept visible</div></div><div class="body">'+
    (schemes.length?bars(schemes,'scheme','tenders',function(i){return i.tenders+' · ₹'+i.floated_fmt;}):'<div class="empty">No data</div>')+
    '</div></div>';

  var bandMax=Math.max.apply(null,vb.map(function(b){return b.count;}).concat([1]));
  var bandCard='<div class="card"><div class="head"><h2>Estimated-value distribution</h2>'+
    '<div class="desc">unknown estimates shown separately</div></div><div class="body">'+
    bars(vb,'band','count',function(i){return i.count;},bandMax)+'</div></div>';

  var tc=(a.top_contractors||[]).slice(0,12);
  var tcCard='<div class="card"><div class="head"><h2>Top contractors (tracked)</h2>'+
    '<div class="desc">by known awarded value, monitored tenders only</div></div><div class="body">'+
    (tc.length?bars(tc,'contractor','total_value_inr',function(i){return '₹'+i.total_value_fmt;}):'<div class="empty">No data</div>')+
    '</div></div>';

  var sb=(a.single_bidder||[]);
  var sbCard='<div class="card"><div class="head"><h2>Single-bidder rate by publisher</h2>'+
    '<div class="desc">full-coverage awards only (0–100%)</div></div><div class="body">'+
    (sb.length?('<div class="scale"><span>0%</span><span>50%</span><span>100%</span></div>'+
      bars(sb,'org','single_bidder_rate',function(i){return pct(i.single_bidder_rate);},1)):
      '<div class="empty">No full bidder lists observed yet, so this is not computed. '+
      'Winner-only imports are excluded to avoid a false 100%.</div>')+
    '</div></div>';

  p.innerHTML='<h1>Analytics</h1><p class="sub">Estimated and awarded values are separate measures. '+
    'Percentages use a fixed 0–100% scale; zero shows an empty bar; unknowns are labelled, not zeroed. '+
    'Coverage reflects the tenders we monitor, not all of Maharashtra.</p>'+
    tiles+biddingCard+'<div class="grid2">'+districtCard+bandCard+'</div>'+
    '<div class="grid2">'+schemeCard+tcCard+'</div>'+sbCard;
}
function pct(x){return (x==null?'—':(Math.round(x*1000)/10)+'%');}

/* ---------- WATCHLIST ---------- */
function renderWatchlist(){
  var p=$('#page-watchlist');
  var wt=DATA.tenders.filter(function(t){return watchT[t.id];});
  var wc=(CONTRACTORS.contractors||[]).filter(function(c){return watchC[c.key];});
  var tRows=wt.map(function(t){return '<tr data-open="'+esc(t.id)+'"><td class="t-title">'+shortId(t)+
    '<div class="t-sub">'+esc(t.org||'')+'</div></td><td>'+stBadge(t)+'</td>'+
    '<td class="r">'+esc(t.closing||'—')+'</td></tr>';}).join('')||
    '<tr><td colspan="3" class="empty">No watched tenders. Tap ☆ on any tender.</td></tr>';
  var cRows=wc.map(function(c){return '<tr data-contractor="'+esc(c.key)+'"><td class="t-title">'+esc(c.name)+
    '</td><td class="r num">'+c.count+'</td><td class="r">'+money(c.valueFmt)+'</td></tr>';}).join('')||
    '<tr><td colspan="3" class="empty">No watched contractors. Tap ☆ on any contractor.</td></tr>';
  p.innerHTML='<h1>Watchlist</h1><p class="sub">Tenders and contractors you follow. Stored in this browser. '+
    'Award notices affecting these appear in your Inbox and the "Since your last visit" summary.</p>'+
    '<div class="grid2"><div class="card"><div class="head"><h2>Watched tenders</h2></div>'+
      '<div class="tablewrap"><table class="data"><thead><tr><th class="no-sort">Tender</th>'+
      '<th class="no-sort">Status</th><th class="no-sort r">Closes</th></tr></thead><tbody>'+tRows+
      '</tbody></table></div></div>'+
    '<div class="card"><div class="head"><h2>Watched contractors</h2></div>'+
      '<div class="tablewrap"><table class="data"><thead><tr><th class="no-sort">Contractor</th>'+
      '<th class="no-sort r">Contracts</th><th class="no-sort r">Known value</th></tr></thead><tbody>'+cRows+
      '</tbody></table></div></div></div>';
  wireOpens(p);
  $all('#page-watchlist tr[data-contractor]').forEach(function(tr){tr.addEventListener('click',function(){
    openContractor(tr.dataset.contractor);});});
}

/* ---------- INBOX ---------- */
var inbox={filter:'unread',kind:'',watched:false};
function notifItems(){
  return (NOTIFS.notifications||[]).filter(function(n){
    if(inbox.filter==='unread' && readN[n.id]) return false;
    if(inbox.kind && n.kind!==inbox.kind) return false;
    if(inbox.watched && !(watchT[n.tender_id]||watchC[n.contractorKey])) return false;
    return true;
  });
}
function notifHtml(n){
  return '<div class="notif '+(readN[n.id]?'':'unread')+'" data-nid="'+esc(n.id)+'" data-open="'+esc(n.tender_id)+'">'+
    (readN[n.id]?'':'<span class="unread-mark"></span>')+
    '<div class="nd"><div class="nt"><span class="badge kind-'+esc(n.kind)+'">'+
      esc(n.kind.replace('_',' '))+'</span></div>'+
    '<div class="nm">'+esc(n.message)+'</div>'+
    '<div class="nmeta">'+esc(n.detected_label||'')+'</div></div></div>';
}
function renderInboxPage(){
  var p=$('#page-inbox');
  var items=notifItems();
  var byKind=NOTIFS.by_kind||{};
  p.innerHTML='<h1>Inbox</h1><p class="sub">Award notices, historical imports and data corrections are kept '+
    'distinguishable. Read state is stored in this browser.</p>'+
    '<div class="filters">'+
      '<div class="seg" style="display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden">'+
        '<button class="linklike" style="padding:8px 12px" data-if="unread">Unread</button>'+
        '<button class="linklike" style="padding:8px 12px" data-if="all">All</button></div>'+
      '<select id="ibKind"><option value="">All types ('+(NOTIFS.count||0)+')</option>'+
        '<option value="procurement">Awards ('+(byKind.procurement||0)+')</option>'+
        '<option value="historical_import">Historical ('+(byKind.historical_import||0)+')</option>'+
        '<option value="data_correction">Corrections ('+(byKind.data_correction||0)+')</option></select>'+
      '<label style="display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12.5px">'+
        '<input type="checkbox" id="ibWatched"> Watched only</label>'+
      '<button class="btn" id="ibMarkAll" style="margin-left:auto">Mark all read</button></div>'+
    '<div class="card" id="ibList"></div>';
  $('#ibKind').value=inbox.kind; $('#ibWatched').checked=inbox.watched;
  $('#ibKind').addEventListener('change',function(){inbox.kind=this.value;renderInboxPage();});
  $('#ibWatched').addEventListener('change',function(){inbox.watched=this.checked;renderInboxPage();});
  $all('#page-inbox [data-if]').forEach(function(b){b.addEventListener('click',function(){
    inbox.filter=b.dataset.if;renderInboxPage();});});
  $('#ibMarkAll').addEventListener('click',markAllRead);
  $('#ibList').innerHTML=items.length?items.map(notifHtml).join(''):
    '<div class="empty">Nothing here. Try "All".</div>';
  wireNotifs($('#ibList'));
}

/* ---------- notifications drawer ---------- */
function renderDrawer(){
  var items=(NOTIFS.notifications||[]).filter(function(n){
    if(inbox.filter==='unread' && readN[n.id]) return false;
    if(inbox.kind && n.kind!==inbox.kind) return false;
    if(inbox.watched && !(watchT[n.tender_id]||watchC[n.contractorKey])) return false;
    return true;
  }).slice(0,60);
  $('#drawerList').innerHTML=items.length?items.map(notifHtml).join(''):
    '<div class="empty">No notifications.</div>';
  wireNotifs($('#drawerList'));
  $all('#inboxUnreadSeg button').forEach(function(b){b.classList.toggle('on',b.dataset.f===inbox.filter);});
  $('#inboxKind').value=inbox.kind; $('#inboxWatched').checked=inbox.watched;
}
function markRead(id){readN[id]=true;LS.set('read_notifs',readN);renderNavCounts();}
function markAllRead(){(NOTIFS.notifications||[]).forEach(function(n){readN[n.id]=true;});
  LS.set('read_notifs',readN);renderNavCounts();renderDrawer();
  if($('#page-inbox').classList.contains('active')) renderInboxPage();}
function wireNotifs(root){
  $all('.notif',root).forEach(function(node){node.addEventListener('click',function(){
    markRead(node.dataset.nid);
    var id=node.dataset.open;
    if(id && /^\d/.test(id)) openDetail(id);
    node.classList.remove('unread');
    var m=node.querySelector('.unread-mark'); if(m) m.remove();
  });});
}

/* ---------- detail slide-over ---------- */
function openDetail(id){
  var t=DATA.tenders.filter(function(x){return x.id===id;})[0];
  var aw=AWARDS.filter(function(x){return x.id===id;})[0];
  $('#soTitle').textContent=(t&&t.title)||id;
  $('#soBody').innerHTML='<p class="sub">'+esc(id)+'</p><div class="empty">Loading…</div>';
  openOverlay('#slideover');
  fetch('/api/detail?id='+encodeURIComponent(id)).then(function(r){return r.json();})
    .then(function(d){renderDetail(id,t,aw,d);})
    .catch(function(){ $('#soBody').innerHTML='<div class="empty">Could not load details.</div>'; });
}
function renderDetail(id,t,aw,d){
  var body='';
  var title=(t&&t.title)|| (d.details&&d.details.Title) || id;
  body+='<p class="sub">'+esc(id)+'</p>';
  if(t){
    body+='<div style="margin-bottom:10px">'+stBadge(t)+' '+starBtn('t',id,!!watchT[id])+'</div>';
    body+='<div class="kv">'+
      '<div class="k">Department</div><div>'+esc(t.org||'—')+'</div>'+
      '<div class="k">District</div><div>'+esc(t.cityGroup||'—')+'</div>'+
      '<div class="k">Estimated value</div><div>'+money(t.valueFmt)+'</div>'+
      '<div class="k">Published</div><div>'+esc(t.published||'—')+'</div>'+
      '<div class="k">Closes</div><div>'+esc(t.closing||'—')+'</div>'+
      '<div class="k">Opening</div><div>'+esc(t.opening||'—')+'</div>'+
      '<div class="k">Sources</div><div>'+((t.sources||[]).map(function(s){return '<span class="chip">'+esc(s)+'</span>';}).join(''))+'</div>'+
    '</div>';
  }
  /* award panel */
  var award=d.award&&d.award.award;
  if(award && award.contractor){
    body+='<div class="section-title">Award</div><div class="kv">'+
      '<div class="k">Winner</div><div><a href="#" data-contractor="'+esc(cKey(award.contractor))+'">'+esc(award.contractor)+'</a></div>'+
      '<div class="k">Award value</div><div>'+money(fmtAmt(award.awarded_value))+
        (award.awarded_value_raw?(' <span style="color:var(--faint)">('+esc(award.awarded_value_raw)+')</span>'):'')+'</div>'+
      '<div class="k">Contract date</div><div>'+esc(award.contract_date||'—')+'</div>'+
    '</div>';
    if(award.bidders&&award.bidders.length){
      body+='<div class="section-title">Observed bidders</div><div>'+
        award.bidders.map(function(b){return '<span class="chip">'+esc(b.name)+(b.status?(' · '+esc(b.status)):'')+'</span>';}).join('')+
        '<p style="color:var(--faint);font-size:11px;margin:6px 0 0">Only the winner is listed on the AOC page; this is not the full participant list.</p></div>';
    }
  }
  /* timeline */
  var events=d.events||[];
  if(events.length){
    body+='<div class="section-title">History</div><ul class="timeline">';
    events.forEach(function(ev){
      var det={}; try{det=JSON.parse(ev.detail||'{}');}catch(e){}
      var label=ev.event_type==='award_record_discovered'?'Award record discovered':
        (ev.event_type==='awarded'?'Awarded':ev.event_type);
      var change=det.previous_status?(esc(det.previous_status)+' → awarded'):'';
      body+='<li><span class="tdot"></span><div class="tt">'+esc(label)+'</div>'+
        (change?('<div class="td">'+change+'</div>'):'')+
        (det.award_value_fmt?('<div class="td">Value: ₹'+esc(det.award_value_fmt)+'</div>'):'')+
        '<div class="td">'+esc(fmtIso(ev.event_at))+'</div></li>';
    });
    body+='</ul>';
  } else if(t){
    body+='<div class="section-title">History</div><p style="color:var(--muted);font-size:12.5px">'+
      'Tracking began '+esc(t.first_seen||'when first observed')+'. No further recorded status changes.</p>';
  }
  /* raw fields grouped */
  if(d.details && Object.keys(d.details).length){
    SECTIONS.forEach(function(sec){
      var rows=sec.fields.filter(function(f){return d.details[f[0]];});
      if(!rows.length) return;
      body+='<div class="section-title">'+esc(sec.name)+'</div><div class="kv">'+
        rows.map(function(f){return '<div class="k">'+esc(f[1])+'</div><div>'+esc(d.details[f[0]])+'</div>';}).join('')+
        '</div>';
    });
  }
  /* actions */
  body+='<div class="section-title">Documents & links</div><div class="so-actions">'+
    '<a class="btn" target="_blank" rel="noopener" href="/pdf/'+encodeURIComponent(id)+'/en">Work details (EN) PDF</a>'+
    '<a class="btn" target="_blank" rel="noopener" href="/pdf/'+encodeURIComponent(id)+'/mr">कामाचा तपशील (MR) PDF</a>'+
    '</div>';
  $('#soBody').innerHTML=body;
  wireStars($('#slideover'));
  $all('#slideover a[data-contractor]').forEach(function(a){a.addEventListener('click',function(e){
    e.preventDefault();closeOverlay();openContractor(a.dataset.contractor);});});
}
function fmtAmt(v){ /* client-side format of an exact decimal string */
  if(v==null||v==='') return '';
  var n=Number(v); if(isNaN(n)) return '';
  if(n>=1e7) return (n/1e7).toFixed(2).replace(/\.?0+$/,'')+' Cr';
  if(n>=1e5) return (n/1e5).toFixed(2).replace(/\.?0+$/,'')+' L';
  return n.toLocaleString('en-IN');
}
function cKey(name){ /* mirror server contractor_key loosely for linking */
  return (name||'').toLowerCase().replace(/^(m\/s\.?|messrs\.?|shri\.?|smt\.?|mr\.?|mrs\.?)\s+/,'')
    .replace(/[.,&'"()\/\\-]/g,' ').replace(/\b(pvt|private|ltd|limited|co|company|corporation|corp)\b/g,' ')
    .replace(/\s+/g,' ').trim();
}

/* ---------- overlay plumbing ---------- */
function openOverlay(sel){$('#overlay').classList.add('open');$(sel).classList.add('open');
  document.body.style.overflow='hidden';}
function closeOverlay(){$('#overlay').classList.remove('open');
  $('#slideover').classList.remove('open');$('#drawer').classList.remove('open');
  document.body.style.overflow='';}
$('#overlay').addEventListener('click',closeOverlay);
$('#soClose').addEventListener('click',closeOverlay);
$('#drawerClose').addEventListener('click',closeOverlay);
document.addEventListener('keydown',function(e){if(e.key==='Escape') closeOverlay();});
$('#bellBtn').addEventListener('click',function(){openOverlay('#drawer');renderDrawer();});
$('#markAllRead').addEventListener('click',markAllRead);
$all('#inboxUnreadSeg button').forEach(function(b){b.addEventListener('click',function(){
  inbox.filter=b.dataset.f;renderDrawer();});});
$('#inboxKind').addEventListener('change',function(){inbox.kind=this.value;renderDrawer();});
$('#inboxWatched').addEventListener('change',function(){inbox.watched=this.checked;renderDrawer();});

/* freshness pill + status panel */
$('#freshPill').addEventListener('click',function(e){e.stopPropagation();
  var pnl=$('#statusPanel');pnl.classList.toggle('open');});
document.addEventListener('click',function(e){
  var pnl=$('#statusPanel');
  if(pnl.classList.contains('open') && !pnl.contains(e.target) && e.target.id!=='freshPill'){
    pnl.classList.remove('open');}
});

/* refresh: honest — reloads with ?refresh=1 which re-scrapes live tenders server-side */
$('#refreshBtn').addEventListener('click',function(){
  var b=this; b.setAttribute('disabled','');
  $('#refreshIco').textContent='⟳'; $('#refreshLbl').textContent='Refreshing…';
  var url=location.pathname+'?refresh=1'+location.hash;
  location.href=url;
});

/* ---------- shared wiring ---------- */
function wireOpens(root){
  $all('[data-open]',root||document).forEach(function(node){
    if(node.dataset._wired) return; node.dataset._wired='1';
    node.addEventListener('click',function(e){
      if(e.target.closest('.star')||e.target.closest('a[data-contractor]')) return;
      var id=node.dataset.open; if(id && /^\d/.test(id)) openDetail(id);
    });
  });
}
function wireGoto(root){$all('[data-goto]',root).forEach(function(b){b.addEventListener('click',function(){
  show(b.dataset.goto);});});}
function wireStars(root){
  $all('.star',root||document).forEach(function(btn){
    if(btn.dataset._wired) return; btn.dataset._wired='1';
    btn.addEventListener('click',function(e){
      e.stopPropagation();
      var kind=btn.dataset.star, id=btn.dataset.id;
      var map=kind==='t'?watchT:watchC;
      if(map[id]) delete map[id]; else map[id]=true;
      saveWatch();
      btn.classList.toggle('on',!!map[id]); btn.textContent=map[id]?'★':'☆';
      /* reflect on other copies */
      $all('.star[data-star="'+kind+'"][data-id="'+CSS.escape(id)+'"]').forEach(function(o){
        o.classList.toggle('on',!!map[id]); o.textContent=map[id]?'★':'☆';});
    });
  });
}

/* ---------- boot ---------- */
renderFresh(); renderStatusPanel(); renderNavCounts();
var start=(location.hash||'#overview').slice(1);
if(!$('#page-'+start)) start='overview';
show(start);
/* record visit AFTER computing "since last visit" on overview */
LS.set('last_visit', Date.now());
</script>
</body></html>"""
