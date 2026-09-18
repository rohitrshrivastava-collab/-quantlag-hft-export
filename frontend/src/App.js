import { useEffect, useState } from "react";
import axios from "axios";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Activity, Zap, TrendingUp, TrendingDown, Play, Square, RefreshCw, Radio, Cpu, Wallet, Timer, Target } from "lucide-react";
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

const stateBadge = (state) => {
  const map = {
    WAITING: "bg-slate-800 text-slate-400 border-slate-700",
    HURDLE_MET_LONG: "bg-emerald-950/50 text-emerald-300 border-emerald-800 animate-pulse",
    HURDLE_MET_SHORT: "bg-rose-950/50 text-rose-300 border-rose-800 animate-pulse",
    IN_TRADE: "bg-cyan-950/50 text-cyan-300 border-cyan-800",
  };
  return `inline-flex items-center px-2 py-0.5 rounded-full border font-mono text-[10px] uppercase tracking-wider ${map[state] || map.WAITING}`;
};

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

  const pull = async () => {
    try {
      const [s, sp, op, cl, lg, eq, cfg] = await Promise.all([
        axios.get(`${API}/status`),
        axios.get(`${API}/spreads`),
        axios.get(`${API}/positions/open`),
        axios.get(`${API}/positions/closed?limit=50`),
        axios.get(`${API}/logs?limit=40`),
        axios.get(`${API}/equity-curve`),
        axios.get(`${API}/config`),
      ]);
      setStatus(s.data);
      setSpreads(sp.data.items || []);
      setOpen(op.data.items || []);
      setClosed(cl.data.items || []);
      setLogs(lg.data.items || []);
      setEquity(eq.data.items || []);
      setConfig(cfg.data);
      if (!cfgDraft) setCfgDraft(cfg.data);
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
    });
    setSaving(false);
    pull();
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
              <div className="text-[10px] uppercase tracking-widest text-slate-500 font-mono">Binance → CoinSwitch PRO · Lead-Lag Arb</div>
            </div>
          </div>
          <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
            <span data-testid="telemetry-ws-heartbeat-status" className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border font-mono text-[10px] uppercase tracking-wider ${status?.running ? "border-emerald-800 bg-emerald-950/40 text-emerald-300" : "border-rose-800 bg-rose-950/40 text-rose-300"}`}>
              <Radio className={`h-3 w-3 ${status?.running ? "animate-pulse" : ""}`}/> {status?.running ? "WS LIVE" : "STOPPED"}
            </span>
            <span data-testid="usdt-inr-fx-rate-badge" className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-purple-800 bg-purple-950/30 text-purple-300 font-mono text-[10px] uppercase tracking-wider">
              USDT/INR · {fmtNum(status?.fx_usdt_inr, 4)}
            </span>
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

        {/* Portfolio cards */}
        <section className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
          <StatCard testId="portfolio-equity-card" label="Total Equity (INR)" value={fmtINR(status?.equity_inr)} sub={`Start ₹${status?.starting_capital_inr?.toLocaleString?.() || "—"}`} accent="cyan" icon={<Wallet className="h-4 w-4 text-cyan-400/60"/>} />
          <StatCard testId="portfolio-free-margin-card" label="Free Margin" value={fmtINR(status?.free_margin_inr)} sub={`₹${status?.allocated_per_slot_inr?.toFixed?.(2) || "0"} / slot · ${status?.num_slots || 10} slots`} accent="slate" icon={<Cpu className="h-4 w-4 text-slate-400/60"/>} />
          <StatCard testId="portfolio-net-pnl-card" label="Net Realized PnL" value={`${(status?.realized_pnl_inr ?? 0) >= 0 ? "+" : ""}${fmtINR(status?.realized_pnl_inr).replace("₹","₹")}`} sub={`W ${status?.wins || 0} / L ${status?.losses || 0} · ${wr.toFixed(1)}% win`} accent={pnlAccent} icon={<TrendingUp className="h-4 w-4 text-emerald-400/60"/>} />
          <StatCard testId="portfolio-execution-latency-card" label="Exec Latency (avg)" value={`${(status?.avg_exec_latency_ms || 0).toFixed(0)} ms`} sub={`Hurdle gate · ${fmtPct(status?.hurdle_pct)}`} accent="amber" icon={<Timer className="h-4 w-4 text-amber-400/60"/>} />
        </section>

        {/* Spread monitor grid */}
        <section>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg sm:text-xl font-semibold tracking-tight text-slate-200" style={{fontFamily:"Outfit, sans-serif"}}>Real-time Spread Monitor · <span className="text-cyan-400">10 slots</span></h2>
            <button onClick={pull} className="text-xs text-slate-400 hover:text-slate-200 inline-flex items-center gap-1 transition-colors duration-150"><RefreshCw className="h-3 w-3"/> refresh</button>
          </div>
          <div data-testid="realtime-spread-slot-grid" className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
            {spreads.map((sp) => {
              const positive = (sp.gross_spread_pct || 0) >= 0;
              return (
                <div key={sp.coin} data-testid={`spread-slot-card-${sp.coin}`} className="rounded-lg border border-[#1E2638] bg-[#121721] p-3 transition-colors duration-150 hover:bg-[#182030] hover:border-slate-700">
                  <div className="flex items-center justify-between mb-1">
                    <div className="font-bold text-sm tracking-wide">{sp.coin}<span className="text-slate-500 text-[10px] ml-1">/INR</span></div>
                    <span className={stateBadge(sp.signal_state)}>{(sp.signal_state || "waiting").replace("_"," ")}</span>
                  </div>
                  <div className="flex items-baseline justify-between">
                    <div className={`text-lg font-mono font-bold ${positive ? "text-emerald-400" : "text-rose-400"}`}>{sp.gross_spread_pct >= 0 ? "+" : ""}{(sp.gross_spread_pct ?? 0).toFixed(3)}%</div>
                    <Sparkline points={sp.sparkline || []} color={positive ? "#10B981" : "#EF4444"} width={72} height={20} />
                  </div>
                  <div className="mt-1 grid grid-cols-2 gap-1 text-[10px] font-mono text-slate-500">
                    <div>BIN ask <span className="text-slate-300">{fmtNum(sp.binance_ask, 4)}</span></div>
                    <div>CSW bid <span className="text-slate-300">{fmtNum(sp.coinswitch_bid, 4)}</span></div>
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

        {/* Closed trades + Hourly summary side by side */}
        <section className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            <h2 className="text-lg sm:text-xl font-semibold tracking-tight text-slate-200 mb-3" style={{fontFamily:"Outfit, sans-serif"}}>Recent Closed Trades</h2>
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
                  {closed.map((p) => (
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
                  {!closed.length && <tr><td colSpan={8} className="text-center py-6 text-slate-500">No closed trades yet.</td></tr>}
                </tbody>
              </table>
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
              <div className="grid grid-cols-2 gap-3 text-xs">
                {[
                  ["entry_taker_fee_pct", "Entry Fee %", "config-fee-percentage-input"],
                  ["exit_taker_fee_pct", "Exit Fee %"],
                  ["slippage_buffer_pct", "Slippage %"],
                  ["hurdle_extra_pct", "Extra Hurdle %", "config-hurdle-gate-input"],
                  ["timeout_seconds", "Timeout (s)", "config-timeout-window-input"],
                  ["stop_loss_pct", "Stop Loss %"],
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
