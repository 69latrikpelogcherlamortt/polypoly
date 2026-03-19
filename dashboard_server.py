"""
dashboard_server.py  ·  PAF-001 Live Dashboard Server
───────────────────────────────────────────────────────
Sert le dashboard HTML avec données live injectées côté serveur au premier
chargement, puis un client JS poll /api/data toutes les 3 secondes pour
mettre à jour les KPIs, tableaux, logs et graphiques SANS rechargement de page.

Usage :
    # Terminal 1 — le bot
    python main.py

    # Terminal 2 — le dashboard (ouvre automatiquement le navigateur)
    python dashboard_server.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────
PORT           = 8765
DB_PATH        = Path("paf_trading.db")
SIGNAL_DB      = Path("paf_signals.db")
DASHBOARD_HTML = Path("dashboard/PAF_Dashboard_v5.html")
POLL_MS        = 3000   # intervalle de polling côté navigateur (ms)

# ── Sécurité dashboard ─────────────────────────────────────────────────────
# Token généré aléatoirement à chaque démarrage (ou via DASHBOARD_TOKEN env).
# Transmis automatiquement dans l'URL au premier lancement.
_DASHBOARD_TOKEN: str = os.environ.get("DASHBOARD_TOKEN") or secrets.token_urlsafe(16)

# Headers de sécurité HTTP ajoutés à toutes les réponses
_SECURITY_HEADERS = {
    "X-Content-Type-Options":    "nosniff",
    "X-Frame-Options":           "DENY",
    "X-XSS-Protection":          "1; mode=block",
    "Referrer-Policy":           "no-referrer",
    "Cache-Control":             "no-store, no-cache, must-revalidate",
    "Content-Security-Policy":   (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# LECTURE SQLite
# ═══════════════════════════════════════════════════════════════════════════

def _query(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# CONSTRUCTION DES DONNÉES LIVE
# ═══════════════════════════════════════════════════════════════════════════

def _build_live_data() -> dict:
    """Construit l'objet D à partir des DBs live."""

    # ── NAV history ────────────────────────────────────────────────────────
    nav_rows = _query(DB_PATH,
        "SELECT nav, daily_pnl, ts FROM nav_history ORDER BY ts ASC")
    nav_series = [round(r["nav"], 2) for r in nav_rows] or [100.0]
    bankroll   = nav_series[-1]

    # ── Brier rolling ──────────────────────────────────────────────────────
    brier_rows = _query(DB_PATH, """
        SELECT brier_contrib FROM trades
        WHERE status='closed' AND brier_contrib IS NOT NULL
        ORDER BY exit_ts DESC LIMIT 30
    """)
    brier_vals = [r["brier_contrib"] for r in reversed(brier_rows)]
    brier_rolling: list[float] = []
    for i in range(max(1, len(brier_vals) - 14), len(brier_vals) + 1):
        chunk = brier_vals[max(0, i - 15):i]
        brier_rolling.append(round(sum(chunk) / len(chunk), 4) if chunk else 0.20)
    if not brier_rolling:
        brier_rolling = [0.20]

    # ── Sharpe weekly ──────────────────────────────────────────────────────
    sharpe_rows = _query(DB_PATH, """
        SELECT daily_pnl, nav FROM nav_history ORDER BY ts DESC LIMIT 35
    """)
    sharpe_30d = 0.0
    if len(sharpe_rows) >= 5:
        rf = 0.049 / 365
        returns = [r["daily_pnl"] / max(r["nav"], 1.0) for r in sharpe_rows if r["nav"] > 0]
        if len(returns) >= 2:
            import statistics as _st
            excess = [x - rf for x in returns]
            mean_e = sum(excess) / len(excess)
            try:
                std_e = _st.stdev(excess)
                sharpe_30d = round(mean_e / std_e * (365 ** 0.5), 3) if std_e > 0 else 0.0
            except Exception:
                pass
    sharpe_weekly = [sharpe_30d] * min(15, max(1, len(nav_rows)))

    # ── Positions ouvertes ─────────────────────────────────────────────────
    pos_rows = _query(DB_PATH, "SELECT * FROM open_positions ORDER BY entry_ts")
    positions = []
    for p in pos_rows:
        cur = p.get("current_price") or p.get("p_market") or 0.0
        unr = round(p["n_shares"] * cur - p["cost_basis"], 2)
        positions.append({
            "id":    p["market_id"][:14],
            "q":     p["question"][:65],
            "s":     p["strategy"],
            "cat":   (p.get("category") or "other").lower(),
            "days":  round(float(p["days_to_res"])),
            "entry": round(float(p["p_market"]), 3),
            "cur":   round(float(cur), 3),
            "pm":    round(float(p["p_model"]), 3),
            "unr":   unr,
            "size":  round(float(p["cost_basis"]), 2),
            "sigma": round(float(p.get("sigma_14d") or 0.06), 3),
            "crucix": 1,
        })

    # ── Trades fermés ──────────────────────────────────────────────────────
    closed_rows = _query(DB_PATH, """
        SELECT question, strategy, fill_price, p_at_resolution, p_model,
               edge, size_eur, pnl, exit_reason, outcome, category
        FROM trades WHERE status='closed' AND pnl IS NOT NULL
        ORDER BY exit_ts DESC LIMIT 20
    """)
    closed_trades = []
    for t in closed_rows:
        pnl    = t["pnl"] or 0.0
        status = "ok" if (t["exit_reason"] or "").startswith("resolution") else "exit"
        result = "win" if pnl > 0 else "loss"
        pnl_s  = f"+€{pnl:.2f}" if pnl >= 0 else f"−€{abs(pnl):.2f}"
        edge_s = f"+{(t['edge'] or 0)*100:.1f}¢"
        closed_trades.append([
            (t["question"] or "")[:28],
            (t["strategy"] or "s1").lower(),
            round(float(t["fill_price"] or 0), 3),
            round(float(t["p_at_resolution"] or 0), 3),
            round(float(t["p_model"] or 0), 3),
            edge_s,
            f"€{t['size_eur']:.2f}",
            pnl_s,
            status,
            result,
        ])

    # ── Métriques globales ─────────────────────────────────────────────────
    pnl_rows = _query(DB_PATH,
        "SELECT pnl FROM trades WHERE status='closed' AND pnl IS NOT NULL")
    pnls   = [r["pnl"] for r in pnl_rows]
    gains  = sum(p for p in pnls if p > 0)
    losses = sum(abs(p) for p in pnls if p < 0)
    pf       = round(gains / losses, 3) if losses > 0 else 999.0
    win_rate = round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else 0.0

    # MDD
    mdd = 0.0
    if len(nav_series) >= 2:
        peak = nav_series[0]
        for nav in nav_series[1:]:
            if nav > peak:
                peak = nav
            dd = (peak - nav) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)
    mdd = round(mdd, 4)

    # VaR approx
    var_95 = 0.0
    if len(pnls) >= 5:
        try:
            import numpy as _np
            arr    = _np.array(pnls)
            var_95 = round(float(_np.percentile(arr, 5)), 2)
        except ImportError:
            sorted_pnls = sorted(pnls)
            idx = max(0, int(len(sorted_pnls) * 0.05) - 1)
            var_95 = round(sorted_pnls[idx], 2)

    # ── Distribution P&L ───────────────────────────────────────────────────
    buckets = [-4, -2, 0, 2, 4, 6, 8, float("inf")]
    pnl_dist = [0] * 8
    for p in pnls:
        for i, b in enumerate(buckets):
            if p < b:
                pnl_dist[i] += 1
                break
    if not any(pnl_dist):
        pnl_dist = [1, 1, 1, 1, 1, 1, 1, 1]

    # ── Signal log ─────────────────────────────────────────────────────────
    sig_rows = _query(SIGNAL_DB, """
        SELECT ts, source_id, direction, delta_p, market_id, raw_text
        FROM signal_log ORDER BY ts DESC LIMIT 8
    """)
    trade_rows_log = _query(DB_PATH, """
        SELECT entry_ts, question, strategy, fill_price, size_eur
        FROM trades ORDER BY entry_ts DESC LIMIT 5
    """)

    signals = []
    for t in trade_rows_log:
        ts = (t["entry_ts"] or "")[:16]
        signals.append({
            "t":   ts[11:16] or "--:--",
            "cls": "lb-fill",
            "l":   "FILL",
            "m":   f"{(t['question'] or '')[:42]} @ {t['fill_price']:.3f} · {t['strategy']} · {t['size_eur']:.2f}€",
        })
    for s in sig_rows:
        ts  = (s["ts"] or "")[:16]
        arr = "▲" if s["direction"] == "bullish" else "▼"
        signals.append({
            "t":   ts[11:16] or "--:--",
            "cls": "lb-crux",
            "l":   "CRUCIX",
            "m":   f"{s['source_id']} {arr} {(s['market_id'] or '')[:18]} Δp={s['delta_p']:+.3f}",
        })
    signals = signals[:10]

    # ── Reliability (calibration) ───────────────────────────────────────────
    cal_rows = _query(DB_PATH, """
        SELECT p_model, outcome FROM trades
        WHERE status='closed' AND outcome IS NOT NULL
        ORDER BY exit_ts DESC LIMIT 100
    """)
    if len(cal_rows) >= 10:
        buckets_cal = [i / 10 for i in range(1, 10)]
        rel_x, rel_y = [], []
        for b in buckets_cal:
            chunk = [r for r in cal_rows if abs(r["p_model"] - b) < 0.05]
            if chunk:
                rel_x.append(round(b, 1))
                rel_y.append(round(sum(r["outcome"] for r in chunk) / len(chunk), 2))
        reliability_x = rel_x or [.1,.2,.3,.4,.5,.6,.7,.8,.9]
        reliability_y = rel_y or [.1,.2,.3,.4,.5,.6,.7,.8,.9]
    else:
        reliability_x = [.1,.2,.3,.4,.5,.6,.7,.8,.9]
        reliability_y = [.1,.2,.3,.4,.5,.6,.7,.8,.9]

    # ── Win rate par catégorie ─────────────────────────────────────────────
    cat_rows = _query(DB_PATH, """
        SELECT category, pnl
        FROM trades
        WHERE status='closed' AND pnl IS NOT NULL
    """)
    cats: dict[str, list] = {}
    for r in cat_rows:
        c = (r.get("category") or "other").lower() or "other"
        cats.setdefault(c, []).append(r["pnl"] > 0)
    COL_MAP = {"macro": "var(--gold)", "crypto": "var(--blue)",
               "politics": "var(--up)", "sports": "var(--vio)", "other": "var(--cyan)"}
    win_by_cat = [
        {"c": c.capitalize(), "p": round(sum(v) / len(v) * 100), "col": COL_MAP.get(c, "var(--text)")}
        for c, v in cats.items() if v
    ] or [{"c": "Global", "p": round(win_rate * 100), "col": "var(--blue)"}]

    return {
        "bankroll":      bankroll,
        "nav_start":     100,
        "nav_series":    nav_series,
        "brier_rolling": brier_rolling,
        "sharpe_weekly": sharpe_weekly,
        "reliability_x": reliability_x,
        "reliability_y": reliability_y,
        "pnl_dist":      pnl_dist,
        "positions":     positions,
        "closed_trades": closed_trades,
        "signals":       signals,
        "win_by_cat":    win_by_cat,
        "markets":       [],
        # Métriques scalaires pour les getters JS
        "_mdd":  mdd,
        "_pf":   pf,
        "_var":  var_95,
    }


