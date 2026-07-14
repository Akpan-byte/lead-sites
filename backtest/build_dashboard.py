# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Builds the final backtest deliverables: (1) a single self-contained HTML
#     dashboard (equity + drawdown curves per strategy, all quant metrics,
#     IS/OOS + maker/instant separation, plain-English explanations) and
#     (2) dashboard_raw.json with every merged metric for raw inspection.
# WHY: User wants the full 113-strategy BTC-5m backtest visualized for easy
#      consumption while five_min_trend_breakthrough finishes; the file must
#      work offline (embedded Chart.js + embedded data).
"""
build_dashboard.py — merge backtest results into an HTML dashboard + raw JSON.

Inputs (under results/):
  {is,oos}_{maker,instant}/*.summary.json       — per-strategy backtest summaries
  {is,oos}_{maker,instant}/*.trades.jsonl.gz    — per-trade records (for curves)
  quant_{maker,instant}/*.quant.json            — full quant suite per strategy
  is_scale/*.summary.json                       — taker-fill scale variants

Outputs (under results/):
  backtest_dashboard.html  — self-contained dashboard
  dashboard_raw.json       — every merged metric, raw
"""
import gzip
import json
import math
import os
import glob
import datetime

R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DIRS = ["is_maker", "is_instant", "oos_maker", "oos_instant", "is_scale"]
MAX_CURVE_PTS = 250
START_CAPITAL = 200.0


def load_summaries(d: str) -> dict:
    out = {}
    for p in glob.glob(os.path.join(R, d, "*.summary.json")):
        try:
            j = json.load(open(p))
            out[j["strategy"]] = j
        except Exception:
            pass
    return out


def load_quant(d: str) -> dict:
    out = {}
    for p in glob.glob(os.path.join(R, d, "*.quant.json")):
        try:
            j = json.load(open(p))
            out[j["strategy"]] = j
        except Exception:
            pass
    return out


def equity_curve(d: str, name: str):
    """Cumulative-equity + drawdown curve from the trades file, downsampled.

    Each point is [trade_index, equity, drawdown_pct]. Equity starts at
    START_CAPITAL and compounds trade pnl in close-time order — matching the
    backtest's sequential-capital semantics (run_strategy processes markets
    chronologically with a single shared wallet).
    """
    p = os.path.join(R, d, f"{name}.trades.jsonl.gz")
    if not os.path.exists(p):
        return []
    trades = []
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                t = json.loads(line)
                trades.append((t.get("closed_at", 0), t.get("pnl", 0.0)))
    except Exception:
        return []
    trades.sort(key=lambda x: x[0])
    eq = START_CAPITAL
    peak = START_CAPITAL
    pts = []
    for i, (_, pnl) in enumerate(trades, 1):
        eq += pnl
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100 if peak > 0 else 0.0
        pts.append((i, round(eq, 4), round(dd, 4)))
    if len(pts) > MAX_CURVE_PTS:
        step = len(pts) / MAX_CURVE_PTS
        pts = [pts[int(i * step)] for i in range(MAX_CURVE_PTS - 1)] + [pts[-1]]
    return pts


