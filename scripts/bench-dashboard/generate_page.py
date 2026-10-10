#!/usr/bin/env python3
"""Render results/*.json into a single standalone index.html served by Caddy at
bench.myvector.online. Data is rendered as STATIC HTML (always visible, no JS
required); Chart.js (cdnjs) adds charts as a guarded progressive enhancement.

Usage: generate_page.py <results_dir> <out_html>
"""
import glob
import html
import json
import os
import sys

ORDER = ["8.4", "9.7", "26.7", "MariaDB"]
COL = {"8.4": "#4f9dff", "9.7": "#36c98d", "26.7": "#f0a93b", "MariaDB": "#a56bd8"}


def e(s):
    """HTML-escape a value for safe insertion into static markup."""
    return html.escape(str(s), quote=True)


def disp(d):
    """Display label: explicit 'label' field, else 'MySQL <version>'."""
    return d.get("label") or ("MySQL " + str(d.get("version", "")))


def fmt(x, dp=0):
    if x is None:
        return "—"
    return f"{x:,.{dp}f}"


def load(results_dir):
    rows = {}
    for p in glob.glob(os.path.join(results_dir, "*.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if "version" in d and "metrics" in d:
            d["_srcfile"] = os.path.basename(p)
            rows[str(d["version"])] = d
    return [rows[v] for v in ORDER if v in rows]


def main():
    results_dir, out_html = sys.argv[1], sys.argv[2]
    data = load(results_dir)
    if not data:
        print("no result JSONs found", file=sys.stderr)
        sys.exit(1)
    generated = max((d.get("timestamp", "") for d in data), default="")[:19].replace("T", " ") + " UTC"
    # Escape "<" so a field value can't terminate the inline <script> payload.
    payload = json.dumps(data).replace("<", "\\u003c")
    downloads = " · ".join(
        f'<a href="data/{e(d["_srcfile"])}">{e(disp(d))} JSON</a>' for d in data)

    # ---- static (no-JS) content ----
    legend = "".join(
        f'<span class="lg"><span class="dot" style="background:{COL.get(d["version"],"#888")}"></span>'
        f'{e(disp(d))} <span class="mut">· {e(d.get("arch",""))} · {e(d.get("host_label",""))}</span></span>'
        for d in data)

    cards = "".join(
        f'<div class="card"><h3 style="color:{COL.get(d["version"],"#888")}">{e(disp(d))}</h3>'
        f'<div class="row"><span>Index build</span><b>{fmt(d["metrics"]["index_build_time_s"],1)}s</b></div>'
        f'<div class="row"><span>Insert QPS</span><b>{fmt(d["metrics"]["insert_qps"])}</b></div>'
        f'<div class="row"><span>ANN QPS</span><b>{fmt(d["metrics"]["knn_ann_qps"])}</b></div>'
        f'<div class="row"><span>Recall@10</span><b>{fmt(d["metrics"]["recall_at_10"],3)}</b></div></div>'
        for d in data)

    rowsdef = [
        ("Index build (s)", "index_build_time_s", 1),
        ("Insert QPS", "insert_qps", 0),
        ("Exact KNN QPS", "knn_qps", 0),
        ("KNN p50 (ms)", "knn_p50_ms", 1),
        ("KNN p99 (ms)", "knn_p99_ms", 1),
        ("ANN QPS", "knn_ann_qps", 0),
        ("ANN p50 (ms)", "knn_ann_p50_ms", 1),
        ("ANN p99 (ms)", "knn_ann_p99_ms", 1),
        ("Recall@10", "recall_at_10", 3),
    ]
    thead = "".join(f'<th style="color:{COL.get(d["version"],"#888")}">{e(disp(d))}</th>' for d in data)
    tbody = ""
    for label, key, dp in rowsdef:
        cells = "".join(f"<td>{fmt(d['metrics'].get(key), dp)}</td>" for d in data)
        tbody += f"<tr><td>{label}</td>{cells}</tr>"

    w = data[0].get("workload", {})
    footer = (f'Workload: {fmt(data[0].get("rows_indexed", w.get("rows")))} vectors · dim {w.get("dim")} · '
              f'{w.get("distance")} · HNSW M={w.get("M")}, ef_construction={w.get("ef_construction")} · '
              f'{w.get("holdout_queries")} held-out queries. Identical synthetic data on every instance. '
              f'8.4 on Ampere A2; 9.7, 26.7 &amp; MariaDB on Ampere A1 (pinned 4 vCPU / 24 GB each). '
              f'MariaDB uses its <b>native</b> VECTOR/HNSW index (built online during insert, so its '
              f'build time = load time, with no separate build phase or ef_construction) — a cross-engine '
              f'comparison against MyVector-on-MySQL, not a different MyVector build.')

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MyVector Benchmark</title>
<style>
:root{{ --bg:#f6f8fa; --card:#fff; --fg:#1b2733; --muted:#5a6b7b; --line:#e3e8ee; }}
@media (prefers-color-scheme: dark){{ :root{{ --bg:#0e1621; --card:#17212e; --fg:#e8eef4; --muted:#93a3b4; --line:#243243; }} }}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px 16px 64px}}
h1{{font-size:26px;margin:0 0 4px}} .sub{{color:var(--muted);margin:0 0 10px}}
.desc{{max-width:760px;margin:0 0 18px;color:var(--fg)}}
code{{background:var(--line);padding:1px 5px;border-radius:5px;font-size:.9em}}
.mut{{color:var(--muted);font-weight:400}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 22px}}
.lg{{display:flex;align-items:center;gap:7px;font-weight:600}}
.dot{{width:12px;height:12px;border-radius:3px;display:inline-block}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.card h3{{margin:0 0 8px;font-size:12px;letter-spacing:.04em;text-transform:uppercase}}
.card .row{{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;padding:2px 0}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:18px}}
.panel h2{{font-size:15px;margin:0 0 10px}} .note{{color:var(--muted);font-size:12.5px;margin:0 0 10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}}
canvas{{max-height:260px}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13.5px}}
th,td{{text-align:right;padding:7px 8px;border-bottom:1px solid var(--line)}}
th:first-child,td:first-child{{text-align:left}}
footer{{color:var(--muted);font-size:12.5px;margin-top:10px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>MyVector — cross-version benchmark</h1>
  <p class="sub">MySQL 8.4 vs 9.7 vs 26.7 · HNSW vector search · generated {generated}</p>
  <p class="desc">A comparison of MyVector's native HNSW vector search across three MySQL versions
  (plus MariaDB's native vector as a cross-engine reference) — same data and index parameters, each
  pinned to an identical 4 vCPU / 24 GB slice (hardware noted below). It measures index build time,
  insert throughput, exact KNN, approximate (ANN) QPS and latency, recall@10, and the
  accuracy/throughput tradeoff as <code>ef_search</code> varies.</p>
  <div class="legend">{legend}</div>
  <div class="cards">{cards}</div>

  <div class="panel" id="comparePanel" style="display:none">
    <h2>Head-to-head (normalized — taller is better on every metric)</h2>
    <div class="note">each metric scaled to the best performer = 100%; build time &amp; ANN latency inverted so taller always means better</div>
    <canvas id="c_compare" style="max-height:320px"></canvas>
  </div>

  <div id="charts" class="grid"></div>

  <div class="panel">
    <h2>Full metrics</h2>
    <table><thead><tr><th>Metric</th>{thead}</tr></thead><tbody>{tbody}</tbody></table>
  </div>

  <div class="panel" id="sweepPanel" style="display:none">
    <h2>Accuracy / throughput tradeoff (ef_search sweep)</h2>
    <div class="note">each point is an ef_search value; up-and-right is better</div>
    <canvas id="c_sweep" style="max-height:340px"></canvas>
  </div>

  <p class="sub" style="margin-top:14px">Raw results: {downloads}</p>
  <footer>{footer}</footer>
</div>

<script src="chart.umd.min.js"></script>
<script>
(function(){{
  if (typeof Chart === 'undefined') return;   // charts are enhancement only
  var DATA = {payload};
  var COL = {{"8.4":"#4f9dff","9.7":"#36c98d","26.7":"#f0a93b","MariaDB":"#a56bd8"}};
  function disp(d){{return d.label||("MySQL "+d.version);}}
  var css = getComputedStyle(document.documentElement);
  Chart.defaults.color = css.getPropertyValue('--muted').trim();
  Chart.defaults.borderColor = css.getPropertyValue('--line').trim();
  var labels = DATA.map(function(d){{return disp(d);}});
  var bg = DATA.map(function(d){{return COL[d.version]||"#888";}});
  var m = DATA.map(function(d){{return d.metrics;}});
  function f(x,dp){{return x==null?"—":Number(x).toLocaleString(undefined,{{maximumFractionDigits:dp||0}});}}
  // ---- consolidated head-to-head comparison (normalized, taller = better) ----
  (function(){{
    var metrics = [
      {{name:'Insert QPS', get:function(x){{return x.insert_qps;}}, better:'high'}},
      {{name:'ANN QPS', get:function(x){{return x.knn_ann_qps;}}, better:'high'}},
      {{name:'Recall@10', get:function(x){{return x.recall_at_10;}}, better:'high'}},
      {{name:'Build speed', get:function(x){{return x.index_build_time_s;}}, better:'low'}},
      {{name:'ANN latency', get:function(x){{return x.knn_ann_p99_ms;}}, better:'low'}}
    ];
    var norm = metrics.map(function(mt){{
      var vals = DATA.map(function(d){{return mt.get(d.metrics);}}).filter(function(v){{return v!=null;}});
      if(!vals.length) return null;
      var mx = Math.max.apply(null,vals), mn = Math.min.apply(null,vals);
      return DATA.map(function(d){{
        var v = mt.get(d.metrics); if(v==null) return 0;
        return mt.better==='high' ? (mx?100*v/mx:0) : (v?100*mn/v:0);
      }});
    }});
    var datasets = DATA.map(function(d,i){{
      return {{label:disp(d), backgroundColor:COL[d.version]||"#888", borderRadius:5,
        data:metrics.map(function(_,j){{return norm[j]?Math.round(norm[j][i]):0;}})}};
    }});
    document.getElementById('comparePanel').style.display='';
    new Chart(document.getElementById('c_compare'),{{type:'bar',
      data:{{labels:metrics.map(function(m){{return m.name;}}),datasets:datasets}},
      options:{{plugins:{{tooltip:{{callbacks:{{label:function(c){{return c.dataset.label+': '+c.parsed.y+'% of best';}}}}}}}},
        scales:{{y:{{beginAtZero:true,max:100,ticks:{{callback:function(v){{return v+'%';}}}}}}}}}}}});
  }})();

  function bar(title,note,vals,dp){{
    var wrap=document.createElement('div'); wrap.className='panel';
    var cid='ch'+Math.random().toString(36).slice(2);
    wrap.innerHTML='<h2>'+title+'</h2>'+(note?'<div class="note">'+note+'</div>':'')+'<canvas id="'+cid+'"></canvas>';
    document.getElementById('charts').appendChild(wrap);
    new Chart(document.getElementById(cid),{{type:'bar',
      data:{{labels:labels,datasets:[{{data:vals,backgroundColor:bg,borderRadius:6}}]}},
      options:{{plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:function(c){{return f(c.parsed.y,dp);}}}}}}}},
        scales:{{y:{{beginAtZero:true}}}}}}}});
  }}
  bar('Index build time','lower is better — seconds',m.map(function(x){{return x.index_build_time_s;}}),1);
  bar('Insert throughput','higher is better — rows/sec (online index)',m.map(function(x){{return x.insert_qps;}}),0);
  bar('ANN QPS','higher is better — HNSW approximate search',m.map(function(x){{return x.knn_ann_qps;}}),0);
  bar('ANN p99 latency','lower is better — ms',m.map(function(x){{return x.knn_ann_p99_ms;}}),1);
  bar('Recall@10','higher is better',m.map(function(x){{return x.recall_at_10;}}),3);
  bar('Exact KNN QPS','brute-force baseline',m.map(function(x){{return x.knn_qps;}}),0);

  var hasSweep = DATA.some(function(d){{return (d.metrics.ef_search_sweep||[]).length;}});
  if(hasSweep){{
    document.getElementById('sweepPanel').style.display='';
    var ds = DATA.filter(function(d){{return (d.metrics.ef_search_sweep||[]).length;}}).map(function(d){{
      return {{label:disp(d),borderColor:COL[d.version],backgroundColor:COL[d.version],
        showLine:true,tension:.25,pointRadius:4,
        data:d.metrics.ef_search_sweep.map(function(p){{return {{x:p.qps,y:p.recall_at_10,ef:p.ef_search}};}})}};
    }});
    new Chart(document.getElementById('c_sweep'),{{type:'scatter',data:{{datasets:ds}},
      options:{{plugins:{{tooltip:{{callbacks:{{label:function(c){{return 'ef='+c.raw.ef+': recall '+f(c.raw.y,3)+' @ '+f(c.raw.x)+' QPS';}}}}}}}},
        scales:{{x:{{title:{{display:true,text:'QPS (higher = faster)'}},beginAtZero:true}},
                y:{{title:{{display:true,text:'recall@10 (higher = accurate)'}},suggestedMax:1}}}}}}}});
  }}
}})();
</script>
</body>
</html>
"""
    with open(out_html, "w") as f:
        f.write(html)
    print(f"wrote {out_html} ({len(data)} versions: {', '.join(d['version'] for d in data)}, {len(html)} bytes)")


if __name__ == "__main__":
    main()