# ═══════════════════════════════════════════════════════════════════════════
# GETTERS JS (calculés côté client depuis D.*)
# ═══════════════════════════════════════════════════════════════════════════

_GETTERS_TEMPLATE = """\
  get n_open()       {{ return this.positions.length; }},
  get deployed()     {{ return this.positions.reduce((a,p)=>a+p.size,0); }},
  get deployed_pct() {{ return this.deployed/this.bankroll*100; }},
  get unreal_pnl()   {{ return this.positions.reduce((a,p)=>a+p.unr,0); }},
  get win_rate()     {{ const w=this.closed_trades.filter(t=>t[9]==='win').length; return this.closed_trades.length?w/this.closed_trades.length:0; }},
  get total_trades() {{ return this.closed_trades.length + this.positions.length; }},
  get avg_edge()     {{ return this.positions.length?this.positions.reduce((a,p)=>a+(p.pm-p.cur),0)/this.positions.length:0; }},
  get avg_z()        {{ return this.positions.length?this.positions.reduce((a,p)=>a+(p.pm-p.cur)/p.sigma,0)/this.positions.length:0; }},
  get brier_current(){{ return this.brier_rolling[this.brier_rolling.length-1]; }},
  get itd_pct()      {{ return (this.nav_series[this.nav_series.length-1]-this.nav_start)/this.nav_start*100; }},
  get daily_pnl()    {{ const s=this.nav_series; return s.length>=2?s[s.length-1]-s[s.length-2]:0; }},
  get mdd_30d()      {{ return {mdd}; }},
  get sharpe_30d()   {{ return {sharpe}; }},
  get profit_factor(){{ return {pf}; }},
  get var_95()       {{ return {var}; }},"""