def main():
    sums = {d: load_summaries(d) for d in DIRS}
    quants = {"maker": load_quant("quant_maker"), "instant": load_quant("quant_instant")}
    names = sorted(set().union(*[set(s) for s in sums.values()]))
    print(f"strategies: {len(names)}")

    strategies = {}
    for n in names:
        rec = {"strategy": n}
        for d in DIRS:
            rec[d] = sums[d].get(n)
        rec["family"] = next(
            (sums[d][n].get("family") for d in DIRS if sums[d].get(n)), "unknown"
        )
        rec["quant_maker"] = quants["maker"].get(n)
        rec["quant_instant"] = quants["instant"].get(n)
        rec["curves"] = {d: equity_curve(d, n) for d in DIRS if d != "is_scale"}
        strategies[n] = rec

    # ---- headline aggregates -------------------------------------------------
    def pnl(d, n):
        s = sums[d].get(n)
        return s.get("total_pnl") if s else None

    agg = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "n_strategies": len(names),
        "start_capital": START_CAPITAL,
        "risk_per_trade_pct": 1.0,
        "min_contracts": 5,
        "is_window": "2026-05-22 to 2026-06-30",
        "oos_window": "2026-07-01 to 2026-07-10 (untouched during build)",
        "n_is_maker_positive": sum(1 for n in names if (pnl("is_maker", n) or 0) > 0),
        "n_oos_maker_positive": sum(1 for n in names if (pnl("oos_maker", n) or 0) > 0),
        "n_is_maker_wiped": sum(
            1 for n in names
            if sums["is_maker"].get(n) and sums["is_maker"][n].get("equity", 1) < 1
        ),
        "n_oos_maker_wiped": sum(
            1 for n in names
            if sums["oos_maker"].get(n) and sums["oos_maker"][n].get("equity", 1) < 1
        ),
    }

    raw = {"meta": agg, "strategies": strategies}
    raw_path = os.path.join(R, "dashboard_raw.json")
    with open(raw_path, "w") as fh:
        json.dump(raw, fh, indent=1, default=str)
    print(f"wrote {raw_path} ({os.path.getsize(raw_path)/1e6:.1f} MB)")

    # ---- HTML ----------------------------------------------------------------
    chart_js = ""
    cdn = os.path.join(os.path.dirname(R), "vendor", "chart.umd.min.js")
    if os.path.exists(cdn):
        chart_js = open(cdn).read()
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(raw, default=str))
    html = html.replace("__CHARTJS__", chart_js)
    html_path = os.path.join(R, "backtest_dashboard.html")
    with open(html_path, "w") as fh:
        fh.write(html)
    print(f"wrote {html_path} ({os.path.getsize(html_path)/1e6:.1f} MB)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BTC-5m Polymarket Backtest — 113 Strategies</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#e6edf3;
          --dim:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff; --amber:#d29922; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--border);
           background:var(--bg); }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:var(--dim); font-size:13px; }
  .wrap { padding:20px 24px 60px; max-width:1500px; margin:0 auto; }
  .cards { display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px;
          padding:12px 16px; min-width:150px; flex:1; }
  .card .v { font-size:22px; font-weight:700; }
  .card .k { color:var(--dim); font-size:12px; margin-top:2px; }
  .howto { background:var(--panel); border:1px solid var(--border); border-radius:10px;
           padding:14px 18px; font-size:13.5px; line-height:1.55; margin:16px 0; }
  .howto b { color:var(--blue); }
  .controls { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; align-items:center; }
  input, select, button { background:var(--panel); color:var(--fg); border:1px solid var(--border);
           border-radius:8px; padding:8px 12px; font-size:13px; }
  button { cursor:pointer; }
  button.on { border-color:var(--blue); color:var(--blue); }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th, td { padding:7px 9px; text-align:right; border-bottom:1px solid var(--border); }
  th { position:sticky; top:0; background:var(--panel); cursor:pointer; color:var(--dim);
       font-weight:600; user-select:none; z-index:5; }
  td.name, th.name { text-align:left; }
  tr:hover td { background:#1c2128; }
  tr.sel td { background:#1f2a3a; }
  .pos { color:var(--green); } .neg { color:var(--red); } .dim { color:var(--dim); }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px;
          border:1px solid var(--border); color:var(--dim); }
  #detail { position:fixed; top:0; right:0; width:min(860px,96vw); height:100vh;
            background:var(--panel); border-left:1px solid var(--border);
            transform:translateX(105%); transition:transform .18s ease; z-index:20;
            overflow-y:auto; padding:18px 22px; }
  #detail.open { transform:none; }
  #detail h2 { margin:0 0 2px; font-size:17px; }
  .chartbox { background:var(--bg); border:1px solid var(--border); border-radius:10px;
              padding:10px; margin:12px 0; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:10px; }
  .mcard { background:var(--bg); border:1px solid var(--border); border-radius:10px; padding:10px 12px; }
  .mcard h4 { margin:0 0 6px; font-size:12.5px; color:var(--blue); text-transform:uppercase;
              letter-spacing:.04em; }
  .mcard table { font-size:12px; }
  .mcard td { padding:3px 4px; border:none; }
  .mcard td:last-child { font-variant-numeric:tabular-nums; }
  .close { float:right; cursor:pointer; color:var(--dim); font-size:20px; }
  .legend { font-size:12px; color:var(--dim); margin:4px 2px; }
  .swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 10px; }
  #famchart { max-height:340px; }
  .flag { font-size:11px; padding:1px 6px; border-radius:8px; margin-left:6px; }
  .flag.dead { background:#3d1215; color:var(--red); }
  .flag.hot { background:#0f2d1a; color:var(--green); }
</style>
</head>
<body>
<header>
  <h1>BTC 5-Minute Up/Down — Full Strategy Backtest</h1>
  <div class="sub" id="gensub"></div>
</header>
<div class="wrap">
  <div class="howto" id="howto"></div>
  <div class="cards" id="cards"></div>

  <div class="controls">
    <input id="q" placeholder="search strategy…" oninput="renderTable()">
    <select id="fam" onchange="renderTable()"></select>
    <button id="bOosPos" onclick="toggleFilter(this,'oosPos')">only OOS winners</button>
    <button id="bHideDead" onclick="toggleFilter(this,'hideDead')" class="on">hide wiped-out</button>
    <button id="bHideZero" onclick="toggleFilter(this,'hideZero')" class="on">hide 0-trade</button>
    <span class="dim" id="rowcount" style="font-size:12px"></span>
  </div>

  <div style="overflow-x:auto; max-height:62vh; overflow-y:auto;">
  <table id="tbl"><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table>
  </div>

  <h3 style="margin:26px 0 6px">Family scoreboard — average OOS PnL (realistic fills)</h3>
  <div class="legend">Bars: mean OOS-maker total PnL per family. Green = profitable on data it never saw.</div>
  <div class="chartbox" id="famchart"><canvas id="famCanvas"></canvas></div>
</div>

<div id="detail">
  <span class="close" onclick="closeDetail()">✕</span>
  <h2 id="dName"></h2>
  <div class="sub" id="dSub"></div>
  <div class="cards" id="dCards" style="margin-top:12px"></div>
  <div class="chartbox"><canvas id="eqCanvas"></canvas></div>
  <div class="legend">
    <span class="swatch" style="background:#58a6ff"></span>IS realistic (maker)
    <span class="swatch" style="background:#bc8cff"></span>IS optimistic (instant)
    <span class="swatch" style="background:#3fb950"></span>OOS realistic — data it never saw
  </div>
  <div class="chartbox"><canvas id="ddCanvas"></canvas></div>
  <h3 style="font-size:14px">Quant suite — realistic fills (maker)</h3>
  <div class="grid" id="dQuantM"></div>
  <h3 style="font-size:14px;margin-top:14px">Quant suite — optimistic fills (instant)</h3>
  <div class="grid" id="dQuantI"></div>
  <h3 style="font-size:14px;margin-top:14px">Raw summaries (all bounds)</h3>
  <div class="grid" id="dSums"></div>
</div>

<script>__CHARTJS__</script>
<script>
const DATA = __DATA__;
const M = DATA.meta;
const STRATS = Object.values(DATA.strategies);
const filters = { oosPos:false, hideDead:true, hideZero:true };
let sortKey = "oos_mk", sortDir = -1, eqChart=null, ddChart=null;

const COLS = [
  ["name","strategy",1], ["family","family",0],
  ["is_mk","IS PnL<br><span class='dim'>realistic</span>",-1],
  ["is_in","IS PnL<br><span class='dim'>optimistic</span>",-1],
  ["oos_mk","OOS PnL<br><span class='dim'>realistic ★</span>",-1],
  ["dsr","DSR",-1], ["psr","PSR",-1],
  ["maxdd","Max DD%",1], ["pruin","P(ruin)",1],
  ["wr","Win %",-1], ["pf","Profit<br>factor",-1],
  ["ntr","Trades<br>(IS mk)",-1],
];

function pnl(s,d){ const x=s[d]; return x?x.total_pnl:null; }
function rowVals(s){
  const qm=s.quant_maker||{}, core=qm.core||{}, risk=qm.risk||{}, dd=qm.drawdown||{}, mc=qm.monte_carlo_50k||{};
  const ismk=s.is_maker||{};
  return {
    name:s.strategy, family:s.family,
    is_mk:pnl(s,"is_maker"), is_in:pnl(s,"is_instant"), oos_mk:pnl(s,"oos_maker"),
    dsr:risk.dsr??null, psr:risk.psr??null,
    maxdd:dd.max_dd_pct??null, pruin:mc.p_ruin??null,
    wr:core.win_rate!=null?core.win_rate*100:null, pf:core.profit_factor??null,
    ntr:ismk.n_closed??null,
    wiped: ismk.equity!=null && ismk.equity<1,
  };
}

function fmt(v, kind){
  if(v==null||v===undefined||(typeof v==="number"&&!isFinite(v))) return "<span class='dim'>—</span>";
  if(kind==="money"){ const c=v>=0?"pos":"neg"; return `<span class="${c}">${v>=0?"+":""}$${v.toFixed(2)}</span>`; }
  if(kind==="pct"){ return `${v.toFixed(1)}%`; }
  if(kind==="num"){ return v>=100?Math.round(v).toLocaleString():v.toFixed(2); }
  if(kind==="ratio"){ return v>=999?"∞":v.toFixed(2); }
  return v;
}

function init(){
  document.getElementById("gensub").textContent =
    `Generated ${new Date(M.generated_utc).toLocaleString()} · ${M.n_strategies} strategies · ` +
    `IS ${M.is_window} · OOS ${M.oos_window} · $${M.start_capital} start · ${M.risk_per_trade_pct}% risk/trade · min ${M.min_contracts} contracts`;
  document.getElementById("howto").innerHTML = `
    <b>What am I looking at?</b> Every strategy was run over real tick-by-tick BTC 5-minute Polymarket data — no simulated fills.
    Each started with <b>$${M.start_capital}</b>, risking <b>${M.risk_per_trade_pct}%</b> per trade (min ${M.min_contracts} contracts).<br>
    <b>IS</b> = in-sample (${M.is_window}) — the data strategies were built on. <b>OOS</b> = out-of-sample (${M.oos_window}) — data they
    <i>never saw</i>; this is the column that matters. ★ sorts by it.<br>
    <b>Realistic (maker)</b> fills assume we only get filled when the market actually trades at our price. <b>Optimistic (instant)</b>
    fills assume perfect execution — the truth for live trading sits between them, usually near realistic.<br>
    <b>DSR</b> (Deflated Sharpe Ratio) = probability the edge is real after accounting for how many strategies we tried.
    <b>Above 0.95 is the institutional bar.</b> <b>PSR</b> = probability the strategy's Sharpe beats 0.
    <b>P(ruin)</b> = chance (50,000 Monte Carlo runs) of blowing the account. <b>Wiped out</b> = $200 → ~$0.`;
  const cards = [
    [M.n_strategies, "strategies tested"],
    [M.n_is_maker_positive, "profitable IS (realistic)"],
    [M.n_oos_maker_positive, "profitable OOS ★"],
    [M.n_is_maker_wiped, "wiped out IS"],
    [M.n_oos_maker_wiped, "wiped out OOS"],
  ];
  document.getElementById("cards").innerHTML = cards.map(([v,k])=>
    `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  const fams = [...new Set(STRATS.map(s=>s.family))].sort();
  document.getElementById("fam").innerHTML = `<option value="">all families</option>` +
    fams.map(f=>`<option value="${f}">${f}</option>`).join("");
  const th = document.getElementById("thead");
  th.innerHTML = COLS.map(([k,label],i)=>
    `<th class="${k==="name"?"name":""}" onclick="sortBy('${k}')">${label}</th>`).join("");
  renderTable();
  renderFamChart();
}

function toggleFilter(btn, key){ filters[key]=!filters[key]; btn.classList.toggle("on",filters[key]); renderTable(); }
function sortBy(k){ if(sortKey===k) sortDir*=-1; else { sortKey=k; sortDir=-1; } renderTable(); }

function renderTable(){
  const q = document.getElementById("q").value.toLowerCase();
  const fam = document.getElementById("fam").value;
  let rows = STRATS.map(rowVals).filter(r=>{
    if(q && !r.name.toLowerCase().includes(q)) return false;
    if(fam && r.family!==fam) return false;
    if(filters.oosPos && !(r.oos_mk>0)) return false;
    if(filters.hideDead && r.wiped) return false;
    if(filters.hideZero && !r.ntr) return false;
    return true;
  });
  rows.sort((a,b)=>{
    let x=a[sortKey], y=b[sortKey];
    if(x==null) x=-1e18; if(y==null) y=-1e18;
    if(typeof x==="string") return sortDir*x.localeCompare(y);
    return sortDir*(x-y);
  });
  document.getElementById("rowcount").textContent = `${rows.length} shown`;
  document.getElementById("tbody").innerHTML = rows.map(r=>`
    <tr onclick="openDetail('${r.name.replace(/'/g,"")}')">
      <td class="name">${r.name.replace("phase_2.","")}
        ${r.wiped?'<span class="flag dead">wiped</span>':''}
        ${(r.oos_mk||0)>0?'<span class="flag hot">OOS+</span>':''}</td>
      <td><span class="pill">${r.family}</span></td>
      <td>${fmt(r.is_mk,"money")}</td><td>${fmt(r.is_in,"money")}</td>
      <td><b>${fmt(r.oos_mk,"money")}</b></td>
      <td>${fmt(r.dsr,"num")}</td><td>${fmt(r.psr,"num")}</td>
      <td class="neg">${r.maxdd!=null?fmt(r.maxdd,"pct"):"<span class='dim'>—</span>"}</td>
      <td>${r.pruin!=null?fmt(r.pruin*100,"pct"):"<span class='dim'>—</span>"}</td>
      <td>${r.wr!=null?fmt(r.wr,"pct"):"<span class='dim'>—</span>"}</td>
      <td>${fmt(r.pf,"ratio")}</td><td>${fmt(r.ntr,"num")}</td>
    </tr>`).join("");
}

function renderFamChart(){
  const byFam = {};
  STRATS.forEach(s=>{ const v=pnl(s,"oos_maker"); if(v==null) return;
    (byFam[s.family]=byFam[s.family]||[]).push(v); });
  const fams = Object.keys(byFam).map(f=>({f, avg:byFam[f].reduce((a,b)=>a+b,0)/byFam[f].length}))
    .sort((a,b)=>b.avg-a.avg);
  new Chart(document.getElementById("famCanvas"), {
    type:"bar",
    data:{ labels:fams.map(x=>x.f),
      datasets:[{ data:fams.map(x=>x.avg),
        backgroundColor:fams.map(x=>x.avg>=0?"#3fb950":"#f85149") }]},
    options:{ plugins:{legend:{display:false}}, responsive:true, maintainAspectRatio:false,
      scales:{ x:{ticks:{color:"#8b949e"},grid:{color:"#21262d"}},
               y:{ticks:{color:"#8b949e"},grid:{color:"#21262d"}} } }
  });
}

function curveDataset(label, pts, color, idx=1){
  return { label, data:pts.map(p=>({x:p[0],y:p[idx]})), borderColor:color,
           borderWidth:1.6, pointRadius:0, tension:0 };
}
function openDetail(name){
  const s = DATA.strategies[name]; if(!s) return;
  document.getElementById("dName").textContent = name;
  const mk = s.is_maker||{}, om = s.oos_maker||{};
  document.getElementById("dSub").innerHTML =
    `family <b>${s.family}</b> · IS ${mk.n_markets??"—"} markets / ${mk.n_closed??"—"} trades · ` +
    `OOS ${om.n_markets??"—"} markets / ${om.n_closed??"—"} trades`;
  const qm = s.quant_maker||{}, core=qm.core||{}, risk=qm.risk||{};
  const dc = [
    [fmt(pnl(s,"is_maker"),"money").replace(/<[^>]+>/g,""), "IS PnL realistic"],
    [fmt(pnl(s,"is_instant"),"money").replace(/<[^>]+>/g,""), "IS PnL optimistic"],
    [fmt(pnl(s,"oos_maker"),"money").replace(/<[^>]+>/g,""), "OOS PnL ★"],
    [risk.dsr!=null?risk.dsr.toFixed(3):"—", "DSR"],
    [risk.psr!=null?risk.psr.toFixed(3):"—", "PSR"],
  ];
  document.getElementById("dCards").innerHTML = dc.map(([v,k])=>
    `<div class="card"><div class="v" style="font-size:17px">${v}</div><div class="k">${k}</div></div>`).join("");
  const cv = s.curves||{};
  if(eqChart) eqChart.destroy();
  eqChart = new Chart(document.getElementById("eqCanvas"), {
    type:"line",
    data:{ datasets:[
      curveDataset("IS maker", cv.is_maker||[], "#58a6ff", 1),
      curveDataset("IS instant", cv.is_instant||[], "#bc8cff", 1),
      curveDataset("OOS maker", cv.oos_maker||[], "#3fb950", 1),
    ]},
    options:chartOpts("Equity curve ($200 start) — trade # vs equity $") });
  if(ddChart) ddChart.destroy();
  ddChart = new Chart(document.getElementById("ddCanvas"), {
    type:"line",
    data:{ datasets:[
      curveDataset("IS maker DD%", cv.is_maker||[], "#58a6ff", 2),
      curveDataset("OOS maker DD%", cv.oos_maker||[], "#f85149", 2),
    ]},
    options:chartOpts("Drawdown % (0 = at equity high, lower = deeper hole)") });
  document.getElementById("dQuantM").innerHTML = quantCards(s.quant_maker);
  document.getElementById("dQuantI").innerHTML = quantCards(s.quant_instant);
  document.getElementById("dSums").innerHTML = ["is_maker","is_instant","oos_maker","oos_instant","is_scale"]
    .map(d=>sumCard(d, s[d])).join("");
  document.getElementById("detail").classList.add("open");
}
function chartOpts(title){
  return { responsive:true, plugins:{ legend:{labels:{color:"#8b949e"}}, title:{display:true,text:title,color:"#e6edf3"} },
    scales:{ x:{type:"linear",ticks:{color:"#8b949e"},grid:{color:"#21262d"}},
             y:{ticks:{color:"#8b949e"},grid:{color:"#21262d"}} } };
}
function closeDetail(){ document.getElementById("detail").classList.remove("open"); }
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeDetail(); });

function kvTable(obj, skip){
  if(!obj || typeof obj!=="object") return "<span class='dim'>no data</span>";
  const rows = Object.entries(obj).filter(([k])=>!(skip||[]).includes(k)).map(([k,v])=>{
    let val;
    if(v==null) val="—";
    else if(typeof v==="number") val = isFinite(v)? (Math.abs(v)>=1000?Math.round(v).toLocaleString():v.toFixed(4)) : "∞";
    else if(typeof v==="object") val = `<pre style="margin:0;font-size:10.5px;color:#8b949e">${JSON.stringify(v,null,1)}</pre>`;
    else val = String(v);
    return `<tr><td class="dim">${k}</td><td>${val}</td></tr>`;
  }).join("");
  return `<table>${rows}</table>`;
}
const CARD_TITLES = { core:"Core performance", risk:"Risk-adjusted (PSR/DSR)", drawdown:"Drawdown",
  bootstrap_50k:"Bootstrap 50k — PnL confidence", monte_carlo_50k:"Monte Carlo 50k — ruin & DD",
  brownian:"Brownian projection", markov:"Markov win/loss chains", bayesian:"Bayesian win-rate",
  streaks:"Streaks", timing:"Timing & frequency", regressions:"Equity-curve regressions" };
function quantCards(q){
  if(!q) return "<div class='mcard'><span class='dim'>quant pending (strategy still running)</span></div>";
  return Object.entries(CARD_TITLES).map(([k,t])=>
    `<div class="mcard"><h4>${t}</h4>${kvTable(q[k], ["strategy","note"])}</div>`).join("");
}
function sumCard(d, s){
  return `<div class="mcard"><h4>${d}</h4>${kvTable(s, ["strategy","family","market"])}</div>`;
}
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
