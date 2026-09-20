import { useEffect, useRef, useState } from "react";
import axios from "axios";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Activity, Zap, TrendingUp, TrendingDown, Play, Square, RefreshCw, Radio, Cpu, Wallet, Timer, Target, Send, Flame, Pause, Download, Gauge, Trophy } from "lucide-react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ReferenceLine, ResponsiveContainer } from "recharts";
import "@/App.css";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const API = `${BACKEND_URL}/api`;

const fmtINR = (v) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : `₹${Number(v).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
const fmtNum = (v, d=4) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d});
const fmtPct = (v) => (v === null || v === undefined) ? "—" : `${Number(v).toFixed(3)}%`;

function Sparkline({ points = [], color = "#00F0FF", width = 96, height = 28 }) {
  if (!points.length) return <div style={{width, height}} className="opacity-30" />;
  const min = Math.min(...points), max = Math.max(...points);
  const range = max - min || 1;
  const step = width / Math.max(1, points.length - 1);
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${(i*step).toFixed(1)} ${(height - ((p - min)/range)*height).toFixed(1)}`).join(" ");
  return (
    <svg width={width} height={height} className="overflow-visible">
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

function StatCard({ label, value, sub, accent = "cyan", testId, icon }) {
  const colorMap = {
    cyan: "text-cyan-400", green: "text-emerald-400", red: "text-rose-400",
    amber: "text-amber-400", slate: "text-slate-100", purple: "text-purple-400",
  };
  return (
    <div data-testid={testId} className="rounded-lg border border-[#1E2638] bg-[#121721] p-4 sm:p-5 transition-colors duration-150 hover:bg-[#182030]">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] uppercase font-bold tracking-widest text-slate-400">{label}</span>
        {icon}
      </div>
      <div className={`text-2xl sm:text-3xl font-extrabold tracking-tight font-mono ${colorMap[accent]}`}>{value}</div>
      {sub && <div className="mt-1 text-xs font-mono text-slate-500">{sub}</div>}
    </div>
  );
}
const heatColor = (r) => {
  const a = Math.min(Math.abs(r || 0), 1.2);
  if (a < 0.05) return "#1A2030";
  const alpha = 0.18 + Math.min(a, 1) * 0.82;
  return r >= 0 ? `rgba(16,185,129,${alpha.toFixed(2)})` : `rgba(239,68,68,${alpha.toFixed(2)})`;
};

function HeatBar({ heat = [], coin }) {
  const cells = heat.length ? heat : Array(60).fill(0);
  return (
    <div data-testid={`spread-heat-bar-${coin}`} className="mt-2 grid gap-px" style={{gridTemplateColumns: `repeat(${cells.length}, minmax(0, 1fr))`}} title="60s heat: |spread| vs hurdle, oldest → newest">
      {cells.map((r, i) => (
        <div key={i} className={`h-2 rounded-[1px] ${Math.abs(r) >= 1 ? "shadow-[0_0_4px_rgba(255,255,255,0.35)]" : ""}`} style={{backgroundColor: heatColor(r)}} />
      ))}
    </div>
  );
}