# ═══════════════════════════════════════════════════════════════════════════
# SCRIPT DE POLLING CÔTÉ CLIENT (injecté avant </body>)
# ═══════════════════════════════════════════════════════════════════════════

_POLL_SCRIPT = r"""
<script>
/* ═══════════════════════════════════════════════════════════════════
   PAF-001  LIVE POLLING CLIENT
   Poll /api/data toutes les 3s · mise à jour DOM + Chart.js in-place
   Pas de rechargement de page.
═══════════════════════════════════════════════════════════════════ */
(function startLivePolling() {
  'use strict';
  const POLL_MS = """ + str(POLL_MS) + r""";

  // ── Banner ──────────────────────────────────────────────────────
  function updateBanner(ok) {
    const el = document.getElementById('__paf_live');
    if (!el) return;
    const ts = new Date().toUTCString().slice(17,25) + ' UTC';
    el.textContent = ok ? ('🟢 LIVE · ' + ts + ' · +' + (POLL_MS/1000).toFixed(0) + 's')
                        : ('🔴 ERR · '  + ts + ' · retry…');
    el.style.color = ok ? '#2a7fff' : '#e05252';
  }

  // ── Chart helper ────────────────────────────────────────────────
  function updateChart(id, data0, labels, data1) {
    const el = document.getElementById(id);
    if (!el) return;
    const ch = Chart.getChart(el);
    if (!ch) return;
    if (labels) ch.data.labels = labels;
    ch.data.datasets[0].data = data0;
    if (data1 !== undefined && ch.data.datasets[1]) ch.data.datasets[1].data = data1;
    ch.update('none');
  }

  // ── date labels ─────────────────────────────────────────────────
  function makeLabels(n) {
    return Array.from({length: n}, (_, i) => {
      const d = new Date();
      d.setDate(d.getDate() - n + 1 + i);
      return d.toLocaleDateString('en', {month:'short', day:'numeric'});
    });
  }

  // ── Build computed D from raw API response ───────────────────────
  function buildD(raw) {
    return Object.assign({}, raw, {
      get sharpe_30d()    { return raw.sharpe_weekly ? raw.sharpe_weekly[raw.sharpe_weekly.length-1] : 0; },
      get mdd_30d()       { return raw._mdd || 0; },
      get profit_factor() { return raw._pf  || 1; },
      get var_95()        { return Math.abs(raw._var || 0); },
      get n_open()        { return raw.positions.length; },
      get deployed()      { return raw.positions.reduce((a,p)=>a+p.size, 0); },
      get deployed_pct()  { return this.deployed / raw.bankroll * 100; },
      get unreal_pnl()    { return raw.positions.reduce((a,p)=>a+p.unr, 0); },
      get win_rate()      { const w=raw.closed_trades.filter(t=>t[9]==='win').length; return raw.closed_trades.length ? w/raw.closed_trades.length : 0; },
      get total_trades()  { return raw.closed_trades.length + raw.positions.length; },
      get avg_edge()      { return raw.positions.length ? raw.positions.reduce((a,p)=>a+(p.pm-p.cur),0)/raw.positions.length : 0; },
      get avg_z()         { return raw.positions.length ? raw.positions.reduce((a,p)=>a+(p.pm-p.cur)/p.sigma,0)/raw.positions.length : 0; },
      get brier_current() { return raw.brier_rolling[raw.brier_rolling.length-1]; },
      get daily_pnl()     { const s=raw.nav_series; return s.length>=2 ? s[s.length-1]-s[s.length-2] : 0; },
      get itd_pct()       { return (raw.nav_series[raw.nav_series.length-1]-raw.nav_start)/raw.nav_start*100; },
    });
  }

  // ── KPI row (Dashboard) ─────────────────────────────────────────
  function updateKpis(D) {
    const el = document.getElementById('kpiRow');
    if (!el) return;
    const nav_now = D.nav_series[D.nav_series.length-1];
    const dp = D.daily_pnl;
    const dpSign = dp >= 0 ? '+' : '';
    const fills = D.signals.filter(s=>s.cls==='lb-fill').length;
    const wins  = D.closed_trades.filter(t=>t[9]==='win').length;
    const loss  = D.closed_trades.filter(t=>t[9]==='loss').length;
    el.innerHTML =
      `<div class="kcard go"><div class="kl">NAV</div><div class="kv go">€${nav_now.toFixed(2)}</div><div class="ks up">+€${(nav_now-D.nav_start).toFixed(2)} · +${D.itd_pct.toFixed(1)}% ITD</div></div>` +
      `<div class="kcard ${dp>=0?'up':'dn'}"><div class="kl">Daily P&L</div><div class="kv ${dp>=0?'up':'dn'}">${dpSign}€${dp.toFixed(2)}</div><div class="ks muted">${fills} trades today</div></div>` +
      `<div class="kcard bl"><div class="kl">Sharpe 30d</div><div class="kv bl">${D.sharpe_30d.toFixed(2)}</div><div class="ks up">Target ≥2.0 ${D.sharpe_30d>=2?'✓':'⚠'}</div></div>` +
      `<div class="kcard"><div class="kl">Win Rate ${D.total_trades}t</div><div class="kv">${(D.win_rate*100).toFixed(1)}%</div><div class="ks muted">${wins}W · ${loss}L</div></div>` +
      `<div class="kcard"><div class="kl">Avg Edge</div><div class="kv">${(D.avg_edge*100).toFixed(1)}¢</div><div class="ks muted">δ avg ${D.avg_z.toFixed(2)}σ</div></div>`;
    const nb = document.getElementById('navBadge');
    if (nb) nb.textContent = `€${D.nav_start} → €${nav_now.toFixed(2)}`;
  }

  // ── Top badges ──────────────────────────────────────────────────
  function updateTopBadges(D) {
    const el = document.getElementById('topBadges');
    if (!el) return;
    const bc = D.brier_current, mdd = D.mdd_30d, sr = D.sharpe_30d;
    el.innerHTML =
      `<span class="bdg ${bc<.22?'up':'dn'}">Brier ${bc.toFixed(3)} ${bc<.22?'✓':'⚠'}</span>` +
      `<span class="bdg ${sr>=2?'up':'am'}">SR ${sr.toFixed(2)} ${sr>=2?'✓':'⚠'}</span>` +
      `<span class="bdg ${mdd<.08?'up':'am'}">MDD ${(mdd*100).toFixed(1)}% ${mdd<.08?'✓':'⚠'}</span>` +
      `<span class="bdg bl">7 Gates OK</span>` +
      `<span class="bdg vio">Crucix Active</span>`;
  }

  // ── Positions ───────────────────────────────────────────────────
  function posCard(p) {
    const edge = p.pm - p.cur, z = edge / p.sigma;
    const ep = Math.min(98, Math.max(5, edge / 0.15 * 100));
    const st = edge > 0.04 && z > 1.5 ? 'ok' : edge > 0 ? 'warn' : 'kill';
    const stLbl = st==='ok' ? 'Active' : st==='warn' ? 'Monitor' : 'Review';
    const unrSign = p.unr >= 0 ? '+' : '';
    return `<div class="pc"><div class="pt"><div><div class="pn">${p.q}</div><div class="pm"><span class="${p.s.toLowerCase()}">${p.s}</span><span style="font-size:9px;color:var(--text3)">${p.cat} · ${p.days}d</span></div></div><span class="pill ${st}">${stLbl}</span></div><div class="pnums"><div class="pi"><div class="pl">Entry</div><div class="pv">${p.entry.toFixed(3)}</div></div><div class="pi"><div class="pl">Current</div><div class="pv">${p.cur.toFixed(3)}</div></div><div class="pi"><div class="pl">P_model</div><div class="pv c-go">${p.pm.toFixed(3)}</div></div><div class="pi"><div class="pl">Unreal.</div><div class="pv ${p.unr>=0?'c-up':'c-dn'}">${unrSign}€${Math.abs(p.unr).toFixed(2)}</div></div></div><div><div class="elb">Edge ${(edge*100).toFixed(1)}¢ · δ=${z.toFixed(2)}σ · €${p.size}</div><div class="etr"><div class="efil" style="width:${ep}%;background:${st==='ok'?'var(--up)':st==='warn'?'var(--am)':'var(--dn)'}"></div></div></div></div>`;
  }

  function updatePositions(D) {
    const grid = document.getElementById('posGrid');
    if (grid) grid.innerHTML = D.positions.length
      ? D.positions.map(posCard).join('')
      : '<div style="color:var(--text3);padding:20px;text-align:center;font-size:11px">Aucune position ouverte</div>';

    const kpis = document.getElementById('posKpis');
    if (kpis && D.positions.length > 0) {
      const tu = D.unreal_pnl, aeSign = D.avg_edge >= 0 ? '+' : '';
      const daysArr = D.positions.map(p=>p.days);
      kpis.innerHTML =
        `<div class="kcard"><div class="kl">Open Positions</div><div class="kv">${D.n_open}</div><div class="ks muted">${D.positions.filter(p=>p.s==='S1').length} S1 · ${D.positions.filter(p=>p.s==='S2').length} S2</div></div>` +
        `<div class="kcard up"><div class="kl">Unrealized P&L</div><div class="kv up">${tu>=0?'+':''}€${tu.toFixed(2)}</div><div class="ks muted">Avg edge ${(D.avg_edge*100).toFixed(1)}¢</div></div>` +
        `<div class="kcard"><div class="kl">Capital At Risk</div><div class="kv">€${D.deployed.toFixed(2)}</div><div class="ks muted">${D.deployed_pct.toFixed(1)}% bankroll</div></div>` +
        `<div class="kcard"><div class="kl">Avg Days to Res.</div><div class="kv">${Math.round(D.positions.reduce((a,p)=>a+p.days,0)/D.positions.length)}d</div><div class="ks muted">Range ${Math.min(...daysArr)} — ${Math.max(...daysArr)}d</div></div>`;
    }

    const cb = document.getElementById('closedBadge');
    if (cb) cb.textContent = `${D.closed_trades.length} total`;
    const ct = document.getElementById('closedTbl');
    if (ct) ct.innerHTML = D.closed_trades.map(r =>
      `<tr><td class="tn c-mt">${r[0]}</td><td><span class="${r[1]}">${r[1].toUpperCase()}</span></td><td>${parseFloat(r[2]).toFixed(3)}</td><td>${parseFloat(r[3]).toFixed(3)}</td><td>${parseFloat(r[4]).toFixed(3)}</td><td>${r[5]}</td><td>${r[6]}</td><td class="${r[9]==='loss'?'c-dn':'c-up'}">${r[7]}</td><td><span class="pill ${r[8]}">${r[8]==='ok'?'YES':'NO'}</span></td></tr>`
    ).join('');
  }

  // ── Calibration ─────────────────────────────────────────────────
  function updateCalib(D) {
    const el = document.getElementById('calibKpis');
    if (el) el.innerHTML =
      `<div class="kcard go"><div class="kl">Brier (15t)</div><div class="kv go">${D.brier_current.toFixed(3)}</div><div class="ks up">Kill 0.22 ${D.brier_current<.22?'✓':'⚠'}</div></div>` +
      `<div class="kcard up"><div class="kl">Sharpe 30d</div><div class="kv up">${D.sharpe_30d.toFixed(2)}</div><div class="ks up">Target 2.0 ${D.sharpe_30d>=2?'✓':'⚠'}</div></div>` +
      `<div class="kcard"><div class="kl">Profit Factor</div><div class="kv up">${D.profit_factor.toFixed(2)}</div><div class="ks up">Target 1.5 ${D.profit_factor>=1.5?'✓':'⚠'}</div></div>` +
      `<div class="kcard"><div class="kl">Avg Z-score δ</div><div class="kv">${D.avg_z.toFixed(2)}σ</div><div class="ks up">Min 1.5σ ${D.avg_z>=1.5?'✓':'⚠'}</div></div>`;
    const db = document.getElementById('distBadge');
    if (db) db.textContent = `${D.total_trades} trades`;
  }

  // ── Risk ────────────────────────────────────────────────────────
  function updateRisk(D) {
    const el = document.getElementById('riskKpis');
    if (el) el.innerHTML =
      `<div class="kcard am"><div class="kl">MDD 30d</div><div class="kv am">${(D.mdd_30d*100).toFixed(1)}%</div><div class="ks am">Limit 8% · ${D.mdd_30d<.08?'OK':'WATCH'}</div></div>` +
      `<div class="kcard"><div class="kl">VaR 95% MC</div><div class="kv">€${D.var_95.toFixed(2)}</div><div class="ks up">${(D.var_95/D.bankroll*100).toFixed(1)}% bankroll ✓</div></div>` +
      `<div class="kcard"><div class="kl">Expected Shortfall</div><div class="kv">€${(D.var_95*1.38).toFixed(2)}</div><div class="ks muted">ES₉₅ beyond VaR</div></div>` +
      `<div class="kcard"><div class="kl">Consec. losses</div><div class="kv">—</div><div class="ks up">Limit 4</div></div>`;

    const ks = document.getElementById('ksRows');
    if (ks) {
      const bc = D.brier_current;
      const rows = [
        {l:'Bankroll',          bar:D.bankroll/120*100,         col:'var(--up)', v:'€'+D.bankroll.toFixed(2), st:'ok'},
        {l:'Brier score (15t)', bar:(1-bc/0.22)*100,            col:bc<.22?'var(--up)':'var(--dn)', v:bc.toFixed(3), st:bc<.22?'ok':'warn'},
        {l:'MDD 30d',           bar:D.mdd_30d/0.08*100,         col:D.mdd_30d<.08?'var(--am)':'var(--dn)', v:(D.mdd_30d*100).toFixed(1)+'%', st:D.mdd_30d<.08?'warn':'kill'},
        {l:'Sharpe 30d',        bar:Math.min(100,D.sharpe_30d/3*100), col:'var(--up)', v:D.sharpe_30d.toFixed(2), st:'ok'},
        {l:'Profit Factor',     bar:Math.min(100,D.profit_factor/2.5*100), col:'var(--up)', v:D.profit_factor.toFixed(2), st:'ok'},
        {l:'Exposure',          bar:D.deployed_pct/25*100,      col:'var(--up)', v:D.deployed_pct.toFixed(1)+'%', st:'ok'},
        {l:'VaR 95%',           bar:D.var_95/D.bankroll/0.05*100, col:'var(--up)', v:'€'+D.var_95.toFixed(2), st:'ok'},
      ];
      ks.innerHTML = rows.map(k =>
        `<div class="ks-r"><span class="ks-l">${k.l}</span><div class="ks-t"><div class="ks-f" style="width:${Math.min(100,Math.max(0,k.bar)).toFixed(0)}%;background:${k.col}"></div></div><span class="ks-v ${k.st==='ok'?'c-up':k.st==='warn'?'c-am':'c-dn'}">${k.v}</span><span class="ks-s"><span class="pill ${k.st==='ok'?'ok':k.st==='warn'?'warn':'kill'}">${k.st==='ok'?'OK':'Watch'}</span></span></div>`
      ).join('');
    }
  }

  // ── Signals ─────────────────────────────────────────────────────
  function updateSignals(D) {
    const el = document.getElementById('sigLog');
    if (el) el.innerHTML = D.signals.map(s =>
      `<div class="li"><span class="lt">${s.t}</span><span class="lb ${s.cls}">${s.l}</span><span class="lm">${s.m}</span></div>`
    ).join('');
    const sb = document.getElementById('sigBadge');
    if (sb) sb.textContent = `${D.signals.length} today`;
  }

  // ── Win rate bars ────────────────────────────────────────────────
  function updateWrBars(D) {
    const el = document.getElementById('wrBars');
    if (el) el.innerHTML = D.win_by_cat.map(w =>
      `<div class="wr"><span class="wrl">${w.c}</span><div class="wrt"><div class="wrf" style="width:${w.p}%;background:${w.col}"></div></div><span class="wrp c-up">${w.p}%</span></div>`
    ).join('');
  }

  // ── Charts ──────────────────────────────────────────────────────
  function updateCharts(D) {
    const labels = makeLabels(D.nav_series.length);
    const pnl    = D.nav_series.map((v,i) => i ? +(v - D.nav_series[i-1]).toFixed(2) : 0);

    // NAV chart
    updateChart('navC', D.nav_series, labels);

    // P&L bars (colors depend on sign)
    const pnlEl = document.getElementById('pnlC');
    if (pnlEl) {
      const ch = Chart.getChart(pnlEl);
      if (ch) {
        ch.data.labels = labels;
        ch.data.datasets[0].data = pnl;
        ch.data.datasets[0].backgroundColor = pnl.map(v => v>=0 ? 'rgba(0,200,150,.55)' : 'rgba(224,82,82,.50)');
        ch.update('none');
      }
    }

    // Brier mini + long
    const brLabels = D.brier_rolling.map((_,i) => 'T'+(i+1));
    updateChart('brC',    D.brier_rolling, brLabels, Array(D.brier_rolling.length).fill(.22));
    updateChart('brLongC',D.brier_rolling, brLabels, Array(D.brier_rolling.length).fill(.22));

    // Sharpe weekly
    updateChart('sharpeC', D.sharpe_weekly, D.sharpe_weekly.map((_,i)=>'W'+(i+1)),
                Array(D.sharpe_weekly.length).fill(2));

    // Reliability scatter
    const relEl = document.getElementById('relC');
    if (relEl) {
      const ch = Chart.getChart(relEl);
      if (ch) { ch.data.datasets[0].data = D.reliability_x.map((x,i)=>({x,y:D.reliability_y[i]})); ch.update('none'); }
    }
    const relLEl = document.getElementById('relLongC');
    if (relLEl) {
      const ch = Chart.getChart(relLEl);
      if (ch) { ch.data.datasets[0].data = D.reliability_x.map((x,i)=>({x,y:D.reliability_y[i]})); ch.update('none'); }
    }

    // P&L distribution
    const distEl = document.getElementById('distC');
    if (distEl) {
      const ch = Chart.getChart(distEl);
      if (ch) { ch.data.datasets[0].data = D.pnl_dist; ch.update('none'); }
    }
  }

  // ── Crucix KPIs ─────────────────────────────────────────────────
  function updateCrucix(D) {
    const el = document.getElementById('cxKpis');
    if (el) el.innerHTML =
      `<div class="kcard vio"><div class="kl">Active Sources</div><div class="kv c-vio">26</div><div class="ks muted">24 OK · 2 watch</div></div>` +
      `<div class="kcard"><div class="kl">Alerts Today</div><div class="kv">${D.signals.filter(s=>s.cls==='lb-crux').length}</div><div class="ks muted">signals entrants</div></div>` +
      `<div class="kcard up"><div class="kl">Signals → Trades</div><div class="kv up">${D.signals.filter(s=>s.cls==='lb-fill').length}</div><div class="ks muted">fills aujourd'hui</div></div>` +
      `<div class="kcard"><div class="kl">Agent Uptime</div><div class="kv">—</div><div class="ks up">Running ✓</div></div>`;
  }

  // ── MAIN POLL ────────────────────────────────────────────────────
  async function poll() {
    try {
      const resp = await fetch('/api/data', {cache: 'no-store'});
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const raw = await resp.json();
      const D   = buildD(raw);

      updateKpis(D);
      updateTopBadges(D);
      updatePositions(D);
      updateSignals(D);
      updateCalib(D);
      updateRisk(D);
      updateWrBars(D);
      updateCharts(D);
      updateCrucix(D);
      updateBanner(true);
    } catch (e) {
      updateBanner(false);
      console.warn('[PAF Live] poll error:', e);
    }
  }

  // Premier poll après 800ms (le temps que Chart.js initialise les canvases)
  setTimeout(poll, 800);
  setInterval(poll, POLL_MS);
})();
</script>
"""


