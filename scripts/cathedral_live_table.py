#!/usr/bin/env python3
"""Local live table for Cathedral board/miner stream.

Stdlib-only. Serves a tiny localhost dashboard that polls the public Cathedral
publisher and renders active challenges, recent solves, top miners, and weights.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import statistics
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_BASE = "https://api.cathedral.computer"
DEFAULT_LIMIT = 300


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cathedral Live Board</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101214;
      --panel: #171b1f;
      --panel-2: #1d2329;
      --text: #e8ecef;
      --muted: #98a2ad;
      --line: #2d363f;
      --good: #58d68d;
      --warn: #f7c948;
      --bad: #ff6b6b;
      --accent: #7cc7ff;
      --canonical: #58d68d;
      --diagnostic: #f7c948;
      --fallback: #ff9f43;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 12px 16px;
      background: rgba(16, 18, 20, 0.96);
      border-bottom: 1px solid var(--line);
    }
    h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: 0; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    button, select, input {
      background: var(--panel-2);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      font: inherit;
    }
    button { cursor: pointer; }
    button:hover { border-color: var(--accent); }
    input { min-width: 240px; }
    main { padding: 14px 16px 24px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(142px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      min-height: 72px;
    }
    .card.canonical { border-color: rgba(88, 214, 141, .45); }
    .card.diagnostic { border-color: rgba(247, 201, 72, .45); }
    .card.fallback { border-color: rgba(255, 159, 67, .45); }
    .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .value { font-size: 22px; font-weight: 700; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .small {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
      overflow-wrap: anywhere;
    }
    .good { color: var(--good); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .tabs { display: flex; gap: 8px; margin: 12px 0; flex-wrap: wrap; }
    .tab.active { border-color: var(--accent); color: var(--accent); }
    section { display: none; }
    section.active { display: block; }
    .table-wrap {
      height: calc(100vh - 250px);
      min-height: 420px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .chart-wrap {
      max-height: 42vh;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      margin-bottom: 10px;
    }
    .chart-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
      flex-wrap: wrap;
    }
    .legend {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .legend span {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }
    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 2px;
      display: inline-block;
    }
    .chart-row {
      display: grid;
      grid-template-columns: minmax(160px, 260px) 1fr 96px;
      gap: 8px;
      align-items: center;
      padding: 5px 0;
      border-top: 1px solid rgba(255, 255, 255, 0.045);
    }
    .chart-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .chart-sub {
      color: var(--muted);
      font-size: 11px;
    }
    .bar {
      height: 18px;
      display: flex;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 4px;
      background: #11161b;
    }
    .seg { min-width: 2px; height: 100%; }
    .b-r1 { background: #58d68d; }
    .b-r2-10 { background: #7cc7ff; }
    .b-r11-50 { background: #f7c948; }
    .b-r51-100 { background: #ff9f43; }
    .b-r101 { background: #ff6b6b; }
    .b-t3 { background: #c084fc; }
    .chart-num {
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      white-space: nowrap;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 6px 8px;
      vertical-align: top;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 5;
      background: #20262d;
      color: #cdd6df;
      text-align: left;
      font-weight: 650;
    }
    tr.new-row td { background: rgba(124, 199, 255, 0.12); }
    tr:hover td { background: rgba(255, 255, 255, 0.035); }
    .mono { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
    .right { text-align: right; }
    .status-line {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      margin-bottom: 10px;
      flex-wrap: wrap;
    }
    .source-tag {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 2px 7px;
      border-radius: 999px;
      border: 1px solid var(--line);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .04em;
      white-space: nowrap;
    }
    .source-tag.canonical { color: var(--canonical); border-color: rgba(88, 214, 141, .45); }
    .source-tag.diagnostic { color: var(--diagnostic); border-color: rgba(247, 201, 72, .45); }
    .source-tag.fallback { color: var(--fallback); border-color: rgba(255, 159, 67, .45); }
    .truth-strip, .pipeline, .lane-grid {
      display: grid;
      gap: 10px;
      margin-bottom: 12px;
    }
    .truth-strip { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .pipeline { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .lane-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .flow-step, .lane, .section-note {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
    }
    .flow-title, .lane-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-weight: 650;
      margin-bottom: 6px;
    }
    .flow-value, .lane-value {
      font-size: 20px;
      font-weight: 700;
      margin-bottom: 4px;
    }
    .source-line {
      color: var(--muted);
      font-size: 11px;
      margin-top: 6px;
      overflow-wrap: anywhere;
    }
    .section-note {
      color: var(--muted);
      margin-bottom: 10px;
    }
    .section-note strong { color: var(--text); }
    .endpoint-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(150px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .endpoint-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      min-height: 92px;
    }
    .endpoint-card.good { border-color: rgba(88, 214, 141, .45); }
    .endpoint-card.warn { border-color: rgba(247, 201, 72, .55); }
    .endpoint-card.bad { border-color: rgba(255, 107, 107, .60); }
    .endpoint-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 5px;
    }
    .endpoint-name {
      font-weight: 650;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      display: inline-block;
      flex: 0 0 auto;
    }
    .dot.good { background: var(--good); box-shadow: 0 0 0 3px rgba(88, 214, 141, .12); }
    .dot.warn { background: var(--warn); box-shadow: 0 0 0 3px rgba(247, 201, 72, .12); }
    .dot.bad { background: var(--bad); box-shadow: 0 0 0 3px rgba(255, 107, 107, .12); }
    .endpoint-value {
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 2px;
    }
    .endpoint-detail {
      color: var(--muted);
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .trend-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .trend-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      min-height: 210px;
    }
    .trend-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .trend-title {
      font-size: 13px;
      font-weight: 650;
    }
    .trend-sub {
      color: var(--muted);
      font-size: 11px;
      margin-top: 2px;
    }
    .trend-value {
      color: var(--text);
      font-size: 18px;
      font-weight: 700;
      text-align: right;
      white-space: nowrap;
    }
    .trend-svg {
      width: 100%;
      height: 150px;
      display: block;
      overflow: visible;
    }
    .trend-empty {
      display: grid;
      place-items: center;
      height: 150px;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 6px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel);
      white-space: nowrap;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      padding: 12px;
      color: #d6dee6;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      max-height: calc(100vh - 250px);
      overflow: auto;
    }
    @media (max-width: 1100px) {
      .grid { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .endpoint-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
      .trend-grid { grid-template-columns: repeat(2, minmax(240px, 1fr)); }
      .truth-strip, .pipeline, .lane-grid { grid-template-columns: 1fr; }
      header { grid-template-columns: 1fr; }
      .controls { justify-content: flex-start; }
      input { min-width: 100%; }
    }
    @media (max-width: 700px) {
      header { position: static; }
      .grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
      .endpoint-grid { grid-template-columns: 1fr; }
      .trend-grid { grid-template-columns: 1fr; }
      .truth-strip, .pipeline, .lane-grid { grid-template-columns: 1fr; }
      .card { min-height: 58px; }
      .value { font-size: 18px; }
      .chart-row { grid-template-columns: 1fr; gap: 4px; }
      .chart-num { text-align: left; }
      .table-wrap { height: 60vh; min-height: 340px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Cathedral Live Board</h1>
      <div class="sub">Canonical telemetry. Signed weights are truth; public fallbacks are labeled.</div>
    </div>
    <div class="controls">
      <input id="filter" placeholder="filter tables: coldkey, hotkey, challenge, rank, tier...">
      <select id="interval">
        <option value="2000">2s</option>
        <option value="3000" selected>3s</option>
        <option value="5000">5s</option>
        <option value="10000">10s</option>
      </select>
      <button id="pause">Pause</button>
      <button id="refresh">Refresh</button>
    </div>
  </header>
  <main>
    <div class="grid" id="cards">
      <div class="card">
        <div class="label">loading</div>
        <div class="value warn">snapshot</div>
        <div class="small">waiting for Cathedral API data</div>
      </div>
    </div>
    <div class="status-line" id="status"><span class="pill warn">loading first snapshot...</span></div>
    <div class="endpoint-grid" id="endpointStatus"></div>
    <div class="truth-strip" id="truthStrip"></div>
    <div class="pipeline" id="pipeline"></div>
    <div class="lane-grid" id="laneStatus"></div>
    <div class="trend-grid" id="trendCharts"></div>
    <div class="tabs">
      <button class="tab active" data-tab="truth">Score Truth</button>
      <button class="tab" data-tab="stream">Verified Work</button>
      <button class="tab" data-tab="active">Live Work</button>
      <button class="tab" data-tab="coldkeys">Identity Fallback</button>
      <button class="tab" data-tab="top">Diagnostics</button>
      <button class="tab" data-tab="raw">Raw</button>
    </div>

    <section id="truth" class="active">
      <div class="section-note" id="truthPanel">waiting for signed weight vector...</div>
      <div class="table-wrap"><table id="weightsTable"></table></div>
    </section>
    <section id="stream">
      <div class="section-note" id="verifiedPanel">waiting for verified solve receipts...</div>
      <div class="table-wrap"><table id="streamTable"></table></div>
    </section>
    <section id="coldkeys">
      <div class="section-note" id="identityPanel">waiting for identity diagnostics...</div>
      <div class="chart-wrap">
        <div class="chart-meta">
          <div id="coldkeyChartSummary">waiting for rank distribution...</div>
          <div class="legend">
            <span><i class="swatch b-r1"></i>r1</span>
            <span><i class="swatch b-r2-10"></i>r2-10</span>
            <span><i class="swatch b-r11-50"></i>r11-50</span>
            <span><i class="swatch b-r51-100"></i>r51-100</span>
            <span><i class="swatch b-r101"></i>r101+</span>
          </div>
        </div>
        <div id="coldkeyChart"></div>
      </div>
      <div class="table-wrap"><table id="coldkeyTable"></table></div>
    </section>
    <section id="active">
      <div class="section-note" id="livePanel">waiting for active work...</div>
      <div class="table-wrap"><table id="activeTable"></table></div>
    </section>
    <section id="top">
      <div class="section-note" id="diagnosticPanel">waiting for diagnostic miner ranking...</div>
      <div class="table-wrap"><table id="topTable"></table></div>
    </section>
    <section id="raw">
      <pre id="rawJson"></pre>
    </section>
  </main>

  <script>
    let paused = false;
    let loading = false;
    let timer = null;
    let lastSnapshot = null;
    let seenSolveIds = new Set();
    let streamRows = [];
    let lastNewIds = new Set();
    let historySamples = [];
    let lastHistoryKey = '';
    const maxStreamRows = 1500;
    const maxHistorySamples = 240;
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
    let didInitialScrollReset = false;

    function resetInitialScroll() {
      if (didInitialScrollReset) return;
      didInitialScrollReset = true;
      window.scrollTo(0, 0);
    }
    window.scrollTo(0, 0);
    requestAnimationFrame(() => window.scrollTo(0, 0));
    window.addEventListener('load', () => setTimeout(resetInitialScroll, 0), {once: true});

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const short = (v, n=10) => {
      const s = String(v ?? '');
      return s.length > n ? s.slice(0, n) + '...' : s;
    };
    const fmtAge = (seconds) => seconds == null ? '' : `${Number(seconds).toFixed(1)}s`;
    const clsFor = (ok) => ok ? 'good' : 'bad';

    function card(label, value, small='', cls='', kind='') {
      return `<div class="card ${esc(kind)}"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div><div class="small">${esc(small)}</div></div>`;
    }

    function tag(kind, label) {
      return `<span class="source-tag ${esc(kind)}">${esc(label)}</span>`;
    }

    function fmtPct(value, digits=2) {
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return `${(n * 100).toFixed(digits)}%`;
    }

    function fmtNum(value, digits=3) {
      const n = Number(value);
      if (!Number.isFinite(n)) return '';
      return n.toFixed(digits).replace(/\.?0+$/, '');
    }

    function firstDefined(...values) {
      return values.find(v => v !== undefined && v !== null && v !== '');
    }

    function boolText(value) {
      if (value === true) return 'yes';
      if (value === false) return 'no';
      return 'unknown';
    }

    function visibilityFor(row) {
      return (row && row.visibility) || {};
    }

    function sourceSummary(visibility) {
      const sources = (visibility && visibility.sources) || {};
      const payment = sources.payment || {};
      const chainSource = sources.chain || {};
      const pay = payment.status || visibility.current_signed_weight_status || 'unknown';
      const chain = chainSource.status || ((visibility.chain || {}).source || 'unknown');
      const payAge = payment.staleness_seconds == null ? '' : ` ${fmtAge(payment.staleness_seconds)}`;
      const chainAge = chainSource.staleness_seconds == null ? '' : ` ${fmtAge(chainSource.staleness_seconds)}`;
      return `payment=${pay}${payAge} chain=${chain}${chainAge}`;
    }

    function tierCountsText(counts) {
      return `t1=${counts[1] || counts['1'] || 0} t2=${counts[2] || counts['2'] || 0} t3=${counts[3] || counts['3'] || 0}`;
    }

    function sourceLine(source, dataTime, pollTime, note='') {
      const parts = [`source=${source}`];
      if (dataTime) parts.push(`data=${dataTime}`);
      if (pollTime) parts.push(`poll=${pollTime}`);
      if (note) parts.push(note);
      return `<div class="source-line">${esc(parts.join(' | '))}</div>`;
    }

    function metadata(weights) {
      return (weights && weights.policy_metadata) || {};
    }

    function perminerMeta(weights) {
      const meta = metadata(weights);
      const nested = meta.perminer || {};
      return {
        enabled: Boolean(nested.enabled ?? meta.perminer_enabled ?? meta.perminer_scoring_mode),
        shadow: Boolean(nested.shadow ?? meta.perminer_shadow),
        liveRequested: Boolean(nested.live_requested ?? meta.perminer_live_requested),
        hasScores: Boolean(nested.has_scores ?? meta.perminer_has_scores),
        epoch: nested.epoch ?? meta.perminer_epoch ?? '',
        mode: meta.perminer_scoring_mode || (nested.enabled ? (nested.shadow ? 'shadow' : 'live') : 'off'),
        bonus: meta.perminer_bonus_multiplier,
        floor: meta.perminer_history_floor,
      };
    }

    function pollTime(snapshot, name) {
      return (snapshot.fetch_times && snapshot.fetch_times[name]) || '';
    }

    function endpointRows(snapshot) {
      const status = snapshot.endpoint_status || {};
      const order = ['health', 'active', 'weights', 'recent', 'top'];
      return order.map(name => ({
        name,
        ...(status[name] || {name, ok: false, error: 'missing from snapshot'}),
      }));
    }

    function endpointClass(row) {
      if (!row.ok) return 'bad';
      if (row.stale || Number(row.elapsed_seconds || 0) > 5) return 'warn';
      return 'good';
    }

    function renderEndpointStatus(snapshot) {
      const rows = endpointRows(snapshot);
      $('endpointStatus').innerHTML = rows.map(row => {
        const cls = endpointClass(row);
        const age = row.cache_age_seconds == null ? 'no cache' : `${Number(row.cache_age_seconds).toFixed(1)}s cache`;
        const latency = row.elapsed_seconds == null ? 'n/a' : `${Number(row.elapsed_seconds).toFixed(3)}s`;
        const status = row.status ? `HTTP ${row.status}` : (row.ok ? 'cached' : 'error');
        const value = row.ok ? latency : status;
        const route = row.fallback_url ? `fallback ${row.fallback_url}` : (row.url || row.path || '');
        const detail = row.ok
          ? `${status} | ${age} | timeout ${row.timeout_seconds ?? 'n/a'}s`
          : `${short(row.error || 'unknown error', 72)} | ${age}`;
        return `<div class="endpoint-card ${cls}">
          <div class="endpoint-head">
            <div class="endpoint-name">${esc(row.name)}</div>
            <span class="dot ${cls}"></span>
          </div>
          <div class="endpoint-value ${cls}">${esc(value)}</div>
          <div class="endpoint-detail" title="${esc(row.path || '')}">${esc(row.path || '')}</div>
          <div class="endpoint-detail" title="${esc(route)}">${esc(short(route, 72))}</div>
          <div class="endpoint-detail" title="${esc(detail)}">${esc(detail)}</div>
        </div>`;
      }).join('');
    }

    function showError(prefix, err) {
      const message = err && err.message ? err.message : String(err || 'unknown error');
      $('status').innerHTML = `<span class="pill bad">${esc(prefix)}: ${esc(message)}</span>`;
      if (!lastSnapshot) {
        $('cards').innerHTML = card('dashboard error', message, 'server is still available at /health and /snapshot', 'bad');
      }
    }

    function rowMatches(rowText) {
      const q = $('filter').value.trim().toLowerCase();
      return !q || rowText.toLowerCase().includes(q);
    }

    function renderTable(id, cols, rows, opts={}) {
      const head = `<thead><tr>${cols.map(c => `<th style="width:${c.w || 'auto'}">${esc(c.label)}</th>`).join('')}</tr></thead>`;
      const body = rows.map(r => {
        const text = cols.map(c => r[c.key]).join(' ');
        if (!rowMatches(text)) return '';
        const isNew = opts.newIds && opts.newIds.has(String(r.id || ''));
        return `<tr class="${isNew ? 'new-row' : ''}">${cols.map(c => `<td class="${c.cls || ''}" title="${esc(r[c.key])}">${esc(r[c.key])}</td>`).join('')}</tr>`;
      }).join('');
      $(id).innerHTML = head + `<tbody>${body}</tbody>`;
    }

    function normalizeRecent(snapshot) {
      const rows = (snapshot.recent && snapshot.recent.items) || [];
      return rows
        .filter(r => r.eval_output_schema_version === 6)
        .map(r => ({
          id: r.id,
          ran_at: r.ran_at,
          age: r.age_seconds == null ? '' : `${Number(r.age_seconds).toFixed(1)}s`,
          miner: short(r.miner_hotkey, 14),
          miner_full: r.miner_hotkey,
          tier: r.difficulty_tier,
          rank: r.solve_rank,
          score: r.weighted_score,
          task: short(r.task_id_public, 14),
          solved: r.solved,
          rejection: r.rejection_reason || '',
          answer: short(r.answer_hash, 12),
          source: 'recent_v6_receipt',
        }));
    }

    function mergeStream(snapshot) {
      lastNewIds = new Set();
      for (const r of normalizeRecent(snapshot).reverse()) {
        if (!seenSolveIds.has(String(r.id))) {
          seenSolveIds.add(String(r.id));
          lastNewIds.add(String(r.id));
          streamRows.unshift(r);
        }
      }
      if (streamRows.length > maxStreamRows) streamRows = streamRows.slice(0, maxStreamRows);
    }

    function median(nums) {
      if (!nums.length) return '';
      const arr = nums.slice().sort((a, b) => a - b);
      const mid = Math.floor(arr.length / 2);
      return arr.length % 2 ? arr[mid] : ((arr[mid - 1] + arr[mid]) / 2).toFixed(1);
    }

    function rankBucket(rank) {
      const r = Number(rank || 0);
      if (r === 1) return 'r1';
      if (r <= 10) return 'r2_10';
      if (r <= 50) return 'r11_50';
      if (r <= 100) return 'r51_100';
      return 'r101_plus';
    }

    const num = (v, fallback=0) => {
      const n = Number(v);
      return Number.isFinite(n) ? n : fallback;
    };

    function addHistorySample(snapshot) {
      const d = snapshot.derived || {};
      const active = snapshot.active || {};
      const activeByTier = d.active_by_tier || {};
      const recentByTier = d.recent_v6_by_tier || {};
      const key = [
        d.checked_at || '',
        (snapshot.recent && snapshot.recent.next_since_id) || '',
        active.count ?? '',
        d.recent_v6_solves_sampled ?? '',
        d.latest_solve_age_seconds ?? '',
      ].join('|');
      if (key === lastHistoryKey) return;
      lastHistoryKey = key;
      const ts = Date.parse(d.checked_at || '') || Date.now();
      const sampleWindow = num(d.recent_window_seconds_in_sample);
      historySamples.push({
        ts,
        label: new Date(ts).toLocaleTimeString(),
        active: num(active.count),
        active_t1: num(activeByTier[1] || activeByTier['1']),
        active_t2: num(activeByTier[2] || activeByTier['2']),
        active_t3: num(activeByTier[3] || activeByTier['3']),
        solves_per_sec: sampleWindow > 0 ? num(d.recent_v6_solves_sampled) / sampleWindow : 0,
        latest_age: num(d.latest_solve_age_seconds, null),
        distinct_miners: num(d.recent_v6_distinct_miners),
        recent_t1: num(recentByTier[1] || recentByTier['1']),
        recent_t2: num(recentByTier[2] || recentByTier['2']),
        recent_t3: num(recentByTier[3] || recentByTier['3']),
        top_spread: num(d.top_distinct_solves_spread, null),
        errors: Object.keys(snapshot.errors || {}).length,
        endpoint_errors: endpointRows(snapshot).filter(r => !r.ok).length,
        endpoint_stale: endpointRows(snapshot).filter(r => r.stale).length,
        health_latency: num((snapshot.endpoint_status || {}).health?.elapsed_seconds, null),
        active_latency: num((snapshot.endpoint_status || {}).active?.elapsed_seconds, null),
        weights_latency: num((snapshot.endpoint_status || {}).weights?.elapsed_seconds, null),
        recent_latency: num((snapshot.endpoint_status || {}).recent?.elapsed_seconds, null),
        top_latency: num((snapshot.endpoint_status || {}).top?.elapsed_seconds, null),
      });
      if (historySamples.length > maxHistorySamples) {
        historySamples = historySamples.slice(-maxHistorySamples);
      }
    }

    function latestValue(key, digits=0, suffix='') {
      const latest = historySamples[historySamples.length - 1];
      if (!latest || latest[key] == null) return '...';
      const value = Number(latest[key]);
      if (!Number.isFinite(value)) return '...';
      return `${digits ? value.toFixed(digits) : Math.round(value)}${suffix}`;
    }

    function seriesRange(series) {
      const vals = [];
      for (const s of historySamples) {
        for (const spec of series) {
          const value = Number(s[spec.key]);
          if (Number.isFinite(value)) vals.push(value);
        }
      }
      if (!vals.length) return [0, 1];
      let min = Math.min(...vals);
      let max = Math.max(...vals);
      if (min === max) {
        min = Math.max(0, min - 1);
        max = max + 1;
      }
      if (min > 0 && max / Math.max(min, 0.0001) < 4) min = 0;
      return [min, max];
    }

    function lineSvg(series) {
      if (historySamples.length < 2) {
        return `<div class="trend-empty">collecting samples...</div>`;
      }
      const w = 640, h = 150, padL = 38, padR = 10, padT = 12, padB = 22;
      const [minY, maxY] = seriesRange(series);
      const x = (i) => padL + (i / Math.max(historySamples.length - 1, 1)) * (w - padL - padR);
      const y = (v) => padT + (1 - ((v - minY) / (maxY - minY || 1))) * (h - padT - padB);
      const grid = [0, .25, .5, .75, 1].map(t => {
        const gy = padT + t * (h - padT - padB);
        const label = (maxY - t * (maxY - minY)).toFixed(maxY < 10 ? 1 : 0);
        return `<line x1="${padL}" y1="${gy}" x2="${w - padR}" y2="${gy}" stroke="rgba(255,255,255,.08)"/><text x="4" y="${gy + 4}" fill="#98a2ad" font-size="10">${esc(label)}</text>`;
      }).join('');
      const paths = series.map(spec => {
        const pts = historySamples.map((s, i) => {
          const value = Number(s[spec.key]);
          if (!Number.isFinite(value)) return '';
          return `${x(i).toFixed(1)},${y(value).toFixed(1)}`;
        }).filter(Boolean).join(' ');
        return `<polyline points="${pts}" fill="none" stroke="${spec.color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>`;
      }).join('');
      const last = historySamples[historySamples.length - 1];
      const first = historySamples[0];
      const axis = `<text x="${padL}" y="${h - 5}" fill="#98a2ad" font-size="10">${esc(first.label)}</text><text x="${w - padR}" y="${h - 5}" text-anchor="end" fill="#98a2ad" font-size="10">${esc(last.label)}</text>`;
      return `<svg class="trend-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${grid}${paths}${axis}</svg>`;
    }

    function tierMixSvg() {
      if (historySamples.length < 2) {
        return `<div class="trend-empty">collecting samples...</div>`;
      }
      const w = 640, h = 150, padL = 34, padR = 8, padT = 12, padB = 22;
      const maxY = Math.max(1, ...historySamples.map(s => s.recent_t1 + s.recent_t2 + s.recent_t3));
      const barGap = 1;
      const barW = Math.max(2, (w - padL - padR) / historySamples.length - barGap);
      const bars = historySamples.map((s, i) => {
        const x = padL + i * ((w - padL - padR) / historySamples.length);
        const t1h = (s.recent_t1 / maxY) * (h - padT - padB);
        const t2h = (s.recent_t2 / maxY) * (h - padT - padB);
        const t3h = (s.recent_t3 / maxY) * (h - padT - padB);
        const y3 = h - padB - t3h;
        const y2 = y3 - t2h;
        const y1 = y2 - t1h;
        return `<rect x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${barW.toFixed(1)}" height="${t1h.toFixed(1)}" fill="#58d68d"/><rect x="${x.toFixed(1)}" y="${y2.toFixed(1)}" width="${barW.toFixed(1)}" height="${t2h.toFixed(1)}" fill="#7cc7ff"/><rect x="${x.toFixed(1)}" y="${y3.toFixed(1)}" width="${barW.toFixed(1)}" height="${t3h.toFixed(1)}" fill="#c084fc"/>`;
      }).join('');
      const last = historySamples[historySamples.length - 1];
      const first = historySamples[0];
      return `<svg class="trend-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        <line x1="${padL}" y1="${h - padB}" x2="${w - padR}" y2="${h - padB}" stroke="rgba(255,255,255,.12)"/>
        <text x="4" y="${padT + 4}" fill="#98a2ad" font-size="10">${esc(maxY)}</text>
        ${bars}
        <text x="${padL}" y="${h - 5}" fill="#98a2ad" font-size="10">${esc(first.label)}</text>
        <text x="${w - padR}" y="${h - 5}" text-anchor="end" fill="#98a2ad" font-size="10">${esc(last.label)}</text>
      </svg>`;
    }

    function trendCard(title, value, sub, body, legend='') {
      return `<div class="trend-card">
        <div class="trend-head">
          <div>
            <div class="trend-title">${esc(title)}</div>
            <div class="trend-sub">${esc(sub)}</div>
          </div>
          <div class="trend-value">${esc(value)}</div>
        </div>
        ${body}
        ${legend ? `<div class="legend" style="margin-top:6px">${legend}</div>` : ''}
      </div>`;
    }

    function renderTrendCharts() {
      const legendActive = `<span><i class="swatch" style="background:#7cc7ff"></i>active</span><span><i class="swatch" style="background:#58d68d"></i>tier 1</span><span><i class="swatch" style="background:#f7c948"></i>tier 2</span><span><i class="swatch b-t3"></i>tier 3</span>`;
      const legendTier = `<span><i class="swatch b-r1"></i>tier 1 solves</span><span><i class="swatch b-r2-10"></i>tier 2 solves</span><span><i class="swatch b-t3"></i>tier 3 solves</span>`;
      const legendLatency = `<span><i class="swatch" style="background:#58d68d"></i>health</span><span><i class="swatch" style="background:#7cc7ff"></i>active</span><span><i class="swatch" style="background:#f7c948"></i>recent</span><span><i class="swatch" style="background:#c084fc"></i>weights</span><span><i class="swatch" style="background:#ff9f43"></i>top</span>`;
      $('trendCharts').innerHTML = [
        trendCard('Endpoint Latency', latestValue('recent_latency', 2, 's'), 'seconds per successful API fetch',
          lineSvg([
            {key:'health_latency', color:'#58d68d'},
            {key:'active_latency', color:'#7cc7ff'},
            {key:'recent_latency', color:'#f7c948'},
            {key:'weights_latency', color:'#c084fc'},
            {key:'top_latency', color:'#ff9f43'},
          ]), legendLatency),
        trendCard('Endpoint Errors', `${latestValue('endpoint_errors')} / stale ${latestValue('endpoint_stale')}`, 'failed or stale endpoint snapshots',
          lineSvg([
            {key:'endpoint_errors', color:'#ff6b6b'},
            {key:'endpoint_stale', color:'#f7c948'},
          ])),
        trendCard('Active Supply', latestValue('active'), 'live board count by tier',
          lineSvg([
            {key:'active', color:'#7cc7ff'},
            {key:'active_t1', color:'#58d68d'},
            {key:'active_t2', color:'#f7c948'},
            {key:'active_t3', color:'#c084fc'},
          ]), legendActive),
        trendCard('Solve Rate', latestValue('solves_per_sec', 2, '/s'), 'v6 solves per second in the sampled window',
          lineSvg([{key:'solves_per_sec', color:'#58d68d'}])),
        trendCard('Solve Freshness', latestValue('latest_age', 1, 's'), 'age of newest solve; above 60s is bad',
          lineSvg([{key:'latest_age', color:'#ff9f43'}])),
        trendCard('Miner Participation', latestValue('distinct_miners'), 'distinct miners in recent v6 sample',
          lineSvg([{key:'distinct_miners', color:'#7cc7ff'}])),
        trendCard('Recent Tier Mix', `${latestValue('recent_t1')} / ${latestValue('recent_t2')} / ${latestValue('recent_t3')}`, 'stacked recent v6 solves: tier 1 / tier 2 / tier 3',
          tierMixSvg(), legendTier),
        trendCard('Diagnostic Top Spread', latestValue('top_spread'), 'non-canonical /leaderboard/top distinct-solve spread',
          lineSvg([{key:'top_spread', color:'#ff6b6b'}])),
      ].join('');
    }

    function coldkeyRows(snapshot) {
      const rows = streamRows.length ? streamRows : normalizeRecent(snapshot);
      const map = snapshot.coldkey_map || {};
      const groups = new Map();
      for (const r of rows) {
        const hotkey = r.miner_full || '';
        const coldkey = map[hotkey] || hotkey || 'unknown';
        if (!groups.has(coldkey)) {
          groups.set(coldkey, {
            id: coldkey,
            coldkey,
            mapped: map[hotkey] ? 'yes' : 'fallback',
            hotkeys_set: new Set(),
            task_set: new Set(),
            ranks: [],
            solves: 0,
            r1: 0,
            r2_10: 0,
            r11_50: 0,
            r51_100: 0,
            r101_plus: 0,
            t1: 0,
            t2: 0,
            t3: 0,
          });
        }
        const g = groups.get(coldkey);
        g.hotkeys_set.add(hotkey);
        g.task_set.add(r.task);
        g.solves += 1;
        g[rankBucket(r.rank)] += 1;
        if (Number(r.tier) === 1) g.t1 += 1;
        if (Number(r.tier) === 2) g.t2 += 1;
        if (Number(r.tier) === 3) g.t3 += 1;
        if (Number(r.rank)) g.ranks.push(Number(r.rank));
        if (g.mapped !== 'yes' && map[hotkey]) g.mapped = 'yes';
      }
      return [...groups.values()].map(g => {
        const ranks = g.ranks.slice().sort((a, b) => a - b);
        const sum = ranks.reduce((a, b) => a + b, 0);
        return {
          id: g.id,
          coldkey: short(g.coldkey, 18),
          coldkey_full: g.coldkey,
          mapped: g.mapped,
          hotkeys: g.hotkeys_set.size,
          solves: g.solves,
          avg_rank: ranks.length ? (sum / ranks.length).toFixed(1) : '',
          med_rank: median(ranks),
          best_rank: ranks.length ? ranks[0] : '',
          worst_rank: ranks.length ? ranks[ranks.length - 1] : '',
          r1: g.r1,
          r2_10: g.r2_10,
          r11_50: g.r11_50,
          r51_100: g.r51_100,
          r101_plus: g.r101_plus,
          t1: g.t1,
          t2: g.t2,
          t3: g.t3,
          tasks: g.task_set.size,
        };
      }).sort((a, b) => Number(a.avg_rank || 999999) - Number(b.avg_rank || 999999));
    }

    function renderColdkeyChart(rows) {
      const filtered = rows.filter(r => rowMatches([
        r.coldkey_full,
        r.mapped,
        r.hotkeys,
        r.solves,
        r.avg_rank,
        r.best_rank,
        r.worst_rank,
        r.t1,
        r.t2,
        r.t3
      ].join(' ')));
      const visible = filtered.slice(0, 80);
      $('coldkeyChartSummary').textContent =
        `showing ${visible.length} of ${filtered.length} identities; sorted by average solve rank`;
      if (!visible.length) {
        $('coldkeyChart').innerHTML = `<div class="chart-sub">No coldkey rows match the current filter.</div>`;
        return;
      }
      $('coldkeyChart').innerHTML = visible.map(r => {
        const total = Math.max(Number(r.solves || 0), 1);
        const segs = [
          ['r1', 'b-r1', r.r1],
          ['r2-10', 'b-r2-10', r.r2_10],
          ['r11-50', 'b-r11-50', r.r11_50],
          ['r51-100', 'b-r51-100', r.r51_100],
          ['r101+', 'b-r101', r.r101_plus],
        ].filter(([, , v]) => Number(v || 0) > 0).map(([label, cls, value]) => {
          const pct = (Number(value) / total) * 100;
          return `<div class="seg ${cls}" style="width:${pct.toFixed(3)}%" title="${esc(label)}: ${esc(value)}"></div>`;
        }).join('');
        return `<div class="chart-row">
          <div class="chart-label mono" title="${esc(r.coldkey_full)}">
            ${esc(r.coldkey)}
            <div class="chart-sub">${esc(r.mapped)} · ${esc(r.hotkeys)} hk · ${esc(r.solves)} solves</div>
          </div>
          <div class="bar">${segs}</div>
          <div class="chart-num">avg ${esc(r.avg_rank)} · best ${esc(r.best_rank)}</div>
        </div>`;
      }).join('');
    }

    function flowStep(title, value, kind, label, small, source, dataTime, pollTimeValue, note='') {
      return `<div class="flow-step">
        <div class="flow-title"><span>${esc(title)}</span>${tag(kind, label)}</div>
        <div class="flow-value">${esc(value)}</div>
        <div class="small">${esc(small)}</div>
        ${sourceLine(source, dataTime, pollTimeValue, note)}
      </div>`;
    }

    function renderTruthPanels(snapshot) {
      const d = snapshot.derived || {};
      const weights = snapshot.weights || {};
      const active = snapshot.active || {};
      const meta = metadata(weights);
      const pm = d.perminer || perminerMeta(weights);
      const activeByTier = d.active_by_tier || {};
      const recentByTier = d.recent_v6_by_tier || {};
      const weightRows = weights.weights || [];
      const tierWeights = d.tier_weights || meta.tier_weights || {};
      const tierWeightText = [1, 2, 3].map(t => {
        const v = tierWeights[t] ?? tierWeights[String(t)];
        return `t${t}=${v == null ? '0' : v}`;
      }).join(' ');
      const topMiner = d.weight_top_hotkey ? `${short(d.weight_top_hotkey, 14)} weight ${fmtNum(d.weight_max)} share ${fmtPct(d.weight_top_share)}` : 'no signed rows yet';
      const policyMode = d.score_source || d.effective_mode || d.requested_mode || weights.policy_reason || 'unknown';
      const coldkeyTruth = d.validator_coldkey_map_loaded === true
        ? 'validator metadata: loaded'
        : d.validator_coldkey_map_loaded === false
          ? 'validator metadata: not loaded'
          : 'validator metadata: not exposed';
      const identityMode = d.coldkey_map_entries ? 'local map loaded' : 'local identity fallback';
      const pmText = pm.enabled
        ? `${pm.mode || (pm.shadow ? 'shadow' : 'live')} epoch=${pm.epoch || 'unknown'} bonus=${pm.bonus ?? pm.bonus_multiplier ?? 'n/a'} floor=${pm.floor ?? pm.history_floor ?? 'n/a'}`
        : 'off or not exposed';

      $('truthStrip').innerHTML = [
        flowStep('Score Truth', `${d.weights_count || 0} signed weights`, 'canonical', 'canonical',
          topMiner, '/v1/validator/weights/next', weights.generated_at, pollTime(snapshot, 'weights'),
          d.signature_present ? 'signature present' : 'signature missing'),
        flowStep('Policy', weights.policy_reason || policyMode, 'canonical', 'chain-bound',
          `mode=${policyMode} burn=${d.burn_percentage ?? 'n/a'}% tier_weights ${tierWeightText}; per-miner ${pmText}`,
          '/v1/validator/weights/next', weights.generated_at, pollTime(snapshot, 'weights'),
          `hash=${short(d.policy_hash, 22)}`),
        flowStep('Identity Caveat', identityMode, 'fallback', 'fallback',
          `${coldkeyTruth}; grouped operator rows are not canonical`,
          'local dashboard map plus weight metadata', d.weights_generated_at, pollTime(snapshot, 'weights')),
      ].join('');

      $('pipeline').innerHTML = [
        flowStep('1. Work Issued', `${active.count ?? 0} active`, 'canonical', 'canonical',
          tierCountsText(activeByTier), '/v1/synthetic-boolean/active-challenges',
          active.generated_at || '', pollTime(snapshot, 'active'), 'active set legitimately churns'),
        flowStep('2. Work Verified', `${d.recent_v6_solves_sampled || 0} v6 receipts`, 'canonical', 'canonical',
          `${tierCountsText(recentByTier)} latest=${fmtAge(d.latest_solve_age_seconds)}`,
          '/v1/leaderboard/recent', d.latest_receipt_data_time, pollTime(snapshot, 'recent'),
          `${d.recent_v5compat_rows_sampled || 0} v5compat rows sampled`),
        flowStep('3. Work Scored', `${d.miner_count_in_vector ?? d.weights_count ?? 0} miners`, 'canonical', 'canonical',
          `score_source=${policyMode}; total_weight=${fmtNum(d.weight_total)}`,
          '/v1/validator/weights/next', weights.generated_at, pollTime(snapshot, 'weights'),
          'this is the emissions input'),
      ].join('');

      $('laneStatus').innerHTML = [
        `<div class="lane">
          <div class="lane-title"><span>SAT Verification</span>${tag('canonical', 'live')}</div>
          <div class="lane-value">LIVE</div>
          <div class="small">SAT-only today. Verified solves feed signed weights.</div>
          ${sourceLine('active + recent + weights', d.latest_receipt_data_time, d.checked_at)}
        </div>`,
        `<div class="lane">
          <div class="lane-title"><span>Attested Compute</span>${tag('diagnostic', 'gated')}</div>
          <div class="lane-value">GATED</div>
          <div class="small">Pipeline-ready. Listing-gated for approved 8x RTX PRO 6000 style profiles.</div>
          ${sourceLine('roadmap/status only', '', d.checked_at, 'not a scoring feed here')}
        </div>`,
        `<div class="lane">
          <div class="lane-title"><span>Distillation</span>${tag('diagnostic', 'scaffold')}</div>
          <div class="lane-value">SCAFFOLD</div>
          <div class="small">Verified traces become dataset candidates after replay paths land.</div>
          ${sourceLine('roadmap/status only', '', d.checked_at, 'not live scoring')}
        </div>`,
      ].join('');

      $('truthPanel').innerHTML =
        `${tag('canonical', 'canonical')} <strong>The score is the signed validator weight vector.</strong> ` +
        `This table is the output validators consume. Recent solves, top miners, and coldkey charts explain inputs or diagnostics only. ` +
        `Policy hash <span class="mono">${esc(d.policy_hash || '')}</span>; key <span class="mono">${esc(d.key_id || '')}</span>; expires ${esc(d.weights_expires_at || '')}.`;
      $('verifiedPanel').innerHTML =
        `${tag('canonical', 'canonical')} Verified receipts from <span class="mono">/v1/leaderboard/recent</span>. ` +
        `Rendered rows are v6 solve receipts; v5compat rows are counted separately. Tiers are always shown as T1/T2/T3, including zeros.`;
      $('livePanel').innerHTML =
        `${tag('canonical', 'canonical')} Live active work from <span class="mono">/v1/synthetic-boolean/active-challenges</span>. ` +
        `The count is a moving gauge, not a fixed target; churn is expected as miners solve and refill runs.`;
      $('identityPanel').innerHTML =
        `${tag('fallback', 'fallback')} Coldkey rows here use ${esc(identityMode)}. ` +
        `They are useful for spotting duplication patterns, but not canonical confirmed operator identity. ${esc(coldkeyTruth)}.`;
      $('diagnosticPanel').innerHTML =
        `${tag('diagnostic', 'diagnostic')} <span class="mono">/v1/leaderboard/top?window=24h</span> is a rough cached view. ` +
        `Do not use it as scoring truth; use the signed vector table.`;
    }

    function render(snapshot) {
      lastSnapshot = snapshot;
      resetInitialScroll();
      mergeStream(snapshot);
      const d = snapshot.derived || {};
      const health = snapshot.health || {};
      const active = snapshot.active || {};
      const weights = snapshot.weights || {};
      const top = snapshot.top || {};
      const errs = snapshot.errors || {};
      const activeByTier = d.active_by_tier || {};
      const recentByTier = d.recent_v6_by_tier || {};
      const hardErrorCount = Object.keys(errs).filter(k => k !== 'refresh').length;
      const refreshNote = errs.refresh ? ' refresh=queued' : '';
      addHistorySample(snapshot);
      renderEndpointStatus(snapshot);
      renderTruthPanels(snapshot);

      $('cards').innerHTML = [
        card('score truth', `${d.weights_count || 0} weights`, weights.policy_reason || 'signed vector unavailable', d.signature_present ? 'good' : 'bad', 'canonical'),
        card('top signed share', fmtPct(d.weight_top_share), short(d.weight_top_hotkey || 'no top miner', 22), 'good', 'canonical'),
        card('verified work', `${d.recent_v6_solves_sampled || 0} receipts`, `${tierCountsText(recentByTier)} in sample`, 'good', 'canonical'),
        card('live work', active.count ?? 0, tierCountsText(activeByTier), (active.count || 0) < 5 ? 'warn' : 'good', 'canonical'),
        card('per-miner beta', (d.perminer || {}).mode || ((d.perminer || {}).enabled ? 'enabled' : 'off'), `bonus=${(d.perminer || {}).bonus_multiplier ?? 'n/a'} floor=${(d.perminer || {}).history_floor ?? 'n/a'}`, (d.perminer || {}).enabled ? 'good' : 'warn', 'diagnostic'),
        card('identity grouping', d.coldkey_map_entries ? 'local map' : 'fallback', d.validator_coldkey_map_loaded === true ? 'validator metadata loaded' : 'canonical grouping not exposed', d.coldkey_map_entries ? 'good' : 'warn', 'fallback'),
        card('latest solve', fmtAge(d.latest_solve_age_seconds), `${d.recent_v6_distinct_miners || 0} miners / ${d.recent_window_seconds_in_sample || ''}s sample`, (d.latest_solve_age_seconds || 999) < 60 ? 'good' : 'bad', 'canonical'),
        card('system integrity', health.status || 'unknown', `db=${health.db || ''} signing=${health.signing_key || ''} errors=${hardErrorCount}${refreshNote}`, health.status === 'ok' && health.db === 'ok' && !hardErrorCount ? 'good' : 'bad', hardErrorCount ? 'diagnostic' : 'canonical'),
      ].join('');

      $('status').innerHTML = [
        `<span class="pill">poll ${esc(d.checked_at || '')}</span>`,
        `<span class="pill">weights data ${esc(weights.generated_at || 'missing')}</span>`,
        `<span class="pill">recent data ${esc(d.latest_receipt_data_time || 'missing')}</span>`,
        `<span class="pill">stream rows kept ${streamRows.length}</span>`,
        `<span class="pill">new rows ${lastNewIds.size}</span>`,
        `<span class="pill">chart samples ${historySamples.length}</span>`,
        `<span class="pill">auto ${paused ? 'paused' : 'running'}</span>`,
      ].join('');
      renderTrendCharts();

      renderTable('streamTable', [
        {key:'ran_at', label:'ran_at', w:'190px', cls:'mono'},
        {key:'age', label:'age', w:'70px', cls:'right'},
        {key:'miner', label:'miner', w:'130px', cls:'mono'},
        {key:'tier', label:'tier', w:'60px', cls:'right'},
        {key:'rank', label:'rank', w:'70px', cls:'right'},
        {key:'score', label:'score', w:'80px', cls:'right'},
        {key:'task', label:'task', w:'120px', cls:'mono'},
        {key:'solved', label:'solved', w:'70px'},
        {key:'rejection', label:'rejection', w:'160px'},
        {key:'answer', label:'answer', w:'120px', cls:'mono'},
        {key:'source', label:'source', w:'150px'},
        {key:'id', label:'id', w:'260px', cls:'mono'},
      ], streamRows, {newIds: lastNewIds});

      const cRows = coldkeyRows(snapshot);
      renderColdkeyChart(cRows);
      renderTable('coldkeyTable', [
        {key:'coldkey', label:'coldkey', w:'180px', cls:'mono'},
        {key:'mapped', label:'map', w:'80px'},
        {key:'hotkeys', label:'hotkeys', w:'80px', cls:'right'},
        {key:'solves', label:'solves', w:'80px', cls:'right'},
        {key:'avg_rank', label:'avg rank', w:'90px', cls:'right'},
        {key:'med_rank', label:'med rank', w:'90px', cls:'right'},
        {key:'best_rank', label:'best', w:'70px', cls:'right'},
        {key:'worst_rank', label:'worst', w:'70px', cls:'right'},
        {key:'r1', label:'r1', w:'60px', cls:'right'},
        {key:'r2_10', label:'r2-10', w:'70px', cls:'right'},
        {key:'r11_50', label:'r11-50', w:'80px', cls:'right'},
        {key:'r51_100', label:'r51-100', w:'80px', cls:'right'},
        {key:'r101_plus', label:'r101+', w:'80px', cls:'right'},
        {key:'t1', label:'t1', w:'60px', cls:'right'},
        {key:'t2', label:'t2', w:'60px', cls:'right'},
        {key:'t3', label:'t3', w:'60px', cls:'right'},
        {key:'tasks', label:'tasks', w:'70px', cls:'right'},
        {key:'coldkey_full', label:'coldkey_full', w:'420px', cls:'mono'},
      ], cRows);

      const activeRows = ((active.items || []).map(i => ({
        id: i.challenge_id,
        challenge_id: i.challenge_id,
        tier: i.tier,
        vars: i.num_vars,
        clauses: i.num_clauses,
        bytes: i.cnf_bytes,
        score_weight: i.score_multiplier ?? '',
        status: i.status,
        storage: i.storage,
        sha: i.cnf_sha256,
        source: 'active-challenges',
        submit: i.submit_path,
      })));
      renderTable('activeTable', [
        {key:'challenge_id', label:'challenge_id', w:'360px', cls:'mono'},
        {key:'tier', label:'tier', w:'60px', cls:'right'},
        {key:'vars', label:'vars', w:'80px', cls:'right'},
        {key:'clauses', label:'clauses', w:'90px', cls:'right'},
        {key:'bytes', label:'bytes', w:'90px', cls:'right'},
        {key:'score_weight', label:'score_weight', w:'100px', cls:'right'},
        {key:'status', label:'status', w:'90px'},
        {key:'storage', label:'storage', w:'120px'},
        {key:'source', label:'source', w:'140px'},
        {key:'sha', label:'sha256', w:'360px', cls:'mono'},
      ], activeRows);

      const topByHotkey = new Map((top.miners || []).map(m => [m.miner_hotkey, m]));
      const topRows = ((top.miners || []).map((m, idx) => {
        const vis = visibilityFor(m);
        const pm = vis.perminer_contribution || {};
        const activity = vis.recent_activity || {};
        return {
          id: m.miner_hotkey,
          n: idx + 1,
          miner: m.miner_hotkey,
          uid: firstDefined(m.uid, vis.uid, ''),
          registered: boolText(firstDefined(m.registered, vis.registered)),
          payable: boolText(firstDefined(m.payable, vis.payable)),
          signed_weight: fmtNum(firstDefined(m.current_signed_weight, m.current_weight, vis.current_signed_weight), 8),
          chain_incentive: fmtNum(firstDefined(m.chain_incentive, vis.chain_incentive), 8),
          chain_emission: fmtNum(firstDefined(m.chain_emission, vis.chain_emission), 8),
          pm_units: fmtNum(firstDefined(m.perminer_weighted_units, pm.weighted_units), 3),
          recent_activity: `${firstDefined(m.receipt_distinct_solves_24h, activity.receipt_distinct_solves_24h, m.distinct_solves, 0)} solves`,
          last_seen: firstDefined(m.last_seen, activity.last_seen, ''),
          source: sourceSummary(vis),
        };
      }));
      renderTable('topTable', [
        {key:'n', label:'#', w:'50px', cls:'right'},
        {key:'miner', label:'hotkey', w:'420px', cls:'mono'},
        {key:'uid', label:'uid', w:'70px', cls:'right'},
        {key:'registered', label:'registered', w:'100px'},
        {key:'payable', label:'payable', w:'90px'},
        {key:'signed_weight', label:'signed_weight', w:'140px', cls:'right'},
        {key:'chain_incentive', label:'chain_incentive', w:'150px', cls:'right'},
        {key:'chain_emission', label:'chain_emission', w:'150px', cls:'right'},
        {key:'pm_units', label:'pm_units', w:'110px', cls:'right'},
        {key:'recent_activity', label:'recent_activity', w:'140px'},
        {key:'last_seen', label:'last_seen', w:'190px', cls:'mono'},
        {key:'source', label:'source/staleness', w:'260px'},
      ], topRows);

      const totalWeight = Number(d.weight_total || 0);
      const weightRows = ((weights.weights || [])
        .map(w => ({
          id: w.miner_hotkey,
          miner: w.miner_hotkey,
          weight_raw: Number(w.weight || 0),
        }))
        .sort((a,b) => Number(b.weight_raw) - Number(a.weight_raw))
        .map((w, idx) => ({
          id: w.id,
          n: idx + 1,
          miner: w.miner,
          uid: firstDefined(visibilityFor(topByHotkey.get(w.miner)).uid, ''),
          registered: boolText(visibilityFor(topByHotkey.get(w.miner)).registered),
          payable: boolText(visibilityFor(topByHotkey.get(w.miner)).payable),
          weight: fmtNum(w.weight_raw, 8),
          share: totalWeight > 0 ? fmtPct(w.weight_raw / totalWeight, 4) : '',
          chain_incentive: fmtNum(visibilityFor(topByHotkey.get(w.miner)).chain_incentive, 8),
          chain_emission: fmtNum(visibilityFor(topByHotkey.get(w.miner)).chain_emission, 8),
          pm_units: fmtNum((visibilityFor(topByHotkey.get(w.miner)).perminer_contribution || {}).weighted_units, 3),
          activity: `${firstDefined((visibilityFor(topByHotkey.get(w.miner)).recent_activity || {}).receipt_distinct_solves_24h, 0)} solves`,
          source: sourceSummary(visibilityFor(topByHotkey.get(w.miner))),
        })));
      renderTable('weightsTable', [
        {key:'n', label:'#', w:'50px', cls:'right'},
        {key:'miner', label:'hotkey', w:'420px', cls:'mono'},
        {key:'uid', label:'uid', w:'70px', cls:'right'},
        {key:'registered', label:'registered', w:'100px'},
        {key:'payable', label:'payable', w:'90px'},
        {key:'weight', label:'weight', w:'140px', cls:'right'},
        {key:'share', label:'share_of_total', w:'130px', cls:'right'},
        {key:'chain_incentive', label:'chain_incentive', w:'150px', cls:'right'},
        {key:'chain_emission', label:'chain_emission', w:'150px', cls:'right'},
        {key:'pm_units', label:'pm_units', w:'110px', cls:'right'},
        {key:'activity', label:'recent_activity', w:'140px'},
        {key:'source', label:'source/staleness', w:'260px'},
      ], weightRows);

      $('rawJson').textContent = JSON.stringify(snapshot, null, 2);
    }

    async function load() {
      if (paused || loading) return;
      loading = true;
      try {
        if (!lastSnapshot) {
          $('status').innerHTML = `<span class="pill warn">loading first snapshot...</span>`;
        }
        const firstLoad = !lastSnapshot;
        const limit = firstLoad ? 80 : 300;
        const fast = firstLoad ? '&fast=1' : '';
        const res = await fetch(`/snapshot?limit=${limit}${fast}`, {cache: 'no-store'});
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        render(await res.json());
      } catch (err) {
        showError('snapshot failed', err);
      } finally {
        loading = false;
      }
    }

    function schedule() {
      if (timer) clearInterval(timer);
      timer = setInterval(load, Number($('interval').value));
    }

    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
        btn.classList.add('active');
        $(btn.dataset.tab).classList.add('active');
      });
    });
    $('pause').addEventListener('click', () => {
      paused = !paused;
      $('pause').textContent = paused ? 'Resume' : 'Pause';
      if (!paused) load();
    });
    $('refresh').addEventListener('click', load);
    $('interval').addEventListener('change', schedule);
    $('filter').addEventListener('input', () => { if (lastSnapshot) render(lastSnapshot); });
    window.addEventListener('error', event => showError('script error', event.error || event.message));
    window.addEventListener('unhandledrejection', event => showError('async error', event.reason));
    schedule();
    load();
  </script>
</body>
</html>
"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_endpoint(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()

    def read(url: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "cathedral-live-table/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return {
                "ok": True,
                "status": int(getattr(response, "status", 200)),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "url": url,
                "data": json.loads(raw.decode("utf-8")),
            }

    root = base_url.rstrip("/")
    try:
        return read(root + path)
    except Exception as exc:  # noqa: BLE001 - dashboard should report, not crash.
        if "/api/cathedral" not in root and path.startswith("/v1/"):
            try:
                result = read(root + "/api/cathedral" + path)
                result["fallback_url"] = result["url"]
                result["primary_error"] = str(exc)
                return result
            except Exception as fallback_exc:  # noqa: BLE001 - report both attempts.
                return {
                    "ok": False,
                    "status": getattr(fallback_exc, "code", getattr(exc, "code", None)),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "url": root + path,
                    "fallback_url": root + "/api/cathedral" + path,
                    "error": f"primary: {exc}; fallback: {fallback_exc}",
                    "data": {},
                }
        return {
            "ok": False,
            "status": getattr(exc, "code", None),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "url": root + path,
            "error": str(exc),
            "data": {},
        }


def percentile(values: list[float], index: int) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(max(index, 0), len(values) - 1)]


def load_coldkey_map(path: str | None) -> dict[str, str]:
    """Load hotkey->coldkey JSON for local-only dashboard grouping.

    Accepted shapes:
      {"5Hotkey": "5Coldkey"}
      {"hotkey_to_coldkey": {"5Hotkey": "5Coldkey"}}
      {"items": [{"hotkey": "...", "coldkey": "..."}]}
      [{"hotkey": "...", "coldkey": "..."}]
    """
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        if isinstance(payload.get("hotkey_to_coldkey"), dict):
            payload = payload["hotkey_to_coldkey"]
        elif isinstance(payload.get("items"), list):
            payload = payload["items"]
    if isinstance(payload, dict):
        return {str(k): str(v) for k, v in payload.items() if k and v}
    if isinstance(payload, list):
        out: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            hotkey = item.get("hotkey") or item.get("miner_hotkey")
            coldkey = item.get("coldkey") or item.get("cold_key") or item.get("owner")
            if hotkey and coldkey:
                out[str(hotkey)] = str(coldkey)
        return out
    raise ValueError("coldkey map must be a dict or list")


def derive(snapshot: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    active = snapshot.get("active") or {}
    weights = snapshot.get("weights") or {}
    recent = snapshot.get("recent") or {}
    top = snapshot.get("top") or {}
    active_items = active.get("items") or []
    recent_items = recent.get("items") or []
    weight_items = weights.get("weights") or []
    v6_rows = [r for r in recent_items if r.get("eval_output_schema_version") == 6]
    v5_rows = [r for r in recent_items if r.get("eval_output_schema_version") == 5]

    for row in recent_items:
        ran_at = parse_iso(row.get("ran_at"))
        row["age_seconds"] = round((now - ran_at).total_seconds(), 3) if ran_at else None

    times = [parse_iso(r.get("ran_at")) for r in recent_items]
    times = [t for t in times if t is not None]
    latest = max(times) if times else None
    oldest = min(times) if times else None
    weight_values = [float(w.get("weight") or 0.0) for w in weight_items]
    weight_unique = sorted(set(round(v, 9) for v in weight_values))
    weight_total = sum(weight_values)
    top_weight_row = None
    if weight_items:
        top_weight_row = max(weight_items, key=lambda w: float(w.get("weight") or 0.0))
    top_miners = top.get("miners") or []
    distinct_solves = [float(m.get("distinct_solves") or 0.0) for m in top_miners]
    coldkey_map = snapshot.get("coldkey_map") or {}
    recent_hotkeys = {str(r.get("miner_hotkey")) for r in v6_rows if r.get("miner_hotkey")}
    mapped_recent_hotkeys = sum(1 for hk in recent_hotkeys if hk in coldkey_map)
    meta = weights.get("policy_metadata") or {}
    perminer_meta = meta.get("perminer") or {}
    burn_snapshot = weights.get("burn_snapshot") or {}
    latest_pm_like = [
        r for r in v6_rows
        if str(r.get("task_id_public") or r.get("challenge_id") or "").startswith("pm-")
        or str(r.get("id") or "").startswith("pm-")
    ]

    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "active_by_tier": dict(collections.Counter(str(i.get("tier")) for i in active_items)),
        "active_shapes": [
            f"t{i.get('tier')}:{i.get('num_vars')}x{i.get('num_clauses')}"
            for i in active_items
        ],
        "policy_reason": weights.get("policy_reason"),
        "weights_generated_at": weights.get("generated_at"),
        "weights_expires_at": weights.get("expires_at"),
        "weights_count": len(weight_items),
        "miner_count_in_vector": meta.get("miner_count"),
        "nonzero_weights": sum(1 for v in weight_values if v > 0),
        "unique_weight_count": len(weight_unique),
        "weight_min": min(weight_values) if weight_values else None,
        "weight_max": max(weight_values) if weight_values else None,
        "weight_median": statistics.median(weight_values) if weight_values else None,
        "weight_total": round(weight_total, 12),
        "weight_top_hotkey": (top_weight_row or {}).get("miner_hotkey"),
        "weight_top_share": (
            float((top_weight_row or {}).get("weight") or 0.0) / weight_total
            if weight_total > 0 else None
        ),
        "signature_present": bool(weights.get("signature")),
        "policy_hash": weights.get("policy_hash"),
        "key_id": weights.get("key_id"),
        "burn_uid": burn_snapshot.get("burn_uid"),
        "burn_percentage": burn_snapshot.get("forced_burn_percentage"),
        "requested_mode": meta.get("requested_mode"),
        "effective_mode": meta.get("effective_mode"),
        "score_source": meta.get("score_source"),
        "tier_weights": meta.get("tier_weights") or {},
        "proportional_ledger_empty": meta.get("proportional_ledger_empty"),
        "perminer": {
            "enabled": bool(perminer_meta.get("enabled") or meta.get("perminer_enabled") or meta.get("perminer_scoring_mode")),
            "shadow": bool(perminer_meta.get("shadow") or meta.get("perminer_shadow")),
            "live_requested": bool(perminer_meta.get("live_requested") or meta.get("perminer_live_requested")),
            "has_scores": bool(perminer_meta.get("has_scores") or meta.get("perminer_has_scores")),
            "epoch": perminer_meta.get("epoch") or meta.get("perminer_epoch"),
            "mode": meta.get("perminer_scoring_mode"),
            "bonus_multiplier": meta.get("perminer_bonus_multiplier"),
            "history_floor": meta.get("perminer_history_floor"),
        },
        "validator_coldkey_map_loaded": meta.get("coldkey_map_loaded"),
        "recent_rows_sampled": len(recent_items),
        "recent_v6_solves_sampled": len(v6_rows),
        "recent_v5compat_rows_sampled": len(v5_rows),
        "recent_window_seconds_in_sample": (
            round((latest - oldest).total_seconds(), 3) if latest and oldest else None
        ),
        "latest_solve_age_seconds": (
            round((now - latest).total_seconds(), 3) if latest else None
        ),
        "recent_v6_by_tier": dict(collections.Counter(str(r.get("difficulty_tier")) for r in v6_rows)),
        "recent_v6_distinct_task_public_ids": len({r.get("task_id_public") for r in v6_rows}),
        "recent_v6_distinct_miners": len({r.get("miner_hotkey") for r in v6_rows}),
        "recent_pm_like_rows": len(latest_pm_like),
        "latest_receipt_data_time": latest.isoformat(timespec="milliseconds").replace("+00:00", "Z") if latest else None,
        "coldkey_map_status": "loaded" if coldkey_map else "identity_fallback",
        "coldkey_map_entries": len(coldkey_map),
        "recent_v6_mapped_hotkeys": mapped_recent_hotkeys,
        "recent_v6_unmapped_hotkeys": max(len(recent_hotkeys) - mapped_recent_hotkeys, 0),
        "recent_v6_distinct_coldkeys": len(
            {coldkey_map.get(hk, hk) for hk in recent_hotkeys}
        ),
        "recent_v6_max_solve_rank": max(
            [int(r.get("solve_rank") or 0) for r in v6_rows],
            default=0,
        ),
        "top_endpoint_miners_returned": len(top_miners),
        "top_distinct_solves_min": min(distinct_solves) if distinct_solves else None,
        "top_distinct_solves_max": max(distinct_solves) if distinct_solves else None,
        "top_distinct_solves_spread": (
            max(distinct_solves) - min(distinct_solves) if distinct_solves else None
        ),
        "top_distinct_solves_p10": percentile(distinct_solves, max(int(len(distinct_solves) * 0.10) - 1, 0)),
        "top_distinct_solves_p90": percentile(distinct_solves, max(int(len(distinct_solves) * 0.90) - 1, 0)),
    }


class Dashboard:
    def __init__(self, base_url: str, timeout: float, coldkey_map: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.coldkey_map = coldkey_map or {}
        self._snapshot_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: dict[str, tuple[float, str, Any]] = {}
        self._endpoint_obs: dict[str, dict[str, Any]] = {}

    def _endpoint_plan(self, recent_limit: int, fast: bool = False) -> dict[str, tuple[str, float, float]]:
        """name -> (path, max_cache_age_seconds, timeout_seconds)."""
        plan = {
            "health": ("/health", 10.0, min(self.timeout, 5.0)),
            "active": ("/v1/synthetic-boolean/active-challenges", 4.0, min(self.timeout, 8.0)),
            "weights": ("/v1/validator/weights/next", 30.0, min(self.timeout, 15.0)),
            "recent": (f"/v1/leaderboard/recent?limit={int(recent_limit)}", 2.0, min(self.timeout, 12.0)),
            "top": ("/v1/leaderboard/top?window=24h", 30.0, min(self.timeout, 12.0)),
        }
        if fast:
            plan["health"] = ("/health", 10.0, min(self.timeout, 2.0))
            plan["active"] = ("/v1/synthetic-boolean/active-challenges", 4.0, min(self.timeout, 3.0))
            plan["recent"] = (f"/v1/leaderboard/recent?limit={int(recent_limit)}", 2.0, min(self.timeout, 4.0))
            plan.pop("weights", None)
            plan.pop("top", None)
        return plan

    @staticmethod
    def _cache_key(name: str, path: str) -> str:
        return f"{name}:{path}"

    def snapshot(self, recent_limit: int, fast: bool = False) -> dict[str, Any]:
        plan = self._endpoint_plan(recent_limit, fast=fast)
        full_plan = self._endpoint_plan(recent_limit, fast=False)
        errors: dict[str, str] = {}
        observations: dict[str, dict[str, Any]] = {}

        # Avoid overlapping full polls stampeding the public API. Fast first
        # paint bypasses the lock so an old full poll cannot starve the page.
        acquired = True if fast else self._snapshot_lock.acquire(blocking=False)
        if acquired:
            try:
                now_mono = time.monotonic()
                with self._cache_lock:
                    due = {
                        name: (path, timeout)
                        for name, (path, max_age, timeout) in plan.items()
                        if self._cache_key(name, path) not in self._cache
                        or now_mono - self._cache[self._cache_key(name, path)][0] >= max_age
                    }

                if due:
                    with ThreadPoolExecutor(max_workers=len(due)) as executor:
                        futures = {
                            executor.submit(fetch_endpoint, self.base_url, path, timeout): name
                            for name, (path, timeout) in due.items()
                        }
                        for future in as_completed(futures):
                            name = futures[future]
                            result = future.result()
                            observations[name] = {
                                "name": name,
                                "path": due[name][0],
                                "timeout_seconds": due[name][1],
                                "ok": bool(result.get("ok")),
                                "status": result.get("status"),
                                "elapsed_seconds": result.get("elapsed_seconds"),
                                "url": result.get("url"),
                                "fallback_url": result.get("fallback_url"),
                                "primary_error": result.get("primary_error"),
                                "error": result.get("error"),
                                "fetched_at": utc_now().isoformat(timespec="seconds"),
                            }
                            with self._cache_lock:
                                self._endpoint_obs[name] = dict(observations[name])
                            if not result.get("ok"):
                                errors[name] = str(result.get("error") or "endpoint fetch failed")
                                continue
                            value = result.get("data", {})
                            with self._cache_lock:
                                self._cache[self._cache_key(name, due[name][0])] = (
                                    time.monotonic(),
                                    utc_now().isoformat(timespec="seconds"),
                                    value,
                                )
            finally:
                if not fast:
                    self._snapshot_lock.release()
        else:
            errors["refresh"] = "previous snapshot still refreshing; showing cached data"

        with self._cache_lock:
            data = {
                name: self._cache.get(self._cache_key(name, path), (0.0, "", {}))[2]
                for name, (path, _max_age, _timeout) in plan.items()
            }
            if fast:
                for name in ("weights", "top"):
                    path = full_plan[name][0]
                    data[name] = self._cache.get(self._cache_key(name, path), (0.0, "", {}))[2]
            cache_ages = {
                name: round(time.monotonic() - self._cache[self._cache_key(name, path)][0], 3)
                for name, (path, _max_age, _timeout) in full_plan.items()
                if self._cache_key(name, path) in self._cache
            }
            fetch_times = {
                name: self._cache[self._cache_key(name, path)][1]
                for name, (path, _max_age, _timeout) in full_plan.items()
                if self._cache_key(name, path) in self._cache
            }
            endpoint_status = {}
            now_mono = time.monotonic()
            for name, (path, max_age, timeout) in full_plan.items():
                key = self._cache_key(name, path)
                cached = self._cache.get(key)
                obs = observations.get(name) or self._endpoint_obs.get(name, {})
                age = round(now_mono - cached[0], 3) if cached else None
                cached_ok = cached is not None
                ok = bool(obs.get("ok")) if obs else cached_ok
                endpoint_status[name] = {
                    "name": name,
                    "path": path,
                    "ok": ok,
                    "status": obs.get("status"),
                    "elapsed_seconds": obs.get("elapsed_seconds"),
                    "timeout_seconds": timeout,
                    "url": obs.get("url", ""),
                    "fallback_url": obs.get("fallback_url", ""),
                    "primary_error": obs.get("primary_error", ""),
                    "cache_age_seconds": age,
                    "max_cache_age_seconds": max_age,
                    "stale": bool(age is not None and age > max_age * 2),
                    "last_success_at": cached[1] if cached else "",
                    "last_attempt_at": obs.get("fetched_at", ""),
                    "error": obs.get("error") or errors.get(name, ""),
                }
        data["errors"] = errors
        data["cache_ages_seconds"] = cache_ages
        data["fetch_times"] = fetch_times
        data["endpoint_status"] = endpoint_status
        data["coldkey_map"] = self.coldkey_map
        data["derived"] = derive(data)
        return data


def make_handler(dashboard: Dashboard, recent_limit: int):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

        def send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def send_html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_GET(self) -> None:  # noqa: N802 - http.server API.
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_html(INDEX_HTML)
                return
            if parsed.path == "/snapshot":
                query = urllib.parse.parse_qs(parsed.query)
                limit = recent_limit
                try:
                    limit = int(query.get("limit", [recent_limit])[0])
                except (TypeError, ValueError):
                    pass
                limit = max(20, min(limit, 1000))
                fast = query.get("fast", ["0"])[0] in {"1", "true", "yes"}
                self.send_json(dashboard.snapshot(limit, fast=fast))
                return
            if parsed.path == "/health":
                self.send_json({"ok": True, "base_url": dashboard.base_url})
                return
            self.send_json({"error": "not_found"}, status=404)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local Cathedral live stream table.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--recent-limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--coldkey-map",
        default=os.environ.get("CATHEDRAL_COLDKEY_MAP_PATH", ""),
        help="Optional local JSON hotkey->coldkey map for coldkey rank distribution.",
    )
    args = parser.parse_args()

    coldkey_map = load_coldkey_map(args.coldkey_map) if args.coldkey_map else {}
    dashboard = Dashboard(args.base_url, args.timeout, coldkey_map=coldkey_map)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(dashboard, args.recent_limit),
    )
    print(f"Cathedral live table: http://{args.host}:{args.port}")
    print(f"Polling: {args.base_url}")
    print(f"Coldkey map entries: {len(coldkey_map)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