function EquityCurveChart({ points = [], start }) {
  const data = points.map((p) => ({ t: p.ts * 1000, equity: Number(p.equity) }));
  const last = data.length ? data[data.length - 1].equity : start;
  const up = (last ?? 0) >= (start ?? 0);
  const color = up ? "#10B981" : "#EF4444";
  const vals = data.map((d) => d.equity).concat([start || 0]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const pad = Math.max((max - min) * 0.15, (start || 4000) * 0.0025);
  const fmtT = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <div data-testid="equity-curve-chart-card" className="rounded-lg border border-[#1E2638] bg-[#121721] p-4 sm:p-5 min-w-0 overflow-hidden">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="font-semibold text-slate-200" style={{fontFamily:"Outfit, sans-serif"}}>Equity Curve</h3>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">5s samples · baseline ₹{(start || 0).toLocaleString("en-IN")}</div>
        </div>
        <div data-testid="equity-curve-last-value" className={`font-mono text-lg font-bold ${up ? "text-emerald-400" : "text-rose-400"}`}>{fmtINR(last)}</div>
      </div>
      <div className="h-48 sm:h-56">
        {data.length < 2 ? (
          <div className="h-full grid place-items-center text-slate-500 text-xs font-mono">Collecting equity samples…</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={fmtT} tick={{ fill: "#64748B", fontSize: 10, fontFamily: "monospace" }} axisLine={{ stroke: "#1E2638" }} tickLine={false} minTickGap={48} />
              <YAxis domain={[min - pad, max + pad]} tickFormatter={(v) => `₹${Math.round(v)}`} tick={{ fill: "#64748B", fontSize: 10, fontFamily: "monospace" }} axisLine={false} tickLine={false} width={64} />
              <Tooltip contentStyle={{ background: "#0A0D12", border: "1px solid #1E2638", borderRadius: 6, fontSize: 11, fontFamily: "monospace" }} labelStyle={{ color: "#94A3B8" }} itemStyle={{ color }} labelFormatter={(t) => new Date(t).toLocaleTimeString()} formatter={(v) => [fmtINR(v), "Equity"]} />
              <ReferenceLine y={start} stroke="#475569" strokeDasharray="4 4" />
              <Area type="monotone" dataKey="equity" stroke={color} strokeWidth={2} fill="url(#eqFill)" isAnimationActive={false} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

function TelegramPanel({ tg, onTest, onSummary, result }) {
  const ok = tg?.enabled;
  return (
    <div data-testid="telegram-alerts-panel" className="mt-4 pt-4 border-t border-[#1E2638]">
      <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono inline-flex items-center gap-1.5"><Send className="h-3 w-3"/> Telegram Alerts</span>
        <div className="flex gap-2">
          <button data-testid="telegram-send-test-btn" onClick={onTest} disabled={!ok || result?.loading} className="px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest bg-sky-700 hover:bg-sky-600 text-white transition-colors duration-150 disabled:opacity-40">Send test alert</button>
          <button data-testid="telegram-send-summary-btn" onClick={onSummary} disabled={!ok || result?.loading} className="px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest bg-slate-800 hover:bg-slate-700 text-slate-100 transition-colors duration-150 disabled:opacity-40">Summary now</button>
        </div>
      </div>
      <div className="flex flex-wrap gap-2 text-[10px] font-mono">
        <span data-testid="telegram-status-badge" className={`px-2 py-0.5 rounded-full border uppercase tracking-wider ${ok ? "border-emerald-800 bg-emerald-950/40 text-emerald-300" : "border-rose-800 bg-rose-950/40 text-rose-300"}`}>{ok ? `CONFIGURED${tg.bot_username ? " · @" + tg.bot_username : ""}` : "NOT CONFIGURED"}</span>
        {ok && <span data-testid="telegram-sent-count-badge" className="px-2 py-0.5 rounded-full border border-slate-700 text-slate-300">sent {tg.sent_count} · failed {tg.fail_count}</span>}
        {ok && tg.last_sent_ts && <span className="px-2 py-0.5 rounded-full border border-slate-700 text-slate-400">last {new Date(tg.last_sent_ts*1000).toLocaleTimeString()}</span>}
      </div>
      {!ok && <div className="mt-2 text-[11px] text-slate-500 font-mono">Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in backend/.env and restart.</div>}
      {result && (
        <div data-testid="telegram-test-result" className={`mt-2 text-[11px] font-mono rounded border px-2 py-1.5 ${result.loading ? "border-slate-700 text-slate-400" : result.ok ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>
          {result.loading ? "Sending…" : result.ok ? `✓ ${result.msg}` : `✗ ${result.error}`}
        </div>
      )}
      {tg?.last_error && !result?.loading && <div data-testid="telegram-last-error" className="mt-1 text-[10px] font-mono text-rose-400/80 truncate">last error: {tg.last_error}</div>}
    </div>
  );
}

const f1 = (v, d = 1) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);

function HurdleTuner({ draftHurdle, sim }) {
  const d = sim?.draft, c = sim?.current;
  return (
    <div data-testid="hurdle-tuner-panel" className="mt-4 pt-4 border-t border-[#1E2638]">
      <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono inline-flex items-center gap-1.5"><Gauge className="h-3 w-3"/> Hurdle Tuner · last {d ? Math.round((d.covered_s || 0) / 60) : 0} min of real spreads</span>
        <span data-testid="hurdle-tuner-draft-badge" className="px-2 py-0.5 rounded-full border border-purple-800 bg-purple-950/30 text-purple-300 font-mono text-[10px]">DRAFT {f1(draftHurdle, 3)}%</span>
      </div>
      {d ? (
        <>
          <div className="grid grid-cols-2 gap-2 text-xs font-mono">
            <div className="rounded border border-[#1E2638] bg-[#0A0D12] px-3 py-2">
              <div className="text-[10px] uppercase tracking-widest text-slate-500">would fire @ draft</div>
              <div data-testid="hurdle-tuner-draft-count" className="text-xl font-bold text-purple-300">{d.total_episodes}<span className="text-slate-500 text-xs"> signals</span></div>
              <div className="text-[10px] text-slate-500">{d.total_seconds_above}s above hurdle</div>
            </div>
            <div className="rounded border border-[#1E2638] bg-[#0A0D12] px-3 py-2">
              <div className="text-[10px] uppercase tracking-widest text-slate-500">@ saved {f1(c?.hurdle_pct, 3)}%</div>
              <div data-testid="hurdle-tuner-current-count" className="text-xl font-bold text-slate-200">{c?.total_episodes ?? 0}<span className="text-slate-500 text-xs"> signals</span></div>
              <div className="text-[10px] text-slate-500">{c?.total_seconds_above ?? 0}s above hurdle</div>
            </div>
          </div>
          <div data-testid="hurdle-tuner-coin-row" className="mt-2 flex flex-wrap gap-1.5">
            {d.coins.map((x) => (
              <span key={x.coin} data-testid={`hurdle-tuner-coin-${x.coin}`} className={`font-mono text-[10px] px-1.5 py-0.5 rounded border ${x.episodes > 0 ? "border-purple-800 text-purple-300" : "border-[#1E2638] text-slate-500"}`}>{x.coin} {x.episodes}</span>
            ))}
          </div>
          <div className="mt-1 text-[10px] text-slate-500 font-mono">A signal = a contiguous run of seconds where |spread| ≥ hurdle. Lower the draft until signals appear, then check they still clear fees.</div>
        </>
      ) : <div className="text-[11px] text-slate-500 font-mono">Collecting spread history…</div>}
    </div>
  );
}

function ReplayPanel({ sessions, recorder, config, status, onRun, result, running }) {
  const [p, setP] = useState({ date: "", hurdle_pct: "", leverage: "", num_slots: "", timeout_seconds: "", stop_loss_pct: "", basis_filter: true });
  useEffect(() => { if (!p.date && sessions?.length) setP((x) => ({ ...x, date: sessions[0].date })); }, [sessions, p.date]);
  useEffect(() => {
    if (config && p.hurdle_pct === "") setP((x) => ({ ...x, hurdle_pct: String(config.hurdle_pct ?? ""), leverage: String(config.leverage ?? 1), num_slots: String(status?.num_slots ?? 10), timeout_seconds: String(config.timeout_seconds ?? 45), stop_loss_pct: String(config.stop_loss_pct ?? 0.35), basis_filter: !!config.basis_filter }));
    // eslint-disable-next-line
  }, [config?.hurdle_pct]);
  const num = (k, label, tid, step) => (
    <label className="block"><span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">{label}</span>
      <input data-testid={tid} type="number" step={step} value={p[k]} onChange={(e) => setP({ ...p, [k]: e.target.value })} className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 text-xs focus:outline-none focus:border-cyan-600" /></label>
  );
  const r = result;
  return (
    <section data-testid="session-replay-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
        <div>
          <h3 className="font-semibold text-slate-200 inline-flex items-center gap-2" style={{fontFamily:"Outfit, sans-serif"}}><RefreshCw className="h-4 w-4 text-purple-400"/> Session Replay</h3>
          <div data-testid="recorder-status-text" className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">recorder {recorder?.enabled ? "ON" : "OFF"} · {recorder?.lines_written?.toLocaleString?.() ?? 0} ticks this run · {sessions?.length ?? 0} day file(s) · keep {recorder?.retention_days ?? 7} d</div>
        </div>
        <button data-testid="replay-run-btn" onClick={() => onRun({ date: p.date, hurdle_pct: Number(p.hurdle_pct) || undefined, leverage: Number(p.leverage) || undefined, num_slots: Number(p.num_slots) || undefined, timeout_seconds: Number(p.timeout_seconds) || undefined, stop_loss_pct: Number(p.stop_loss_pct) || undefined, basis_filter: p.basis_filter })} disabled={running || !p.date} className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-purple-600 hover:bg-purple-500 text-white text-xs font-bold uppercase tracking-widest transition-colors duration-150 disabled:opacity-50">{running ? <><RefreshCw className="h-3.5 w-3.5 animate-spin"/> Replaying…</> : <><Play className="h-3.5 w-3.5"/> Replay day</>}</button>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3 text-xs">
        <label className="block"><span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Recorded day</span>
          <select data-testid="replay-date-select" value={p.date} onChange={(e) => setP({ ...p, date: e.target.value })} className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 text-xs">
            {(sessions || []).map((s) => <option key={s.date} value={s.date}>{s.date} · {s.size_mb} MB</option>)}
          </select></label>
        {num("hurdle_pct", "Hurdle %", "replay-hurdle-input", "0.005")}
        {num("leverage", "Leverage", "replay-leverage-input", "1")}
        {num("num_slots", "Slots", "replay-slots-input", "1")}
        {num("timeout_seconds", "Timeout s", "replay-timeout-input", "1")}
        {num("stop_loss_pct", "Stop %", "replay-stop-input", "0.01")}
        <label className="block"><span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Basis filter</span>
          <div className="mt-1 h-[30px] flex items-center"><input data-testid="replay-basis-checkbox" type="checkbox" checked={p.basis_filter} onChange={(e) => setP({ ...p, basis_filter: e.target.checked })} className="h-4 w-4 accent-purple-500" /></div></label>
      </div>
      {r?.error && <div data-testid="replay-error" className="mt-3 text-[11px] font-mono text-rose-300">✗ {r.error}</div>}
      {r && !r.error && (
        <div data-testid="replay-result" className="mt-4 grid lg:grid-cols-3 gap-4">
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              {[["Trades", r.trades, ""], ["Win rate", `${r.win_rate}%`, ""], ["Net PnL", fmtINR(r.net_pnl_inr), r.net_pnl_inr >= 0 ? "text-emerald-300" : "text-rose-300"], ["Return", `${r.return_pct}%`, r.return_pct >= 0 ? "text-emerald-300" : "text-rose-300"], ["Max DD", `${r.max_drawdown_pct}%`, "text-amber-300"], ["Span", `${Math.round(r.span_s / 60)} min`, ""]].map(([k, v, cls]) => (
                <div key={k} className="rounded border border-[#1E2638] bg-[#0A0D12] px-3 py-2"><div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">{k}</div><div data-testid={`replay-metric-${k.toLowerCase().replace(/\s/g, "-")}`} className={`font-mono text-lg font-bold ${cls || "text-slate-200"}`}>{v}</div></div>
              ))}
            </div>
            <div className="text-[10px] font-mono text-slate-500">{r.file} · {r.lines.toLocaleString()} ticks · hurdle {r.params.hurdle_pct}% · {r.params.leverage}x · {r.params.num_slots} slots · basis {r.params.basis_filter ? "on" : "off"}</div>
          </div>
          <div className="lg:col-span-2 min-w-0">
            <EquityCurveChart points={r.equity_curve} start={r.params.equity_inr} />
            {r.per_coin.length > 0 && (
              <div className="mt-3 overflow-x-auto rounded border border-[#1E2638]">
                <table data-testid="replay-per-coin-table" className="w-full text-xs font-mono">
                  <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638]"><tr><th className="text-left px-3 py-1.5">Coin</th><th className="text-right px-3 py-1.5">Trades</th><th className="text-right px-3 py-1.5">Wins</th><th className="text-right px-3 py-1.5">Net ₹</th><th className="text-right px-3 py-1.5">Timeouts</th><th className="text-right px-3 py-1.5">Avg hold</th></tr></thead>
                  <tbody>{r.per_coin.map((c) => <tr key={c.coin} className="border-b border-[#1E2638]/60"><td className="px-3 py-1.5 font-bold text-slate-200">{c.coin}</td><td className="px-3 py-1.5 text-right">{c.trades}</td><td className="px-3 py-1.5 text-right">{c.wins}</td><td className={`px-3 py-1.5 text-right font-bold ${c.net >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{c.net.toFixed(2)}</td><td className="px-3 py-1.5 text-right">{c.timeouts}</td><td className="px-3 py-1.5 text-right">{c.avg_hold_s}s</td></tr>)}</tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
      {!r && <div className="mt-3 text-[11px] font-mono text-slate-500">Ticks are recorded to data/ticks/YYYY-MM-DD.jsonl while the bot runs on real feeds. Pick a day, change hurdle/basis/leverage and replay to see what the engine would have done.</div>}
    </section>
  );
}

function SweepPanel({ sessions, onRun, result, running }) {
  const [date, setDate] = useState("");
  const [hurdles, setHurdles] = useState("0.05, 0.08, 0.10, 0.12, 0.15, 0.20");
  const [windows, setWindows] = useState("60, 180, 300, 600");
  useEffect(() => { if (!date && sessions?.length) setDate(sessions[0].date); }, [sessions, date]);
  const parse = (s) => s.split(",").map((x) => Number(x.trim())).filter((x) => x > 0);
  const r = result;
  const hs = r?.hurdles || [], ws = r?.basis_windows || [];
  const cell = (h, w) => r?.grid?.find((g) => g.hurdle_pct === h && g.basis_window_s === w);
  const maxAbs = Math.max(0.01, ...(r?.grid || []).map((g) => Math.abs(g.net_pnl_inr)));
  return (
    <section data-testid="replay-sweep-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
        <div>
          <h3 className="font-semibold text-slate-200 inline-flex items-center gap-2" style={{fontFamily:"Outfit, sans-serif"}}><Flame className="h-4 w-4 text-amber-400"/> Replay Sweep · hurdle × basis window</h3>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">net ₹ heat table over one recorded day — pick settings by evidence</div>
        </div>
        <button data-testid="sweep-run-btn" onClick={() => onRun({ date, hurdles: parse(hurdles), basis_windows: parse(windows).map(Math.round) })} disabled={running || !date} className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-amber-600 hover:bg-amber-500 text-white text-xs font-bold uppercase tracking-widest transition-colors duration-150 disabled:opacity-50">{running ? <><RefreshCw className="h-3.5 w-3.5 animate-spin"/> Sweeping…</> : <><Play className="h-3.5 w-3.5"/> Run sweep</>}</button>
      </div>
      <div className="grid md:grid-cols-3 gap-3 text-xs">
        <label className="block"><span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Recorded day</span>
          <select data-testid="sweep-date-select" value={date} onChange={(e) => setDate(e.target.value)} className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 text-xs">{(sessions || []).map((s) => <option key={s.date} value={s.date}>{s.date}</option>)}</select></label>
        <label className="block"><span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Hurdles % (comma)</span><input data-testid="sweep-hurdles-input" value={hurdles} onChange={(e) => setHurdles(e.target.value)} className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 text-xs" /></label>
        <label className="block"><span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Basis windows s (comma)</span><input data-testid="sweep-windows-input" value={windows} onChange={(e) => setWindows(e.target.value)} className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 text-xs" /></label>
      </div>
      {r?.error && <div data-testid="sweep-error" className="mt-3 text-[11px] font-mono text-rose-300">✗ {r.error}</div>}
      {r && !r.error && (
        <div className="mt-4">
          {r.best && <div data-testid="sweep-best" className="mb-3 text-xs font-mono text-emerald-300">★ Best: hurdle <b>{r.best.hurdle_pct}%</b> · window <b>{r.best.basis_window_s}s</b> → {fmtINR(r.best.net_pnl_inr)} over {r.best.trades} trades ({r.best.win_rate}% win, DD {r.best.max_drawdown_pct}%) · {r.lines.toLocaleString()} ticks</div>}
          <div className="overflow-x-auto rounded border border-[#1E2638]">
            <table data-testid="sweep-heat-table" className="w-full text-xs font-mono">
              <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638]"><tr><th className="text-left px-3 py-2">hurdle ↓ / window →</th>{ws.map((w) => <th key={w} className="text-right px-3 py-2">{w}s</th>)}</tr></thead>
              <tbody>{hs.map((h) => <tr key={h} className="border-b border-[#1E2638]/60"><td className="px-3 py-2 font-bold text-slate-200">{h}%</td>{ws.map((w) => { const g = cell(h, w); if (!g) return <td key={w}/>; const a = Math.abs(g.net_pnl_inr) / maxAbs; const bg = g.net_pnl_inr >= 0 ? `rgba(16,185,129,${0.12 + 0.6 * a})` : `rgba(239,68,68,${0.12 + 0.6 * a})`; const best = r.best && r.best.hurdle_pct === h && r.best.basis_window_s === w; return <td key={w} data-testid={`sweep-cell-${h}-${w}`} title={`${g.trades} trades · ${g.win_rate}% win · DD ${g.max_drawdown_pct}%`} className={`px-3 py-2 text-right ${best ? "ring-1 ring-inset ring-amber-400" : ""}`} style={{backgroundColor: bg}}><div className="font-bold text-slate-100">{g.net_pnl_inr >= 0 ? "+" : ""}{g.net_pnl_inr.toFixed(2)}</div><div className="text-[10px] text-slate-300">{g.trades} tr · {g.win_rate}%</div></td>; })}</tr>)}</tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

function ScreenerPanel({ result, onRun, onApply, running, applyResult, openCount }) {
  const [sel, setSel] = useState(null);
  const rows = result?.rows || [];
  const chosen = sel ?? (result?.recommended || []);
  const toggle = (c) => setSel(chosen.includes(c) ? chosen.filter((x) => x !== c) : [...chosen, c]);
  return (
    <section data-testid="coin-screener-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
        <div>
          <h3 className="font-semibold text-slate-200 inline-flex items-center gap-2" style={{fontFamily:"Outfit, sans-serif"}}><Target className="h-4 w-4 text-emerald-400"/> Coin Screener</h3>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">score = 0.6 × log-volume rank (CoinDCX futures 24h) + 0.4 × lag activity (p95 |divergence| vs hurdle from today's ticks){result ? ` · ${result.candidates} candidates · lag data for ${result.lag_data_coins}` : ""}</div>
        </div>
        <div className="flex gap-2">
          <button data-testid="screener-run-btn" onClick={onRun} disabled={running} className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold uppercase tracking-widest transition-colors duration-150 disabled:opacity-50">{running ? <RefreshCw className="h-3.5 w-3.5 animate-spin"/> : <Radio className="h-3.5 w-3.5"/>} Screen now</button>
          <button data-testid="screener-apply-btn" onClick={() => onApply(chosen)} disabled={!result || !chosen.length || openCount > 0} title={openCount > 0 ? "close open positions first" : ""} className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-cyan-500 hover:bg-cyan-400 text-slate-900 text-xs font-bold uppercase tracking-widest transition-colors duration-150 disabled:opacity-50">Apply universe ({chosen.length})</button>
        </div>
      </div>
      {applyResult && <div data-testid="screener-apply-result" className={`mb-3 text-[11px] font-mono ${applyResult.ok ? "text-emerald-300" : "text-rose-300"}`}>{applyResult.ok ? `✓ Universe now: ${applyResult.coins.join(", ")} (feeds restarted)` : `✗ ${applyResult.error}`}</div>}
      {result && (
        <div className="mb-3 flex flex-wrap gap-2 text-[10px] font-mono">
          {result.add?.length > 0 && <span data-testid="screener-add-badge" className="px-2 py-0.5 rounded-full border border-emerald-800 bg-emerald-950/30 text-emerald-300">+ {result.add.join(", ")}</span>}
          {result.drop?.length > 0 && <span data-testid="screener-drop-badge" className="px-2 py-0.5 rounded-full border border-rose-800 bg-rose-950/30 text-rose-300">− {result.drop.join(", ")}</span>}
          <span className="px-2 py-0.5 rounded-full border border-[#1E2638] text-slate-400">Binance list: {result.binance_source} · min vol ${(result.min_volume_usdt / 1e6).toFixed(0)}M · CoinSwitch listing assumed for majors</span>
        </div>
      )}
      {rows.length > 0 ? (
        <div className="overflow-x-auto rounded border border-[#1E2638] max-h-80 overflow-y-auto">
          <table data-testid="screener-table" className="w-full text-xs font-mono">
            <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638] sticky top-0 bg-[#121721]"><tr><th className="px-3 py-2 text-left">Pick</th><th className="px-3 py-2 text-left">#</th><th className="px-3 py-2 text-left">Coin</th><th className="px-3 py-2 text-right">24h vol $M</th><th className="px-3 py-2 text-right">lag p95 bps</th><th className="px-3 py-2 text-right">lag score</th><th className="px-3 py-2 text-right">vol score</th><th className="px-3 py-2 text-right">score</th></tr></thead>
            <tbody>{rows.slice(0, 30).map((x, i) => <tr key={x.coin} data-testid={`screener-row-${x.coin}`} className={`border-b border-[#1E2638]/60 ${chosen.includes(x.coin) ? "bg-emerald-950/20" : ""}`}><td className="px-3 py-1.5"><input data-testid={`screener-pick-${x.coin}`} type="checkbox" checked={chosen.includes(x.coin)} onChange={() => toggle(x.coin)} className="h-3.5 w-3.5 accent-emerald-500"/></td><td className="px-3 py-1.5 text-slate-500">{i + 1}</td><td className="px-3 py-1.5 font-bold text-slate-200">{x.coin}{x.tracked && <span className="ml-1 text-[9px] text-cyan-400">●</span>}</td><td className="px-3 py-1.5 text-right">{(x.volume_24h_usdt / 1e6).toFixed(0)}</td><td className="px-3 py-1.5 text-right text-purple-300">{x.lag_p95_dev_bps ?? "—"}</td><td className="px-3 py-1.5 text-right">{x.lag_score ?? "—"}</td><td className="px-3 py-1.5 text-right">{x.volume_score}</td><td className="px-3 py-1.5 text-right font-bold text-emerald-300">{x.score}</td></tr>)}</tbody>
          </table>
        </div>
      ) : <div className="text-[11px] font-mono text-slate-500">Click "Screen now" to rank the CoinDCX/Binance futures universe. ● = currently tracked. Applying a new universe restarts the feeds and requires no open positions.</div>}
    </section>
  );
}

const EX_ORDER = ["binance", "coinswitch", "coindcx"];

function WalletRow({ wallet }) {
  if (!wallet) return null;
  const ex = (wallet.exchange || "").toUpperCase();
  return (
    <div data-testid="account-wallet-row" className="mb-4 rounded border border-[#1E2638] bg-[#0A0D12] px-3 py-2 text-xs font-mono flex items-center gap-3 flex-wrap">
      <span className="text-[10px] uppercase tracking-widest text-slate-500">Exchange wallet · {ex}</span>
      {!wallet.keys && <span data-testid="account-wallet-nokeys" className="text-amber-300">API keys not configured for {ex}</span>}
      {wallet.error && <span data-testid="account-wallet-error" className="text-rose-300 truncate">✗ {wallet.error}</span>}
      {wallet.keys && !wallet.error && (wallet.balances || []).map((b) => (
        <span key={b.currency} data-testid={`account-wallet-${b.currency}`} className={`px-2 py-0.5 rounded border ${b.currency === wallet.margin_currency ? "border-emerald-800 bg-emerald-950/30 text-emerald-200" : "border-[#1E2638] text-slate-300"}`}>
          {b.currency} <b>{b.currency === "INR" ? fmtINR(b.balance) : Number(b.balance).toFixed(4)}</b>{b.locked ? <span className="text-slate-500"> · locked {b.currency === "INR" ? fmtINR(b.locked) : Number(b.locked).toFixed(4)}</span> : null}
        </span>
      ))}
      {wallet.keys && !wallet.error && !(wallet.balances || []).length && <span className="text-slate-500">empty</span>}
      <span className="ml-auto text-[10px] text-slate-600">refreshed {wallet.fetched_at ? `${Math.max(0, Math.round(Date.now() / 1000 - wallet.fetched_at))}s ago` : "—"}</span>
    </div>
  );
}

function WalletChip({ wallet }) {
  const main = wallet?.balances?.find((b) => b.currency === (wallet.margin_currency || "INR")) || wallet?.balances?.[0];
  const label = !wallet ? "…" : !wallet.keys ? "NO KEYS" : wallet.error ? "ERROR" : main ? (main.currency === "INR" ? fmtINR(main.balance) : `${Number(main.balance).toFixed(2)} ${main.currency}`) : "—";
  return (
    <span data-testid="wallet-balance-chip" title={wallet?.error || `${(wallet?.exchange || "").toUpperCase()} futures wallet (10 s refresh)`} className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border font-mono text-[10px] uppercase tracking-wider ${wallet?.error || (wallet && !wallet.keys) ? "border-amber-800 bg-amber-950/30 text-amber-300" : "border-emerald-800 bg-emerald-950/30 text-emerald-300"}`}>
      <Wallet className="h-3 w-3"/> {(wallet?.exchange || "").toUpperCase()} WALLET · {label}
    </span>
  );
}

function AccountPanel({ status, onApply, result, wallet }) {
  const [draft, setDraft] = useState({ equity_inr: "", num_slots: "", leverage: "" });
  useEffect(() => {
    if (status && draft.num_slots === "" && draft.leverage === "") {
      setDraft({ equity_inr: String(status.starting_capital_inr ?? ""), num_slots: String(status.num_slots ?? ""), leverage: String(status.leverage ?? 1) });
    }
    // eslint-disable-next-line
  }, [status?.num_slots, status?.leverage, status?.starting_capital_inr]);
  const field = (k, label, tid, step = "1", hint) => (
    <label className="block">
      <span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">{label}</span>
      <input data-testid={tid} type="number" step={step} min="1" value={draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
        className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 text-xs focus:outline-none focus:border-cyan-600 focus:ring-1 focus:ring-cyan-800" />
      {hint && <span className="text-[10px] text-slate-600 font-mono">{hint}</span>}
    </label>
  );
  const equityChanged = Number(draft.equity_inr) !== Number(status?.starting_capital_inr);
  const notional = (Number(status?.allocated_per_slot_inr) || 0) * (Number(draft.leverage) || 1);
  return (
    <div data-testid="account-controls-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
      <div className="flex items-center justify-between mb-4 gap-2 flex-wrap">
        <h3 className="font-semibold text-slate-200 inline-flex items-center gap-2" style={{fontFamily:"Outfit, sans-serif"}}><Wallet className="h-4 w-4 text-cyan-400"/> Account Controls</h3>
        <span data-testid="account-exposure-badge" className="px-2 py-0.5 rounded-full border border-purple-800 bg-purple-950/30 text-purple-300 font-mono text-[10px]">
          {fmtINR(status?.allocated_per_slot_inr)} margin × {draft.leverage || 1}x = {fmtINR(notional)} / slot
        </span>
      </div>
      <WalletRow wallet={wallet} />
      <div className="grid grid-cols-3 gap-3 text-xs">
        {field("equity_inr", "Total Equity ₹", "account-equity-input", "100", equityChanged ? "resets PnL & stats" : "current baseline")}
        {field("num_slots", "Open Positions (slots)", "account-slots-input", "1", `${status?.open_positions ?? 0} open now`)}
        {field("leverage", "Leverage ×", "account-leverage-input", "1", status?.mode === "LIVE" ? "pushed to exchange" : "paper: scales notional")}
      </div>
      <button data-testid="account-apply-btn" onClick={() => onApply({
        ...(equityChanged && draft.equity_inr ? { equity_inr: Number(draft.equity_inr) } : {}),
        ...(draft.num_slots ? { num_slots: Number(draft.num_slots) } : {}),
        ...(draft.leverage ? { leverage: Number(draft.leverage) } : {}),
      })} className="mt-4 w-full py-2 rounded-full bg-purple-600 hover:bg-purple-500 text-white text-xs font-bold uppercase tracking-widest transition-colors duration-150">Apply account settings</button>
      {result && <div data-testid="account-apply-result" className={`mt-2 text-[11px] font-mono rounded border px-2 py-1.5 ${result.ok ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>{result.ok ? `✓ ${result.msg}` : `✗ ${result.error}`}</div>}
      <div className="mt-2 text-[10px] text-slate-500 font-mono">Equity reset is blocked while positions are open. Leverage multiplies notional (PnL and fees); in LIVE it calls the exchange's update-leverage for idle symbols first.</div>
    </div>
  );
}

function SlotAnalytics({ items, onToggle, onDigest, digestResult }) {
  const traded = items.filter((r) => r.trades > 0).length;
  return (
    <section data-testid="slot-analytics-section">
      <div className="flex items-center justify-between gap-2 flex-wrap mb-3">
        <h2 className="text-lg sm:text-xl font-semibold tracking-tight text-slate-200" style={{fontFamily:"Outfit, sans-serif"}}>Slot Analytics · <span className="text-cyan-400">{traded} coins traded</span></h2>
        <button data-testid="telegram-send-digest-btn" onClick={onDigest} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-sky-700 hover:bg-sky-600 text-white text-[10px] font-bold uppercase tracking-widest transition-colors duration-150"><Send className="h-3 w-3"/> Send daily digest now</button>
      </div>
      {digestResult && <div data-testid="telegram-digest-result" className={`mb-2 text-[11px] font-mono ${digestResult.ok ? "text-emerald-300" : "text-rose-300"}`}>{digestResult.ok ? "✓ Digest delivered" : `✗ ${digestResult.error}`}</div>}
      <div className="rounded-lg border border-[#1E2638] bg-[#121721] overflow-x-auto">
        <table data-testid="slot-analytics-table" className="w-full text-xs font-mono">
          <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638]">
            <tr><th className="text-left px-3 py-2">Coin</th><th className="text-right px-3 py-2">Trades</th><th className="text-right px-3 py-2">Win %</th><th className="text-right px-3 py-2">Net ₹</th><th className="text-right px-3 py-2">Fees ₹</th><th className="text-right px-3 py-2">Avg hold</th><th className="text-right px-3 py-2">Tgt / T-O / SL</th><th className="text-right px-3 py-2">Basis %</th><th className="text-right px-3 py-2">Slot</th></tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.coin} data-testid={`slot-analytics-row-${r.coin}`} className="border-b border-[#1E2638]/60 hover:bg-[#182030]">
                <td className="px-3 py-2 font-bold text-slate-200">{r.coin}</td>
                <td className="px-3 py-2 text-right">{r.trades}</td>
                <td className="px-3 py-2 text-right text-cyan-300">{r.win_rate.toFixed(0)}%</td>
                <td className={`px-3 py-2 text-right font-bold ${r.net_pnl_inr >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{r.net_pnl_inr >= 0 ? "+" : ""}{r.net_pnl_inr.toFixed(2)}</td>
                <td className="px-3 py-2 text-right text-slate-500">{r.fees_inr.toFixed(2)}</td>
                <td className="px-3 py-2 text-right text-slate-400">{r.avg_hold_sec.toFixed(1)}s</td>
                <td className="px-3 py-2 text-right text-slate-400">{r.target_hits} / {r.timeouts} / {r.stops}</td>
                <td className="px-3 py-2 text-right text-purple-300">{r.basis_pct.toFixed(3)}</td>
                <td className="px-3 py-2 text-right">
                  <button data-testid={`slot-analytics-toggle-${r.coin}`} onClick={() => onToggle(r.coin, r.paused)} className={`px-2 py-1 rounded text-[10px] font-bold uppercase tracking-wider transition-colors duration-150 ${r.paused ? "bg-amber-700 hover:bg-amber-600 text-white" : "bg-slate-800 hover:bg-slate-700 text-slate-100"}`}>{r.paused ? "Resume" : "Bench"}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function BenchmarkPanel({ bench, verdict, onRun, running, duration, setDuration, fees, setFees }) {
  const res = bench?.result, part = bench?.partial;
  const view = running && part ? part : res;
  const rows = view ? EX_ORDER.filter((k) => view.exchanges?.[k]).map((k) => ({ key: k, ...view.exchanges[k] })) : [];
  const progress = running && part ? Math.min(100, (part.elapsed_s / part.duration_s) * 100) : 0;
  return (
    <section data-testid="latency-benchmark-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
        <div>
          <h3 className="font-semibold text-slate-200 inline-flex items-center gap-2" style={{fontFamily:"Outfit, sans-serif"}}><Activity className="h-4 w-4 text-cyan-400"/> Exchange Pair Benchmark</h3>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">TCP RTT · WS tick-to-receipt · book quality · divergence vs Binance · run from this server (EC2 = production numbers)</div>
        </div>
        <div className="flex items-center gap-2">
          <select data-testid="benchmark-duration-select" value={duration} onChange={(e) => setDuration(Number(e.target.value))} disabled={running} className="bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 text-xs font-mono text-slate-200">
            {[30, 60, 120, 300].map((s) => <option key={s} value={s}>{s}s</option>)}
          </select>
          <button data-testid="benchmark-run-btn" onClick={onRun} disabled={running} className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-cyan-500 hover:bg-cyan-400 text-slate-900 text-xs font-bold uppercase tracking-widest transition-colors duration-150 disabled:opacity-50">
            {running ? <><RefreshCw className="h-3.5 w-3.5 animate-spin"/> Running {Math.round(progress)}%</> : <><Play className="h-3.5 w-3.5"/> Run benchmark</>}
          </button>
        </div>
      </div>
      {running && <div className="h-1 w-full bg-[#1E2638] rounded mb-4 overflow-hidden"><div data-testid="benchmark-progress-bar" className="h-full bg-cyan-500 transition-[width] duration-500" style={{width: `${progress}%`}}/></div>}
      {bench?.error && <div data-testid="benchmark-error" className="text-[11px] font-mono text-rose-300 mb-3">✗ {bench.error}</div>}
      <div className="overflow-x-auto rounded border border-[#1E2638]">
        <table data-testid="benchmark-results-table" className="w-full text-xs font-mono">
          <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638]">
            <tr>
              <th className="text-left px-3 py-2">Exchange</th>
              <th className="text-right px-3 py-2">TCP p50/p99</th>
              <th className="text-right px-3 py-2">Tick→recv p50/p99</th>
              <th className="text-right px-3 py-2">msg/s</th>
              <th className="text-right px-3 py-2">max gap</th>
              <th className="text-right px-3 py-2">rc</th>
              <th className="text-right px-3 py-2">spread bps</th>
              <th className="text-right px-3 py-2">depth ±10bps</th>
              <th className="text-right px-3 py-2">div. p50/p99 bps</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} data-testid={`benchmark-row-${r.key}`} className="border-b border-[#1E2638]/60">
                <td className="px-3 py-2 text-slate-200 font-bold">{r.label}</td>
                <td className="px-3 py-2 text-right text-cyan-300">{f1(r.tcp_rtt_ms?.p50, 0)} / {f1(r.tcp_rtt_ms?.p99, 0)} ms</td>
                <td className="px-3 py-2 text-right text-amber-300">{f1(r.ws_tick_to_receipt_ms?.p50, 0)} / {f1(r.ws_tick_to_receipt_ms?.p99, 0)} ms</td>
                <td className="px-3 py-2 text-right">{f1(r.ws_msgs_per_s, 1)}</td>
                <td className="px-3 py-2 text-right">{f1(r.ws_max_gap_ms, 0)} ms</td>
                <td className={`px-3 py-2 text-right ${r.ws_reconnects ? "text-rose-300" : "text-slate-400"}`}>{r.ws_reconnects}</td>
                <td className="px-3 py-2 text-right">{f1(r.spread_bps_median, 2)}</td>
                <td className="px-3 py-2 text-right">{r.depth_10bps_usdt_median == null ? "—" : `$${Math.round(r.depth_10bps_usdt_median).toLocaleString("en-US")}`}</td>
                <td className="px-3 py-2 text-right text-purple-300">{r.divergence_bps ? `${f1(r.divergence_bps.p50, 2)} / ${f1(r.divergence_bps.p99, 2)}` : "—"}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={9} className="text-center py-6 text-slate-500">No benchmark yet — run one to compare Pair A (CoinSwitch) vs Pair B (CoinDCX).</td></tr>}
          </tbody>
        </table>
      </div>
      {res && <div className="mt-1 text-[10px] text-slate-500 font-mono">last run: {new Date(res.finished_at * 1000).toLocaleString()} · {res.duration_s}s · {res.symbols?.join(", ")} · host {res.host?.hostname}</div>}
      <VerdictCard verdict={verdict} fees={fees} setFees={setFees} />
    </section>
  );
}

function VerdictCard({ verdict, fees, setFees }) {
  if (!verdict) return null;
  const feeInput = (k, label, tid) => (
    <label className="block text-[10px] uppercase tracking-widest text-slate-500 font-mono">{label}
      <input data-testid={tid} type="number" step="0.005" value={fees[k]} onChange={(e) => setFees({...fees, [k]: e.target.value})} className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1 font-mono text-slate-100 text-xs focus:outline-none focus:border-cyan-600"/>
    </label>
  );
  return (
    <div data-testid="benchmark-verdict-card" className="mt-4 rounded-lg border border-[#1E2638] bg-[#0A0D12] p-4">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono inline-flex items-center gap-1.5"><Trophy className="h-3 w-3 text-amber-400"/> Verdict</div>
          <div data-testid="verdict-winner" className="text-xl font-bold text-emerald-300 mt-1" style={{fontFamily:"Outfit, sans-serif"}}>{verdict.winner_pair} · {verdict.winner_label}</div>
          <div data-testid="verdict-confidence" className="text-[11px] font-mono text-slate-400 mt-1">margin {verdict.margin ?? "—"} pts · confidence: <span className="text-amber-300">{verdict.confidence}</span>{!verdict.any_qualified && <span className="text-rose-300"> · WARNING: no pair passed the reliability gates</span>}</div>
        </div>
        <div className="grid grid-cols-2 gap-2 w-full sm:w-64">
          {feeInput("coinswitch", "CoinSwitch taker %", "verdict-fee-coinswitch-input")}
          {feeInput("coindcx", "CoinDCX taker %", "verdict-fee-coindcx-input")}
        </div>
      </div>
      <div className="mt-3 grid md:grid-cols-2 gap-3">
        {verdict.rows.map((r) => (
          <div key={r.exchange} data-testid={`verdict-row-${r.exchange}`} className={`rounded border p-3 text-xs font-mono ${r.exchange === verdict.winner ? "border-emerald-800 bg-emerald-950/20" : "border-[#1E2638]"}`}>
            <div className="flex items-center justify-between">
              <span className="font-bold text-slate-200">{r.pair} · {r.exchange}</span>
              <span data-testid={`verdict-score-${r.exchange}`} className="text-lg font-bold text-cyan-300">{r.score.total}<span className="text-slate-500 text-[10px]">/100</span></span>
            </div>
            <div className="mt-1 grid grid-cols-5 gap-1 text-[10px] text-slate-400">
              {["net_edge", "fees", "latency", "book", "stability"].map((k) => <div key={k}><div className="uppercase tracking-wider text-slate-600">{k.replace("_"," ")}</div><div className="text-slate-200">{r.score[k]}</div></div>)}
            </div>
            <dl className="mt-2 space-y-0.5 text-[11px]">
              <div className="flex justify-between"><dt className="text-slate-500">hurdle (2×fee+slip+extra)</dt><dd>{r.hurdle_pct}% = {r.hurdle_bps} bps</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">opportunities / min</dt><dd className="text-purple-300">{r.opportunities_per_min}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">mean excess over hurdle</dt><dd>{r.mean_excess_bps} bps</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">expected ₹ / hour (1 slot)</dt><dd className={r.expected_inr_per_hour > 0 ? "text-emerald-300" : "text-slate-300"}>₹{r.expected_inr_per_hour}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">est. order RTT</dt><dd>{r.exec_rtt_est_ms ?? "—"} ms {r.gate_exec ? "✓" : "✗"}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">feed gate</dt><dd>{r.gate_feed ? "✓ pass" : "✗ fail"}</dd></div>
            </dl>
            {r.flags.length > 0 && <div className="mt-2 text-[10px] text-amber-300">{r.flags.map((f) => <div key={f}>⚠ {f}</div>)}</div>}
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-slate-500 font-mono">Rules: docs/EXCHANGE_PAIR_DECISION.md · run on the EC2 box that will trade, at 2–3 different hours, 300 s each, before committing capital.</div>
    </div>
  );
}



const SOURCE_STYLE = {
  COINSWITCH_WS: "border-emerald-800 bg-emerald-950/40 text-emerald-300",
  COINDCX_WS: "border-emerald-800 bg-emerald-950/40 text-emerald-300",
  SIMULATOR: "border-amber-700 bg-amber-950/30 text-amber-300",
  REST_POLLER: "border-orange-700 bg-orange-950/30 text-orange-300",
  STALE: "border-rose-800 bg-rose-950/40 text-rose-300",
};

function FeedHealthStrip({ feed }) {
  if (!feed) return null;
  const lg = feed.laggard, ld = feed.leader;
  const src = feed.source || "STALE";
  const pill = (testId, label, cls) => (
    <span data-testid={testId} className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border font-mono text-[10px] uppercase tracking-wider ${cls}`}>{label}</span>
  );
  return (
    <section data-testid="laggard-feed-health-strip" className="rounded-lg border border-[#1E2638] bg-[#121721] px-4 py-3 flex flex-wrap items-center gap-3">
      <div className="flex items-center gap-2 mr-2">
        <Radio className={`h-3.5 w-3.5 ${src.endsWith("_WS") ? "text-emerald-400 animate-pulse" : "text-slate-500"}`} />
        <span className="text-xs font-semibold tracking-wide text-slate-300" style={{fontFamily:"Outfit, sans-serif"}}>Laggard Feed</span>
      </div>
      {pill("feed-source-badge", `SRC · ${src.replace("_", " ")}`, SOURCE_STYLE[src] || SOURCE_STYLE.STALE)}
      {lg && pill("feed-ws-connected-badge", lg.subscribed ? "WS SUBSCRIBED" : lg.connected ? "WS CONNECTING" : "WS DOWN",
        lg.subscribed ? SOURCE_STYLE.COINSWITCH_WS : SOURCE_STYLE.STALE)}
      {lg && pill("feed-wire-latency-badge", `WIRE · ${lg.wire_latency_ms ?? "—"} ms`, "border-cyan-800 bg-cyan-950/30 text-cyan-300")}
      {lg && pill("feed-msg-rate-badge", `${lg.msgs_per_s ?? 0} snap/s · ${lg.reconnects} rc`, "border-slate-700 bg-slate-900/40 text-slate-300")}
      {ld && pill("feed-leader-badge", `BINANCE ${ld.connected ? "WS UP" : "WS DOWN"} · ${ld.coins_with_quotes}/10`,
        ld.connected ? SOURCE_STYLE.COINSWITCH_WS : SOURCE_STYLE.STALE)}
      {feed.rest_fallback?.active && pill("feed-rest-fallback-badge", "REST FALLBACK ACTIVE", SOURCE_STYLE.REST_POLLER)}
      {feed.user_ws && feed.user_ws.active && pill("feed-user-ws-badge",
        `DCX FILLS · ${feed.user_ws.joined ? "WS LIVE" : feed.user_ws.connected ? "WS JOINING" : "WS DOWN"} · ${feed.user_ws.order_updates} upd${feed.user_ws.last_create_ms != null ? ` · create ${feed.user_ws.last_create_ms} ms` : ""}${feed.user_ws.last_fill_latency_ms != null ? ` · fill ${feed.user_ws.last_fill_source} ${feed.user_ws.last_fill_latency_ms} ms` : ""}${feed.user_ws.last_warm_ms != null ? ` · warm ${feed.user_ws.last_warm_ms} ms` : ""}`,
        feed.user_ws.joined ? "border-violet-800 bg-violet-950/40 text-violet-300" : SOURCE_STYLE.STALE)}
      {feed.simulate && pill("feed-simulator-badge", "SIMULATOR · set SIMULATE_MARKET=false for real feeds", SOURCE_STYLE.SIMULATOR)}
      {lg && (
        <div data-testid="feed-coin-age-row" className="flex flex-wrap gap-1.5 ml-auto">
          {lg.coins.map((c) => (
            <span key={c.coin} data-testid={`feed-coin-age-${c.coin}`} title={`${c.coin} last tick ${c.age_ms ?? "—"} ms ago`}
              className={`font-mono text-[10px] px-1.5 py-0.5 rounded border ${c.stale ? "border-rose-900 text-rose-400" : "border-[#1E2638] text-slate-400"}`}>
              {c.coin} {c.age_ms == null ? "—" : c.age_ms >= 1000 ? `${(c.age_ms/1000).toFixed(1)}s` : `${c.age_ms}ms`}
            </span>
          ))}
        </div>
      )}
      {lg?.last_error && <div data-testid="feed-last-error" className="w-full text-[10px] font-mono text-rose-400/80 truncate">last error: {lg.last_error}</div>}
    </section>
  );
}

const stateBadge = (state) => {
  if (state === "PAUSED") return "px-1.5 py-0.5 rounded text-[10px] font-mono uppercase tracking-wider border border-slate-600 bg-slate-800/60 text-slate-300";
  const map = {
    WAITING: "bg-slate-800 text-slate-400 border-slate-700",
    HURDLE_MET_LONG: "bg-emerald-950/50 text-emerald-300 border-emerald-800 animate-pulse",
    HURDLE_MET_SHORT: "bg-rose-950/50 text-rose-300 border-rose-800 animate-pulse",
    IN_TRADE: "bg-cyan-950/50 text-cyan-300 border-cyan-800",
    PHANTOM: "bg-fuchsia-950/50 text-fuchsia-300 border-fuchsia-800",
    THIN: "bg-orange-950/50 text-orange-300 border-orange-800",
    STALE_BOOK: "bg-slate-800 text-fuchsia-200 border-fuchsia-900",
  };
  return `inline-flex items-center px-2 py-0.5 rounded-full border font-mono text-[10px] uppercase tracking-wider ${map[state] || map.WAITING}`;
};

const LIQ_STYLE = {
  LIQUID: "border-emerald-800 bg-emerald-950/40 text-emerald-300",
  THIN: "border-orange-700 bg-orange-950/40 text-orange-300",
  PHANTOM: "border-fuchsia-800 bg-fuchsia-950/40 text-fuchsia-300",
  NO_DEPTH: "border-slate-700 bg-slate-900/40 text-slate-400",
};
const fmtCompact = (v) => (v === null || v === undefined) ? "—" : v >= 1e7 ? `₹${(v / 1e7).toFixed(1)}Cr` : v >= 1e5 ? `₹${(v / 1e5).toFixed(1)}L` : v >= 1e3 ? `₹${(v / 1e3).toFixed(1)}k` : `₹${Number(v).toFixed(0)}`;

function LiquidityRow({ liq, coin }) {
  if (!liq) return <div className="col-span-2 text-slate-600">book: no depth yet</div>;
  return (
    <div data-testid={`spread-liquidity-${coin}`} className="col-span-2 flex items-center gap-1.5 flex-wrap" title={`walk-the-book for ₹${liq.notional_inr}: VWAP slip ${liq.slippage_pct}% · fillable within limit ${fmtCompact(liq.fillable_inr)} · frozen ${liq.frozen_s}s · dynamic hurdle ${liq.dyn_hurdle_pct}%`}>
      <span data-testid={`spread-liq-state-${coin}`} className={`px-1.5 py-0.5 rounded border font-mono text-[9px] uppercase tracking-wider ${LIQ_STYLE[liq.state] || LIQ_STYLE.NO_DEPTH}`}>{liq.state.replace("_", " ")}</span>
      <span>slip <span className={`${liq.slippage_pct > 0.02 ? "text-orange-300" : "text-slate-300"}`}>{Number(liq.slippage_pct).toFixed(3)}%</span></span>
      <span>±10bp <span className="text-slate-300">{fmtCompact(liq.depth_inr)}</span></span>
      {liq.frozen_s >= 1 && <span className="text-fuchsia-300">frozen {liq.frozen_s}s</span>}
    </div>
  );
}

function ComparePanel({ data, onReset }) {
  const ex = data?.exchanges || {};
  const cs = ex.coinswitch, dx = ex.coindcx;
  const v = data?.verdict;
  const g = (m, ...p) => p.reduce((a, k) => (a && a[k] !== undefined ? a[k] : null), m);
  const rows = [
    ["Price-lag behind Binance (median / p90)", (m) => `${g(m, "response_lag_s", "median") ?? "—"}s / ${g(m, "response_lag_s", "p90") ?? "—"}s · n=${g(m, "response_lag_s", "n") ?? 0}`, "time to follow a ≥2 bp Binance move"],
    ["Mean |divergence| vs Binance", (m) => m.avg_abs_spread_pct != null ? `${Number(m.avg_abs_spread_pct).toFixed(4)}%` : "—", "higher = wider lag"],
    ["Lag window (median / p75)", (m) => `${g(m, "lag_window_s", "median") ?? "—"}s / ${g(m, "lag_window_s", "p75") ?? "—"}s · n=${g(m, "lag_window_s", "n") ?? 0}`, "|spread| ≥ hurdle until it decays"],
    ["Catch-up signal→target (median)", (m) => g(m, "catch_up_s", "median") != null ? `${g(m, "catch_up_s", "median")}s` : "—", "the exploitable window"],
    ["Laggard bid-ask spread (median / p90)", (m) => `${g(m, "book_spread_pct", "median") != null ? Number(g(m, "book_spread_pct", "median")).toFixed(4) + "%" : "—"} / ${g(m, "book_spread_pct", "p90") != null ? Number(g(m, "book_spread_pct", "p90")).toFixed(4) + "%" : "—"}`, "half paid on entry, half on exit"],
    ["Entry slippage @ slot (median / p90)", (m) => `${g(m, "slippage_pct", "median") ?? "—"}% / ${g(m, "slippage_pct", "p90") ?? "—"}%`, "walk-the-book VWAP vs top"],
    ["Depth within ±10 bps (median)", (m) => fmtCompact(g(m, "depth_10bps_inr", "median")), "phantom-liquidity buffer"],
    ["Slippage ladder ₹10k / ₹1L / ₹10L", (m) => ["10000", "100000", "1000000"].map((n) => { const s = g(m, "slippage_ladder", n, "median"); return s == null ? "—" : s >= 100 ? "∅" : `${Number(s).toFixed(3)}%`; }).join(" / "), "worst side, median"],
    ["Skips phantom / thin / edge · shrunk", (m) => `${g(m, "skips", "phantom")} / ${g(m, "skips", "thin")} / ${g(m, "skips", "edge")} · ${g(m, "skips", "shrunk")}`, "of signals"],
    ["Fee per side · exec latency", (m) => `${m.fee_pct_per_side}% · ${m.exec_latency_s}s`, "assumptions"],
    ["Signals → trades", (m) => `${m.signals} → ${m.trades}${m.open ? ` (+${m.open} open)` : ""}`, ""],
    ["Win rate · exits", (m) => `${m.win_rate ?? "—"}% · ${Object.entries(m.exit_reasons || {}).map(([k, n]) => `${k.split("_")[0].toLowerCase()} ${n}`).join(", ") || "—"}`, ""],
    ["Gross · fees", (m) => `${fmtINR(m.gross_inr)} · ${fmtINR(m.fees_inr)}`, ""],
    ["Net ₹ · per hour", (m) => `${fmtINR(m.net_inr)} · ${m.net_inr_per_hour >= 0 ? "+" : ""}${Number(m.net_inr_per_hour).toFixed(2)}/h`, "after fees + slippage"],
    ["Feed", (m) => `${m.feed?.subscribed ? "WS" : "down"} · ${m.feed?.msgs_per_s} msg/s · ${m.feed?.wire_latency_ms ?? "—"} ms`, ""],
  ];
  const runtime = data?.runtime_s || 0;
  return (
    <section data-testid="compare-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
        <div>
          <h3 className="font-semibold text-slate-200 inline-flex items-center gap-2" style={{fontFamily:"Outfit, sans-serif"}}><Radio className="h-4 w-4 text-violet-400"/> Head-to-head · CoinSwitch vs CoinDCX (shadow paper, live books)</h3>
          <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">same rules · each exchange's fee, latency and walk-the-book fills · running {Math.floor(runtime / 60)} min · primary {data?.primary || "—"}</div>
        </div>
        <button data-testid="compare-reset-btn" onClick={onReset} className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full border border-[#1E2638] hover:border-slate-600 text-slate-300 text-xs font-bold uppercase tracking-widest transition-colors duration-150"><RefreshCw className="h-3.5 w-3.5"/> Reset stats</button>
      </div>
      {(!cs || !dx) ? <div data-testid="compare-warming" className="text-sm text-slate-500 font-mono">Warming up both laggard feeds…</div> : (
        <>
          <div className="overflow-x-auto rounded border border-[#1E2638]">
            <table data-testid="compare-table" className="w-full text-xs font-mono">
              <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638]"><tr><th className="text-left px-3 py-2">metric</th><th className="text-right px-3 py-2 text-emerald-300">CoinSwitch</th><th className="text-right px-3 py-2 text-cyan-300">CoinDCX</th></tr></thead>
              <tbody>{rows.map(([label, fn, hint]) => (
                <tr key={label} className="border-b border-[#1E2638]/60"><td className="px-3 py-1.5 text-slate-300">{label}{hint && <span className="text-slate-600 ml-1">· {hint}</span>}</td><td className="px-3 py-1.5 text-right text-slate-100">{fn(cs)}</td><td className="px-3 py-1.5 text-right text-slate-100">{fn(dx)}</td></tr>
              ))}</tbody>
            </table>
          </div>
          {v && (
            <div data-testid="compare-verdict" className={`mt-3 rounded border p-3 text-xs font-mono space-y-1 ${v.winner === "coinswitch" ? "border-emerald-800 bg-emerald-950/20" : v.winner === "coindcx" ? "border-cyan-800 bg-cyan-950/20" : "border-[#1E2638]"}`}>
              <div className="text-[10px] uppercase tracking-widest text-slate-500">verdict · confidence {v.confidence}</div>
              {v.lines.map((l, i) => <div key={i} className={l.startsWith("→") ? "text-slate-100 font-bold" : "text-slate-300"}>{l}</div>)}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Dashboard() {
  const [status, setStatus] = useState(null);
  const [config, setConfig] = useState(null);
  const [spreads, setSpreads] = useState([]);
  const [open, setOpen] = useState([]);
  const [closed, setClosed] = useState([]);
  const [logs, setLogs] = useState([]);
  const [equity, setEquity] = useState([]);
  const [saving, setSaving] = useState(false);
  const [cfgDraft, setCfgDraft] = useState(null);
  const cfgDraftInit = useRef(false);
  const [liveCheck, setLiveCheck] = useState(null);
  const [telemetry, setTelemetry] = useState(null);
  const [compare, setCompare] = useState(null);
  const resetCompare = async () => { try { const r = await axios.post(`${API}/compare/reset`); setCompare(r.data); } catch (e) { console.error(e); } };

  const pull = async () => {
    try {
      const [s, sp, op, cl, lg, eq, cfg, tm, cmp] = await Promise.all([
        axios.get(`${API}/status`),
        axios.get(`${API}/spreads`),
        axios.get(`${API}/positions/open`),
        axios.get(`${API}/positions/closed?limit=50`),
        axios.get(`${API}/logs?limit=40`),
        axios.get(`${API}/equity-curve`),
        axios.get(`${API}/config`),
        axios.get(`${API}/telemetry`),
        axios.get(`${API}/compare`),
      ]);
      setStatus(s.data);
      setSpreads(sp.data.items || []);
      setOpen(op.data.items || []);
      setClosed(cl.data.items || []);
      setLogs(lg.data.items || []);
      setEquity(eq.data.items || []);
      setConfig(cfg.data);
      setTelemetry(tm.data);
      setCompare(cmp.data);
      if (!cfgDraftInit.current) { cfgDraftInit.current = true; setCfgDraft(cfg.data); }
    } catch (e) { console.error(e); }
  };

  useEffect(() => {
    pull();
    const id = setInterval(pull, 1500);
    return () => clearInterval(id);
    // eslint-disable-next-line
  }, []);

  const toggleMode = async () => {
    if (!status) return;
    await axios.post(`${API}/bot/mode`, { paper_trading: status.mode !== "PAPER" });
    pull();
  };
  const [wallet, setWallet] = useState(null);
  const [pairSwitching, setPairSwitching] = useState(false);
  const [pairMsg, setPairMsg] = useState(null);
  const pullWallet = async () => { try { const r = await axios.get(`${API}/wallet`); setWallet(r.data); } catch (e) { console.error(e); } };
  useEffect(() => { pullWallet(); const id = setInterval(pullWallet, 10000); return () => clearInterval(id); }, []);
  const togglePair = async () => {
    if (!status || pairSwitching) return;
    const next = status.laggard_exchange === "coindcx" ? "coinswitch" : "coindcx";
    setPairSwitching(true); setPairMsg(null);
    try {
      const r = await axios.post(`${API}/laggard`, { exchange: next });
      setPairMsg(r.data.warning ? { level: "warn", text: r.data.warning } : { level: "ok", text: `Pair switched to Binance → ${next.toUpperCase()}` });
    } catch (e) { setPairMsg({ level: "error", text: e.response?.data?.detail || e.message }); }
    setPairSwitching(false);
    pull(); pullWallet();
  };
  const startStop = async () => {
    await axios.post(`${API}/bot/${status?.running ? "stop" : "start"}`);
    pull();
  };
  const closePos = async (coin) => {
    await axios.post(`${API}/positions/${coin}/close`);
    pull();
  };
  const saveCfg = async () => {
    setSaving(true);
    await axios.put(`${API}/config`, {
      entry_taker_fee_pct: Number(cfgDraft.entry_taker_fee_pct),
      exit_taker_fee_pct: Number(cfgDraft.exit_taker_fee_pct),
      slippage_buffer_pct: Number(cfgDraft.slippage_buffer_pct),
      hurdle_extra_pct: Number(cfgDraft.hurdle_extra_pct),
      timeout_seconds: Number(cfgDraft.timeout_seconds),
      stop_loss_pct: Number(cfgDraft.stop_loss_pct),
      basis_filter: !!cfgDraft.basis_filter,
      max_slippage_pct: Number(cfgDraft.max_slippage_pct),
      phantom_frozen_s: Number(cfgDraft.phantom_frozen_s),
      min_order_inr: Number(cfgDraft.min_order_inr),
      max_entry_frozen_s: Number(cfgDraft.max_entry_frozen_s),
    });
    setSaving(false);
    pull();
  };
  const runLiveCheck = async () => {
    setLiveCheck({ loading: true });
    try {
      const r = await axios.post(`${API}/coinswitch/check`);
      setLiveCheck({ ok: true, data: r.data });
    } catch (e) {
      setLiveCheck({ ok: false, error: e.response?.data?.detail || e.message });
    }
  };
  const [tgResult, setTgResult] = useState(null);
  const sendTelegram = async (kind) => {
    setTgResult({ loading: true });
    try {
      const r = await axios.post(`${API}/telegram/${kind}`);
      setTgResult({ ok: true, msg: kind === "test" ? `Test alert delivered via @${r.data.bot}` : "Hourly summary delivered" });
    } catch (e) {
      setTgResult({ ok: false, error: e.response?.data?.detail || e.message });
    }
    pull();
  };
  const [coinFilter, setCoinFilter] = useState("");
  const [analytics, setAnalytics] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [recorder, setRecorder] = useState(null);
  const [replay, setReplay] = useState(null);
  const [replayRunning, setReplayRunning] = useState(false);
  useEffect(() => {
    const go = async () => { try { const r = await axios.get(`${API}/replay/sessions`); setSessions(r.data.items || []); setRecorder(r.data.recorder); } catch (e) {} };
    go(); const id = setInterval(go, 10000); return () => clearInterval(id);
  }, []);
  const runReplay = async (body) => {
    setReplayRunning(true);
    try { const r = await axios.post(`${API}/replay/run`, body); setReplay(r.data); }
    catch (e) { setReplay({ error: e.response?.data?.detail || e.message }); }
    setReplayRunning(false);
  };
  const [dcxCheck, setDcxCheck] = useState(null);
  const [dcxTest, setDcxTest] = useState({ coin: "DOGE", notional_usdt: 6, leverage: 2, confirm: false, loading: false, result: null });
  const runDcxTestOrder = async () => {
    setDcxTest((t) => ({ ...t, loading: true, result: null }));
    try {
      const r = await axios.post(`${API}/coindcx/test-order`, { coin: dcxTest.coin, notional_usdt: Number(dcxTest.notional_usdt), leverage: Number(dcxTest.leverage) || 1, confirm: dcxTest.confirm });
      setDcxTest((t) => ({ ...t, loading: false, result: r.data }));
    } catch (e) {
      const d = e.response?.data?.detail;
      setDcxTest((t) => ({ ...t, loading: false, result: { ok: false, error: typeof d === "object" ? d.message : (d || e.message), steps: typeof d === "object" ? d.steps : [] } }));
    }
    pull();
  };
  const [sweep, setSweep] = useState(null);
  const [sweepRunning, setSweepRunning] = useState(false);
  const runSweep = async (body) => {
    setSweepRunning(true);
    try { const r = await axios.post(`${API}/replay/sweep`, body); setSweep(r.data); }
    catch (e) { setSweep({ error: e.response?.data?.detail || e.message }); }
    setSweepRunning(false);
  };
  const [screenRes, setScreenRes] = useState(null);
  const [screening, setScreening] = useState(false);
  const [applyUniverse, setApplyUniverse] = useState(null);
  const runScreener = async () => {
    setScreening(true);
    try { const r = await axios.get(`${API}/screener?top_n=${status?.num_slots || 10}`); setScreenRes(r.data); setApplyUniverse(null); }
    catch (e) { setScreenRes({ rows: [], error: e.response?.data?.detail || e.message }); }
    setScreening(false);
  };
  const applyUniverseCoins = async (coins) => {
    try { const r = await axios.post(`${API}/universe`, { coins }); setApplyUniverse({ ok: true, coins: r.data.coins }); pull(); }
    catch (e) { setApplyUniverse({ ok: false, error: e.response?.data?.detail || e.message }); }
  };
  const runDcxCheck = async () => {
    setDcxCheck({ loading: true });
    try { const r = await axios.post(`${API}/coindcx/check`); setDcxCheck({ ok: true, data: r.data }); }
    catch (e) { setDcxCheck({ ok: false, error: e.response?.data?.detail || e.message }); }
  };
  const [accountResult, setAccountResult] = useState(null);
  const [digestResult, setDigestResult] = useState(null);
  useEffect(() => {
    const go = async () => { try { const r = await axios.get(`${API}/analytics/coins`); setAnalytics(r.data.items || []); } catch (e) {} };
    go(); const id = setInterval(go, 5000); return () => clearInterval(id);
  }, []);
  const applyAccount = async (body) => {
    try {
      const r = await axios.post(`${API}/account`, body);
      const ch = Object.entries(r.data.changes).map(([k, v]) => `${k}=${v}`).join(", ") || "no changes";
      setAccountResult({ ok: true, msg: `Applied: ${ch}${r.data.leverage_errors?.length ? ` · leverage errors: ${r.data.leverage_errors.length}` : ""}` });
    } catch (e) { setAccountResult({ ok: false, error: e.response?.data?.detail || e.message }); }
    pull();
  };
  const sendDigest = async () => {
    try { await axios.post(`${API}/telegram/digest`); setDigestResult({ ok: true }); }
    catch (e) { setDigestResult({ ok: false, error: e.response?.data?.detail || e.message }); }
  };
  const togglePause = async (coin, paused) => {
    await axios.post(`${API}/coins/${coin}/${paused ? "resume" : "pause"}`);
    pull();
  };
  const [hurdleSim, setHurdleSim] = useState(null);
  const draftHurdle = cfgDraft ? ["entry_taker_fee_pct", "exit_taker_fee_pct", "slippage_buffer_pct", "hurdle_extra_pct"].reduce((a, k) => a + (Number(cfgDraft[k]) || 0), 0) : null;
  useEffect(() => {
    if (!draftHurdle || draftHurdle <= 0) return;
    let alive = true;
    const go = async () => {
      try { const r = await axios.get(`${API}/hurdle/simulate?hurdle_pct=${draftHurdle}&window_s=600`); if (alive) setHurdleSim(r.data); } catch (e) {}
    };
    go();
    const id = setInterval(go, 3000);
    return () => { alive = false; clearInterval(id); };
  }, [draftHurdle]);
  const [bench, setBench] = useState(null);
  const [verdict, setVerdict] = useState(null);
  const [benchDuration, setBenchDuration] = useState(60);
  const [fees, setFees] = useState({ coinswitch: 0.05, coindcx: 0.075 });
  useEffect(() => {
    let alive = true;
    const go = async () => {
      try {
        const r = await axios.get(`${API}/benchmark/status`);
        if (!alive) return;
        setBench(r.data);
        if (r.data.result) {
          const v = await axios.get(`${API}/benchmark/verdict?coinswitch_fee_pct=${Number(fees.coinswitch) || 0.05}&coindcx_fee_pct=${Number(fees.coindcx) || 0.075}`);
          if (alive) setVerdict(v.data);
        }
      } catch (e) {}
    };
    go();
    const id = setInterval(go, 2000);
    return () => { alive = false; clearInterval(id); };
  }, [fees]);
  const runBenchmark = async () => {
    try { await axios.post(`${API}/benchmark/run`, { duration_s: benchDuration, symbols: ["BTC", "ETH", "SOL"] }); }
    catch (e) { setBench((b) => ({ ...(b || {}), error: e.response?.data?.detail || e.message })); }
  };

  const wr = status?.win_rate || 0;
  const pnlAccent = (status?.realized_pnl_inr || 0) >= 0 ? "green" : "red";

  return (
    <div className="min-h-screen bg-[#0A0D12] text-slate-100 font-manrope">
      {/* Top nav */}
      <header data-testid="top-nav-bar" className="backdrop-blur-xl bg-[#0A0D12]/90 sticky top-0 z-50 border-b border-[#1E2638] px-4 sm:px-6 py-3">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <div className="h-9 w-9 rounded-lg bg-gradient-to-br from-cyan-500 to-cyan-800 grid place-items-center shadow-[0_0_20px_rgba(0,240,255,0.25)]">
              <Zap className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-lg font-extrabold tracking-tight" style={{fontFamily: "Outfit, sans-serif"}}>QuantLag <span className="text-cyan-400">HFT</span></div>
              <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Binance → {status?.laggard_exchange === "coindcx" ? "CoinDCX Futures" : "CoinSwitch PRO"} · Lead-Lag Arb</div>
            </div>
          </div>
          <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
            <span data-testid="telemetry-ws-heartbeat-status" className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border font-mono text-[10px] uppercase tracking-wider ${status?.running ? "border-emerald-800 bg-emerald-950/40 text-emerald-300" : "border-rose-800 bg-rose-950/40 text-rose-300"}`}>
              <Radio className={`h-3 w-3 ${status?.running ? "animate-pulse" : ""}`}/> {status?.running ? "WS LIVE" : "STOPPED"}
            </span>
            <span data-testid="usdt-inr-fx-rate-badge" className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-purple-800 bg-purple-950/30 text-purple-300 font-mono text-[10px] uppercase tracking-wider">
              USDT/INR · {fmtNum(status?.fx_usdt_inr, 4)}
            </span>
            <span data-testid="leverage-slots-badge" className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-cyan-800 bg-cyan-950/30 text-cyan-300 font-mono text-[10px] uppercase tracking-wider">
              {status?.leverage || 1}x · {status?.num_slots || 10} slots
            </span>
            <WalletChip wallet={wallet} />
            <button data-testid="pair-toggle" onClick={togglePair} disabled={pairSwitching} title="Switch the traded pair (refused while positions are open)" className={`px-3 py-1.5 rounded-full border font-mono text-[10px] uppercase tracking-widest transition-colors duration-150 disabled:opacity-50 ${status?.laggard_exchange === "coindcx" ? "border-cyan-700 bg-cyan-950/30 text-cyan-300 hover:bg-cyan-950/60" : "border-violet-700 bg-violet-950/30 text-violet-300 hover:bg-violet-950/60"}`}>
              PAIR · BINANCE→{pairSwitching ? "…" : (status?.laggard_exchange || "coinswitch").toUpperCase()}
            </button>
            <button data-testid="paper-live-mode-toggle" onClick={toggleMode} className={`px-3 py-1.5 rounded-full border font-mono text-[10px] uppercase tracking-widest transition-colors duration-150 ${status?.mode === "PAPER" ? "border-amber-700 bg-amber-950/30 text-amber-300 hover:bg-amber-950/60" : "border-emerald-700 bg-emerald-950/30 text-emerald-300 hover:bg-emerald-950/60"}`}>
              MODE · {status?.mode || "…"}
            </button>
            <button data-testid="bot-start-stop-button" onClick={startStop} className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full text-xs font-bold tracking-wider transition-colors duration-150 ${status?.running ? "bg-rose-600 hover:bg-rose-500 text-white" : "bg-emerald-500 hover:bg-emerald-400 text-slate-900"}`}>
              {status?.running ? <><Square className="h-3.5 w-3.5"/> STOP</> : <><Play className="h-3.5 w-3.5"/> START</>}
            </button>
          </div>
        </div>
      </header>

      <main className="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
        {pairMsg && (
          <div data-testid="pair-switch-message" className={`rounded border px-3 py-2 text-xs font-mono flex items-center justify-between gap-3 ${pairMsg.level === "ok" ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : pairMsg.level === "warn" ? "border-amber-800 bg-amber-950/30 text-amber-300" : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>
            <span>{pairMsg.level === "ok" ? "✓" : "⚠"} {pairMsg.text}</span>
            <button data-testid="pair-switch-dismiss" onClick={() => setPairMsg(null)} className="text-slate-400 hover:text-slate-200">dismiss</button>
          </div>
        )}

        {/* Portfolio cards */}
        <section className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
          <StatCard testId="portfolio-equity-card" label="Total Equity (INR)" value={fmtINR(status?.equity_inr)} sub={`Start ₹${status?.starting_capital_inr?.toLocaleString?.() || "—"}`} accent="cyan" icon={<Wallet className="h-4 w-4 text-cyan-400/60"/>} />
          <StatCard testId="portfolio-free-margin-card" label="Free Margin" value={fmtINR(status?.free_margin_inr)} sub={`₹${status?.allocated_per_slot_inr?.toFixed?.(2) || "0"} / slot · ${status?.num_slots || 10} slots`} accent="slate" icon={<Cpu className="h-4 w-4 text-slate-400/60"/>} />
          <StatCard testId="portfolio-net-pnl-card" label="Net Realized PnL" value={`${(status?.realized_pnl_inr ?? 0) >= 0 ? "+" : ""}${fmtINR(status?.realized_pnl_inr).replace("₹","₹")}`} sub={`W ${status?.wins || 0} / L ${status?.losses || 0} · ${wr.toFixed(1)}% win`} accent={pnlAccent} icon={<TrendingUp className="h-4 w-4 text-emerald-400/60"/>} />
          <StatCard testId="portfolio-execution-latency-card" label="Exec Latency (avg)" value={`${(status?.avg_exec_latency_ms || 0).toFixed(0)} ms`} sub={`Hurdle gate · ${fmtPct(status?.hurdle_pct)}`} accent="amber" icon={<Timer className="h-4 w-4 text-amber-400/60"/>} />
        </section>

        <FeedHealthStrip feed={telemetry?.feed} />

        <AccountPanel status={status} onApply={applyAccount} result={accountResult} wallet={wallet} />

        {/* Spread monitor grid */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg sm:text-xl font-semibold tracking-tight text-slate-200" style={{fontFamily:"Outfit, sans-serif"}}>Real-time Spread Monitor · <span className="text-cyan-400">{spreads.length || 10} coins · {status?.num_slots || 10} slots</span></h2>
            <button onClick={pull} className="text-xs text-slate-400 hover:text-slate-200 inline-flex items-center gap-1 transition-colors duration-150"><RefreshCw className="h-3 w-3"/> refresh</button>
          </div>
          <div data-testid="realtime-spread-slot-grid" className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
            {spreads.map((sp) => {
              const positive = (sp.gross_spread_pct || 0) >= 0;
              return (
                <div key={sp.coin} data-testid={`spread-slot-card-${sp.coin}`} className="rounded-lg border border-[#1E2638] bg-[#121721] p-3 transition-colors duration-150 hover:bg-[#182030] hover:border-slate-700">
                  <div className="flex items-center justify-between gap-1 mb-1 flex-wrap">
                    <div className="font-bold text-sm tracking-wide">{sp.coin}<span className="text-slate-500 text-[10px] ml-1">/INR</span></div>
                    <div className="flex items-center gap-1 min-w-0">
                      <button data-testid={`spread-pause-toggle-${sp.coin}`} onClick={() => togglePause(sp.coin, sp.paused)} title={sp.paused ? "Resume slot" : "Pause slot (no new entries)"} className={`h-5 w-5 grid place-items-center rounded border transition-colors duration-150 ${sp.paused ? "border-amber-700 bg-amber-950/40 text-amber-300 hover:bg-amber-900/40" : "border-[#1E2638] text-slate-500 hover:text-slate-200 hover:border-slate-600"}`}>
                        {sp.paused ? <Play className="h-2.5 w-2.5"/> : <Pause className="h-2.5 w-2.5"/>}
                      </button>
                      <span data-testid={`spread-near-hurdle-hits-${sp.coin}`} title="seconds in last 60s where |spread| ≥ 80% of hurdle" className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded border font-mono text-[10px] ${(sp.near_hurdle_hits_60s || 0) > 0 ? "border-amber-700 bg-amber-950/40 text-amber-300" : "border-[#1E2638] text-slate-500"}`}><Flame className="h-2.5 w-2.5"/>{sp.near_hurdle_hits_60s ?? 0}</span>
                      <span className={stateBadge(sp.signal_state)}>{(sp.signal_state || "waiting").replace("_"," ")}</span>
                    </div>
                  </div>
                  <div className="flex items-baseline justify-between">
                    <div className={`text-lg font-mono font-bold ${positive ? "text-emerald-400" : "text-rose-400"}`}>{sp.gross_spread_pct >= 0 ? "+" : ""}{(sp.gross_spread_pct ?? 0).toFixed(3)}%</div>
                    <Sparkline points={sp.sparkline || []} color={positive ? "#10B981" : "#EF4444"} width={72} height={20} />
                  </div>
                  <HeatBar heat={sp.heat} coin={sp.coin} />
                  <div className="mt-1 grid grid-cols-2 gap-1 text-[10px] font-mono text-slate-500">
                    <div>BIN ask <span className="text-slate-300">{fmtNum(sp.binance_ask, 4)}</span></div>
                    <div>CSW bid <span className="text-slate-300">{fmtNum(sp.coinswitch_bid, 4)}</span></div>
                    <div className="col-span-2">peak 60s <span data-testid={`spread-peak-ratio-${sp.coin}`} className="text-slate-300">{Math.round((sp.peak_ratio_60s || 0) * 100)}%</span> of hurdle · basis <span data-testid={`spread-basis-${sp.coin}`} className="text-purple-300">{(sp.basis_pct ?? 0).toFixed(3)}%</span></div>
                    <LiquidityRow liq={sp.liquidity} coin={sp.coin} />
                  </div>
                </div>
              );
            })}
            {!spreads.length && <div className="col-span-full text-center py-8 text-slate-500 text-sm font-mono">Warming up market feed…</div>}
          </div>
        </section>

        {/* Open positions */}
        <section>
          <h2 className="text-lg sm:text-xl font-semibold tracking-tight text-slate-200 mb-3" style={{fontFamily:"Outfit, sans-serif"}}>Open Positions</h2>
          <div className="rounded-lg border border-[#1E2638] bg-[#121721] overflow-x-auto">
            <table data-testid="open-positions-table" className="w-full text-xs font-mono">
              <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638]">
                <tr>
                  <th className="text-left px-3 py-2">Coin</th>
                  <th className="text-left px-3 py-2">Dir</th>
                  <th className="text-right px-3 py-2">Size</th>
                  <th className="text-right px-3 py-2">Entry</th>
                  <th className="text-right px-3 py-2">Target</th>
                  <th className="text-right px-3 py-2">Stop</th>
                  <th className="text-right px-3 py-2">Hold</th>
                  <th className="text-right px-3 py-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {open.map((p) => {
                  const held = Math.floor((Date.now()/1000) - p.opened_at);
                  return (
                    <tr key={p.id} data-testid={`open-position-row-${p.coin}`} className="border-b border-[#1E2638]/60 hover:bg-[#182030]">
                      <td className="px-3 py-2 font-bold text-slate-200">{p.coin}</td>
                      <td className="px-3 py-2"><span className={`px-2 py-0.5 rounded border text-[10px] font-bold ${p.direction === "LONG" ? "border-emerald-700 text-emerald-300 bg-emerald-950/30" : "border-rose-700 text-rose-300 bg-rose-950/30"}`}>{p.direction}</span></td>
                      <td className="px-3 py-2 text-right">{fmtINR(p.size_inr)}</td>
                      <td className="px-3 py-2 text-right">{fmtNum(p.entry_price_inr, 6)}</td>
                      <td className="px-3 py-2 text-right text-cyan-300">{fmtNum(p.target_price_inr, 6)}</td>
                      <td className="px-3 py-2 text-right text-rose-300">{fmtNum(p.stop_price_inr, 6)}</td>
                      <td className="px-3 py-2 text-right text-slate-400">{held}s / {Math.floor(p.timeout_at - p.opened_at)}s</td>
                      <td className="px-3 py-2 text-right">
                        <button data-testid={`close-position-btn-${p.coin}`} onClick={() => closePos(p.coin)} className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-100 text-[10px] font-bold uppercase tracking-wider transition-colors duration-150">Close</button>
                      </td>
                    </tr>
                  );
                })}
                {!open.length && <tr><td colSpan={8} className="text-center py-6 text-slate-500">No open positions.</td></tr>}
              </tbody>
            </table>
          </div>
        </section>

        <SlotAnalytics items={analytics} onToggle={togglePause} onDigest={sendDigest} digestResult={digestResult} />

        {/* Closed trades + Hourly summary side by side */}
        <section className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6 min-w-0">
            <EquityCurveChart points={equity} start={status?.starting_capital_inr} />
            <div>
            <div className="flex items-center justify-between gap-2 flex-wrap mb-3">
              <h2 className="text-lg sm:text-xl font-semibold tracking-tight text-slate-200" style={{fontFamily:"Outfit, sans-serif"}}>Recent Closed Trades</h2>
              <div className="flex items-center gap-2">
                <select data-testid="closed-trades-coin-filter" value={coinFilter} onChange={(e) => setCoinFilter(e.target.value)} className="bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 text-xs font-mono text-slate-200">
                  <option value="">All coins</option>
                  {(config?.coins || []).map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <a data-testid="closed-trades-export-csv-btn" href={`${API}/positions/closed/export.csv${coinFilter ? `?coin=${coinFilter}` : ""}`} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-100 text-[10px] font-bold uppercase tracking-widest transition-colors duration-150"><Download className="h-3 w-3"/> Export CSV</a>
              </div>
            </div>
            <div className="rounded-lg border border-[#1E2638] bg-[#121721] overflow-x-auto max-h-[420px] overflow-y-auto">
              <table data-testid="closed-trades-history-table" className="w-full text-xs font-mono">
                <thead className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-[#1E2638] sticky top-0 bg-[#121721]">
                  <tr>
                    <th className="text-left px-3 py-2">Coin</th>
                    <th className="text-left px-3 py-2">Dir</th>
                    <th className="text-right px-3 py-2">Entry</th>
                    <th className="text-right px-3 py-2">Exit</th>
                    <th className="text-right px-3 py-2">Gross</th>
                    <th className="text-right px-3 py-2">Fees</th>
                    <th className="text-right px-3 py-2">Net</th>
                    <th className="text-left px-3 py-2">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {closed.filter((p) => !coinFilter || p.coin === coinFilter).map((p) => (
                    <tr key={p.id} className="border-b border-[#1E2638]/60 hover:bg-[#182030]">
                      <td className="px-3 py-2 font-bold text-slate-200">{p.coin}</td>
                      <td className="px-3 py-2"><span className={`text-[10px] font-bold ${p.direction === "LONG" ? "text-emerald-300" : "text-rose-300"}`}>{p.direction}</span></td>
                      <td className="px-3 py-2 text-right">{fmtNum(p.entry_price_inr, 6)}</td>
                      <td className="px-3 py-2 text-right">{fmtNum(p.exit_price_inr, 6)}</td>
                      <td className={`px-3 py-2 text-right ${p.gross_pnl_inr >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{p.gross_pnl_inr >= 0 ? "+" : ""}{fmtNum(p.gross_pnl_inr, 2)}</td>
                      <td className="px-3 py-2 text-right text-slate-500">−{fmtNum(p.fees_inr, 2)}</td>
                      <td className={`px-3 py-2 text-right font-bold ${p.net_pnl_inr >= 0 ? "text-emerald-300" : "text-rose-300"}`}>{p.net_pnl_inr >= 0 ? "+" : ""}{fmtNum(p.net_pnl_inr, 2)}</td>
                      <td className="px-3 py-2 text-[10px] uppercase tracking-wider text-slate-500">{p.close_reason}</td>
                    </tr>
                  ))}
                  {!closed.filter((p) => !coinFilter || p.coin === coinFilter).length && <tr><td colSpan={8} className="text-center py-6 text-slate-500">No closed trades{coinFilter ? ` for ${coinFilter}` : ""} yet.</td></tr>}
                </tbody>
              </table>
            </div>
            </div>
          </div>

          <div data-testid="hourly-performance-summary-card" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5 h-fit">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-slate-200" style={{fontFamily:"Outfit, sans-serif"}}>Session Summary</h3>
              <Target className="h-4 w-4 text-cyan-400/60"/>
            </div>
            <dl className="space-y-3 text-sm font-mono">
              <div className="flex justify-between"><dt className="text-slate-500">Trades</dt><dd className="text-slate-100">{status?.trades ?? 0}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Win rate</dt><dd className="text-cyan-300">{wr.toFixed(1)}%</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Realized PnL</dt><dd className={pnlAccent === "green" ? "text-emerald-300" : "text-rose-300"}>{status?.realized_pnl_inr >= 0 ? "+" : ""}{fmtINR(status?.realized_pnl_inr)}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Avg hold</dt><dd className="text-slate-100">{(status?.avg_hold_sec || 0).toFixed(1)}s</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Avg latency</dt><dd className="text-amber-300">{(status?.avg_exec_latency_ms || 0).toFixed(0)}ms</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Mode</dt><dd className="text-slate-100">{status?.mode}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Hurdle gate</dt><dd className="text-purple-300">{fmtPct(status?.hurdle_pct)}</dd></div>
            </dl>
          </div>
        </section>

        {/* Config panel + Event log */}
        <section className="grid lg:grid-cols-2 gap-6">
          <div data-testid="bot-config-controls-panel" className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
            <h3 className="font-semibold text-slate-200 mb-4" style={{fontFamily:"Outfit, sans-serif"}}>Signal & Risk Parameters</h3>
            {cfgDraft && (
              <label data-testid="basis-filter-toggle" className="mb-3 flex items-center justify-between gap-3 rounded border border-[#1E2638] bg-[#0A0D12] px-3 py-2 text-xs">
                <span className="font-mono text-slate-300">Basis filter <span className="text-slate-500">· subtract rolling {cfgDraft.basis_window_s || 300}s median offset</span></span>
                <input data-testid="basis-filter-checkbox" type="checkbox" checked={!!cfgDraft.basis_filter} onChange={(e) => setCfgDraft({ ...cfgDraft, basis_filter: e.target.checked })} className="h-4 w-4 accent-purple-500" />
              </label>
            )}
            {cfgDraft && (
              <div className="grid grid-cols-2 gap-3 text-xs">
                {[
                  ["entry_taker_fee_pct", "Entry Fee %", "config-fee-percentage-input"],
                  ["exit_taker_fee_pct", "Exit Fee %"],
                  ["slippage_buffer_pct", "Slippage %"],
                  ["hurdle_extra_pct", "Extra Hurdle %", "config-hurdle-gate-input"],
                  ["timeout_seconds", "Timeout (s)", "config-timeout-window-input"],
                  ["stop_loss_pct", "Stop Loss %"],
                  ["max_slippage_pct", "Max RT Slippage %", "config-max-slippage-input"],
                  ["phantom_frozen_s", "Phantom after (s)", "config-phantom-frozen-input"],
                  ["min_order_inr", "Shrink floor ₹", "config-min-order-input"],
                  ["max_entry_frozen_s", "Fresh book ≤ (s)", "config-fresh-book-input"],
                ].map(([k, label, tid]) => (
                  <label key={k} className="block">
                    <span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">{label}</span>
                    <input data-testid={tid} type="number" step="0.01" value={cfgDraft[k]} onChange={(e) => setCfgDraft({...cfgDraft, [k]: e.target.value})}
                      className="mt-1 w-full bg-[#0A0D12] border border-[#1E2638] rounded px-2 py-1.5 font-mono text-slate-100 focus:outline-none focus:border-cyan-600 focus:ring-1 focus:ring-cyan-800"/>
                  </label>
                ))}
              </div>
            )}
            <button data-testid="config-save-settings-btn" disabled={saving} onClick={saveCfg} className="mt-4 w-full py-2 rounded-full bg-cyan-500 hover:bg-cyan-400 text-slate-900 text-xs font-bold uppercase tracking-widest transition-colors duration-150 disabled:opacity-50">
              {saving ? "Saving…" : "Save Settings"}
            </button>
            <HurdleTuner draftHurdle={draftHurdle} sim={hurdleSim} />
            <div className="mt-4 pt-4 border-t border-[#1E2638]">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">CoinSwitch REST</span>
                <button data-testid="coinswitch-live-check-btn" onClick={runLiveCheck} className="px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest bg-slate-800 hover:bg-slate-700 text-slate-100 transition-colors duration-150">Run check</button>
              </div>
              {liveCheck && (
                <div data-testid="coinswitch-live-check-result" className={`text-[11px] font-mono rounded border px-2 py-1.5 ${liveCheck.loading ? "border-slate-700 text-slate-400" : liveCheck.ok ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>
                  {liveCheck.loading ? "Calling wallet_balance…" : liveCheck.ok ? "✓ CoinSwitch API reachable · keys valid" : `✗ ${liveCheck.error}`}
                </div>
              )}
              <div className="mt-3 flex items-center justify-between gap-2 flex-wrap">
                <span className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">CoinDCX (Pair B) · {config?.coindcx_client_ready ? "keys loaded" : "no keys"}</span>
                <button data-testid="coindcx-check-btn" onClick={runDcxCheck} disabled={dcxCheck?.loading} className="px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-widest bg-slate-800 hover:bg-slate-700 text-slate-100 transition-colors duration-150 disabled:opacity-40">Check CoinDCX</button>
              </div>
              {dcxCheck && (
                <div data-testid="coindcx-check-result" className={`mt-2 text-[11px] font-mono rounded border px-2 py-1.5 break-words ${dcxCheck.loading ? "border-slate-700 text-slate-400" : dcxCheck.ok ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>
                  {dcxCheck.loading ? "Calling futures wallets…" : dcxCheck.ok ? `✓ CoinDCX Futures API reachable · keys valid · wallets: ${(dcxCheck.data.wallets || []).map((w) => `${w.currency_short_name || w.currency || "USDT"} ${w.balance ?? ""}`).join(", ") || "n/a"}` : `✗ ${dcxCheck.error}`}
                </div>
              )}
              <div data-testid="coindcx-test-order-panel" className="mt-3 rounded border border-[#1E2638] bg-[#0A0D12] p-3">
                <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono mb-2">Pair B test order · market BUY → reduce-only SELL (real money)</div>
                <div className="grid grid-cols-4 gap-2 text-xs">
                  <label className="block"><span className="text-[10px] text-slate-500 font-mono">Coin</span><input data-testid="coindcx-test-coin-input" value={dcxTest.coin} onChange={(e) => setDcxTest({ ...dcxTest, coin: e.target.value.toUpperCase() })} className="mt-1 w-full bg-[#121721] border border-[#1E2638] rounded px-2 py-1 font-mono text-slate-100 text-xs"/></label>
                  <label className="block"><span className="text-[10px] text-slate-500 font-mono">Size USDT (≥6)</span><input data-testid="coindcx-test-size-input" type="number" min="1" max="50" step="1" value={dcxTest.notional_usdt} onChange={(e) => setDcxTest({ ...dcxTest, notional_usdt: e.target.value })} className="mt-1 w-full bg-[#121721] border border-[#1E2638] rounded px-2 py-1 font-mono text-slate-100 text-xs"/></label>
                  <label className="block"><span className="text-[10px] text-slate-500 font-mono">Leverage</span><input data-testid="coindcx-test-leverage-input" type="number" min="1" max="10" step="1" value={dcxTest.leverage} onChange={(e) => setDcxTest({ ...dcxTest, leverage: e.target.value })} className="mt-1 w-full bg-[#121721] border border-[#1E2638] rounded px-2 py-1 font-mono text-slate-100 text-xs"/></label>
                  <label className="flex items-end gap-2 pb-1 text-[10px] font-mono text-slate-400"><input data-testid="coindcx-test-confirm-checkbox" type="checkbox" checked={dcxTest.confirm} onChange={(e) => setDcxTest({ ...dcxTest, confirm: e.target.checked })} className="h-4 w-4 accent-rose-500"/> I understand this trades real funds</label>
                </div>
                <button data-testid="coindcx-test-order-btn" onClick={runDcxTestOrder} disabled={!dcxCheck?.ok || !dcxTest.confirm || dcxTest.loading} title={!dcxCheck?.ok ? "run Check CoinDCX first" : ""} className="mt-2 w-full py-1.5 rounded-full text-[10px] font-bold uppercase tracking-widest bg-rose-700 hover:bg-rose-600 text-white transition-colors duration-150 disabled:opacity-40">{dcxTest.loading ? "Placing…" : "Place test round-trip"}</button>
                {dcxTest.result && (
                  <div data-testid="coindcx-test-order-result" className={`mt-2 text-[11px] font-mono rounded border px-2 py-1.5 break-words ${dcxTest.result.ok ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : "border-rose-800 bg-rose-950/30 text-rose-300"}`}>
                    {dcxTest.result.ok ? `✓ ${dcxTest.result.coin} qty ${dcxTest.result.qty} · round-trip cost ≈ ${dcxTest.result.round_trip_cost_usdt} USDT · flat: ${dcxTest.result.flat ? "yes" : "CHECK"}` : `✗ ${dcxTest.result.error}`}
                    {(dcxTest.result.steps || []).map((s, i) => <div key={i} className="text-[10px] text-slate-400">{s.ok ? "✓" : "✗"} {s.step}{s.status ? ` · ${s.status}` : ""}{s.avg_price ? ` @ ${s.avg_price}` : ""}{s.latency_ms ? ` · ${s.latency_ms} ms` : ""}{s.error ? ` · ${s.error}` : ""}</div>)}
                  </div>
                )}
              </div>
              {telemetry?.feed?.fx && (
                <div data-testid="fx-sources-row" className="mt-3 text-[10px] font-mono text-slate-500">
                  <span className="uppercase tracking-widest">USDT/INR blend</span> <span className="text-slate-200">₹{telemetry.feed.fx.rate}</span> = median of {Object.entries(telemetry.feed.fx.sources).map(([k, v]) => <span key={k} data-testid={`fx-source-${k}`} className={`ml-1.5 px-1.5 py-0.5 rounded border ${v.rate && telemetry.feed.fx.used.includes(k) ? "border-[#1E2638] text-slate-300" : "border-rose-900 text-rose-400"}`}>{k} {v.rate ?? "—"}</span>)}
                </div>
              )}
            </div>
            <TelegramPanel tg={telemetry?.telegram} result={tgResult} onTest={() => sendTelegram("test")} onSummary={() => sendTelegram("summary")} />
          </div>

          <div className="rounded-lg border border-[#1E2638] bg-[#121721] p-5">
            <h3 className="font-semibold text-slate-200 mb-4" style={{fontFamily:"Outfit, sans-serif"}}>Event Log</h3>
            <div data-testid="event-log" className="space-y-1 text-[11px] font-mono max-h-[280px] overflow-y-auto">
              {logs.map((l, i) => (
                <div key={i} className="flex gap-2">
                  <span className="text-slate-600 shrink-0">{new Date(l.ts*1000).toLocaleTimeString()}</span>
                  <span className={`shrink-0 font-bold ${l.level === "TRADE" ? "text-cyan-400" : l.level === "WARN" ? "text-amber-400" : "text-slate-500"}`}>{l.level}</span>
                  <span className="text-slate-300">{l.msg}</span>
                </div>
              ))}
              {!logs.length && <div className="text-slate-500 text-xs">No events yet.</div>}
            </div>
          </div>
        </section>

        <BenchmarkPanel bench={bench} verdict={verdict} onRun={runBenchmark} running={!!bench?.running} duration={benchDuration} setDuration={setBenchDuration} fees={fees} setFees={setFees} />

        <ReplayPanel sessions={sessions} recorder={recorder} config={config} status={status} onRun={runReplay} result={replay} running={replayRunning} />

        <ComparePanel data={compare} onReset={resetCompare} />
        <SweepPanel sessions={sessions} onRun={runSweep} result={sweep} running={sweepRunning} />

        <ScreenerPanel result={screenRes} onRun={runScreener} onApply={applyUniverseCoins} running={screening} applyResult={applyUniverse} openCount={open.length} />

        <footer className="pt-4 pb-8 text-center text-[10px] uppercase tracking-widest text-slate-600 font-mono">
          QuantLag HFT · Binance Futures leader → CoinSwitch PRO laggard · {status?.mode || "…"} mode
        </footer>
      </main>
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard/>} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