# ═══════════════════════════════════════════════════════════════════════════
# INJECTION HTML (premier rendu côté serveur)
# ═══════════════════════════════════════════════════════════════════════════

def _inject_live_data(html: str, data: dict) -> str:
    """Remplace const D = {...} par les données live pour le premier rendu.
    Le client JS prend ensuite le relais toutes les {POLL_MS}ms."""

    mdd    = data.pop("_mdd", 0.0)
    pf     = data.pop("_pf",  1.0)
    var    = data.pop("_var", 0.0)
    sharpe = data["sharpe_weekly"][-1] if data["sharpe_weekly"] else 0.0

    getters = _GETTERS_TEMPLATE.format(
        mdd=mdd, sharpe=sharpe, pf=pf, var=abs(var)
    )

    live_js = (
        "const D = {\n"
        f"  bankroll:      {data['bankroll']},\n"
        f"  nav_start:     {data['nav_start']},\n"
        f"  nav_series:    {json.dumps(data['nav_series'])},\n"
        f"  brier_rolling: {json.dumps(data['brier_rolling'])},\n"
        f"  sharpe_weekly: {json.dumps(data['sharpe_weekly'])},\n"
        f"  reliability_x: {json.dumps(data['reliability_x'])},\n"
        f"  reliability_y: {json.dumps(data['reliability_y'])},\n"
        f"  pnl_dist:      {json.dumps(data['pnl_dist'])},\n"
        f"  positions:     {json.dumps(data['positions'], ensure_ascii=False)},\n"
        f"  closed_trades: {json.dumps(data['closed_trades'], ensure_ascii=False)},\n"
        f"  signals:       {json.dumps(data['signals'], ensure_ascii=False)},\n"
        f"  win_by_cat:    {json.dumps(data['win_by_cat'], ensure_ascii=False)},\n"
        f"  markets:       {json.dumps(data['markets'])},\n"
        f"{getters}\n"
        "};"
    )

    # Remplace le bloc "const D = { ... };"
    new_html = re.sub(r"const D = \{.*?\n\};", live_js, html, flags=re.DOTALL, count=1)

    # Bandeau "Live" avec id pour mise à jour dynamique (pas de meta refresh)
    ts_now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    live_banner = (
        f'<div id="__paf_live" style="position:fixed;top:0;right:0;z-index:9999;'
        f'background:#0a1422;border-left:1px solid rgba(42,127,255,.3);'
        f'border-bottom:1px solid rgba(42,127,255,.3);'
        f'padding:4px 10px;font-family:monospace;font-size:10px;color:#2a7fff;">'
        f'🟢 LIVE · {ts_now} · +{POLL_MS//1000}s</div>'
    )
    new_html = new_html.replace("<body", f"{live_banner}<body", 1)

    # Injecte le client de polling avant </body>
    new_html = new_html.replace("</body>", _POLL_SCRIPT + "\n</body>", 1)

    return new_html


# ═══════════════════════════════════════════════════════════════════════════
# SERVEUR HTTP
# ═══════════════════════════════════════════════════════════════════════════

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence les logs HTTP bruts

    def _send_security_headers(self):
        """Ajoute les headers de sécurité HTTP à toutes les réponses."""
        for k, v in _SECURITY_HEADERS.items():
            self.send_header(k, v)

    def _check_token(self) -> bool:
        """Vérifie le token dans query string (?t=...) ou header Authorization."""
        # Authorization: Bearer <token>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidate = auth[7:]
            if secrets.compare_digest(candidate, _DASHBOARD_TOKEN):
                return True
        # Query string : ?t=<token>
        if f"t={_DASHBOARD_TOKEN}" in self.path:
            return True
        return False

    def do_GET(self):
        # ── Health check (pas d'auth nécessaire) ─────────────────────────
        if self.path == "/ping":
            self.send_response(200)
            self._send_security_headers()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"pong")
            return

        # ── Vérification du token pour toutes les routes protégées ───────
        if not self._check_token():
            self.send_response(401)
            self._send_security_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("WWW-Authenticate", 'Bearer realm="PAF-001 Dashboard"')
            self.end_headers()
            self.wfile.write(b"401 Unauthorized - token requis (?t=TOKEN ou header Authorization: Bearer TOKEN)")
            return

        # Normaliser path (strip token query string pour la logique de routage)
        clean_path = self.path.split("?")[0]

        # ── API JSON ─────────────────────────────────────────────────────
        if clean_path == "/api/data":
            try:
                data = _build_live_data()
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._send_security_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
            return

        # ── Dashboard HTML ────────────────────────────────────────────────
        if clean_path in ("/", "/dashboard"):
            try:
                html = DASHBOARD_HTML.read_text(encoding="utf-8")
                data = _build_live_data()
                html = _inject_live_data(html, data)
                body = html.encode("utf-8")
                self.send_response(200)
                self._send_security_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Erreur: {e}".encode())
            return

        self.send_response(404)
        self.end_headers()


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    url       = f"http://localhost:{PORT}"
    url_auth  = f"{url}/?t={_DASHBOARD_TOKEN}"

    print("=" * 60)
    print("  PAF-001 Dashboard Server  —  LIVE (3s polling)")
    print("=" * 60)
    print(f"  Dashboard : {url_auth}")
    print(f"  API JSON  : {url}/api/data?t={_DASHBOARD_TOKEN}")
    print(f"  Token     : {_DASHBOARD_TOKEN}  (généré à ce démarrage)")
    print(f"  DB        : {DB_PATH.resolve()}")
    print(f"  Signal DB : {SIGNAL_DB.resolve()}")
    print(f"  Polling   : toutes les {POLL_MS}ms (pas de rechargement)")
    print("=" * 60)
    print("  Pour fixer le token : DASHBOARD_TOKEN=<valeur> dans .env")
    print("  Ctrl+C pour arrêter")
    print()

    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    webbrowser.open(url_auth)  # ouvre avec le token intégré dans l'URL
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServeur arrêté.")
