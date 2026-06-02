"use client";

import { useEffect, useMemo, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import { ArrowDownUp, PlayCircle, RefreshCw, ShieldCheck, TrendingUp } from "lucide-react";

export default function FundingArbPage() {
  const [summary, setSummary] = useState<any>(null);
  const [opportunities, setOpportunities] = useState<any[]>([]);
  const [positions, setPositions] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [action, setAction] = useState<string | null>(null);
  const [notional, setNotional] = useState("100");
  const [lastSmoke, setLastSmoke] = useState<any>(null);

  async function loadAll() {
    setLoading(true);
    try {
      const [summaryData, oppData, posData] = await Promise.all([
        apiGet("/funding-arb/summary"),
        apiGet("/funding-arb/opportunities?limit=50"),
        apiGet("/funding-arb/positions?limit=50"),
      ]);
      setSummary(summaryData || null);
      setOpportunities(oppData?.items || []);
      setPositions(posData?.items || []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  const candidates = useMemo(() => opportunities.filter((item) => item.status === "candidate"), [opportunities]);
  const openPositions = useMemo(() => positions.filter((item) => item.status === "open"), [positions]);

  async function scanNow() {
    setAction("scan");
    try {
      const res = await apiPost("/funding-arb/scan", {});
      if (res?.status === "error") {
        alert(`Funding scan failed: ${res.error}`);
      }
      await loadAll();
    } finally {
      setAction(null);
    }
  }


  async function evaluateExits() {
    setAction("exits");
    try {
      const res = await apiPost("/funding-arb/evaluate-exits", {});
      if (res?.status === "error") {
        alert(`Exit evaluation failed: ${res.error}`);
      }
      await loadAll();
    } finally {
      setAction(null);
    }
  }

  async function runPaperSmoke() {
    const amount = Number(notional);
    if (!Number.isFinite(amount) || amount <= 0) {
      alert("Введите положительный notional USDT для paper smoke");
      return;
    }
    setAction("smoke");
    try {
      const res = await apiPost("/funding-arb/paper-smoke", {
        notional_usdt: amount,
        funding_periods: 1,
        persist: false,
      });
      setLastSmoke(res);
      if (res?.status === "error") {
        alert(`Paper smoke failed: ${res.error}`);
      }
      await loadAll();
    } finally {
      setAction(null);
    }
  }

  async function openPaper(opportunityId: number) {
    const amount = Number(notional);
    if (!Number.isFinite(amount) || amount <= 0) {
      alert("Введите положительный notional USDT для paper hedge");
      return;
    }
    if (!window.confirm(`Открыть PAPER hedge по opportunity #${opportunityId} на ${amount} USDT?`)) return;

    setAction(`open-${opportunityId}`);
    try {
      const res = await apiPost("/funding-arb/open", {
        opportunity_id: opportunityId,
        notional_usdt: amount,
        mode: "paper",
      });
      if (res?.status !== "ok") {
        alert(`Open hedge failed: ${res?.error || "unknown"}`);
      }
      await loadAll();
    } finally {
      setAction(null);
    }
  }

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
            <ArrowDownUp />
            HTX Funding Arb
          </h1>
          <p className="mt-2 max-w-3xl text-emerald-100/60">
            Single-exchange funding-rate arbitrage: spot long + USDT perpetual short inside HTX, monitored on 8h funding windows and gated by futures/live-safety flags.
          </p>
        </div>

        <div className="flex flex-wrap gap-3">
          <button onClick={loadAll} className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700">
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
          <button onClick={scanNow} className="flex items-center gap-2 rounded-xl bg-cyan-700 px-4 py-2 font-semibold hover:bg-cyan-600">
            <PlayCircle size={16} />
            {action === "scan" ? "Сканирование..." : "Scan HTX funding"}
          </button>
          <button onClick={evaluateExits} className="flex items-center gap-2 rounded-xl bg-yellow-700 px-4 py-2 font-semibold hover:bg-yellow-600">
            <ShieldCheck size={16} />
            {action === "exits" ? "Проверка exits..." : "Evaluate exits"}
          </button>
          <button onClick={runPaperSmoke} className="flex items-center gap-2 rounded-xl bg-purple-700 px-4 py-2 font-semibold hover:bg-purple-600">
            <ShieldCheck size={16} />
            {action === "smoke" ? "Paper smoke..." : "Paper smoke"}
          </button>
        </div>
      </header>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <Stat title="Engine" value={summary?.enabled ? "enabled" : "disabled"} good={summary?.enabled} warn={!summary?.enabled} />
        <Stat title="Symbols" value={(summary?.symbols || []).join(", ") || "-"} />
        <Stat title="Candidates" value={candidates.length} good={candidates.length > 0} />
        <Stat title="Open hedges" value={summary?.open_positions ?? openPositions.length} warn={(summary?.open_positions ?? openPositions.length) > 0} />
        <Stat title="Realized P&L" value={`${formatNumber(summary?.realized_pnl)} USDT`} good={(summary?.realized_pnl ?? 0) > 0} warn={(summary?.realized_pnl ?? 0) < 0} />
      </section>

      {lastSmoke && (
        <section className="rounded-2xl border border-purple-900 bg-purple-950/20 p-5">
          <h2 className="mb-3 flex items-center gap-2 text-xl font-semibold text-purple-200"><ShieldCheck size={18} /> Funding paper smoke</h2>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
            <Metric label="Status" value={lastSmoke.status || "unknown"} good={lastSmoke.status === "ok"} warn={lastSmoke.status !== "ok"} />
            <Metric label="Persisted" value={String(Boolean(lastSmoke.persisted))} />
            <Metric label="Funding periods" value={lastSmoke.position?.funding_periods ?? "-"} />
            <Metric label="Funding" value={`${formatNumber(lastSmoke.position?.funding_collected)} USDT`} good={(lastSmoke.position?.funding_collected ?? 0) > 0} />
            <Metric label="Realized" value={`${formatNumber(lastSmoke.position?.realized_pnl)} USDT`} good={(lastSmoke.position?.realized_pnl ?? 0) > 0} warn={(lastSmoke.position?.realized_pnl ?? 0) < 0} />
          </div>
        </section>
      )}

      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-emerald-200"><TrendingUp size={18} /> Latest opportunities</h2>
            <p className="mt-1 text-sm text-emerald-100/50">Candidate = funding rate edge passed threshold and basis is not too wide.</p>
          </div>
          <label className="flex items-center gap-2 text-sm text-emerald-100/60">
            Paper notional
            <input
              value={notional}
              onChange={(event) => setNotional(event.target.value)}
              className="w-28 rounded-lg border border-emerald-900 bg-slate-950 px-3 py-2 text-right text-emerald-100 outline-none focus:border-emerald-400"
            />
            USDT
          </label>
        </div>

        {opportunities.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-emerald-100/50">
                <tr className="border-b border-emerald-950">
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Symbol</th>
                  <th className="py-2 pr-4">Funding</th>
                  <th className="py-2 pr-4">Annualized</th>
                  <th className="py-2 pr-4">Basis</th>
                  <th className="py-2 pr-4">Edge</th>
                  <th className="py-2 pr-4">Spot / Swap</th>
                  <th className="py-2 pr-4">Next funding</th>
                  <th className="py-2 pr-4">Action</th>
                </tr>
              </thead>
              <tbody>
                {opportunities.map((item) => (
                  <tr key={item.id} className="border-b border-emerald-950/70">
                    <td className="py-3 pr-4"><Badge good={item.status === "candidate"} warn={item.status !== "candidate"}>{item.status}</Badge></td>
                    <td className="py-3 pr-4 font-semibold text-emerald-100">{item.symbol}</td>
                    <td className="py-3 pr-4 text-emerald-100">{formatPct(item.funding_rate_pct)}</td>
                    <td className="py-3 pr-4 text-emerald-100">{formatPct(item.annualized_rate_pct)}</td>
                    <td className="py-3 pr-4 text-emerald-100">{formatPct(item.basis_pct)}</td>
                    <td className="py-3 pr-4 text-emerald-100">{formatPct(item.estimated_edge_pct)}</td>
                    <td className="py-3 pr-4 text-emerald-100/60">{formatNumber(item.spot_price)} / {formatNumber(item.swap_price)}</td>
                    <td className="py-3 pr-4 text-emerald-100/60">{formatDate(item.next_funding_at)}</td>
                    <td className="py-3 pr-4">
                      <button
                        onClick={() => openPaper(item.id)}
                        disabled={item.status !== "candidate" || action === `open-${item.id}`}
                        className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-semibold text-emerald-50 disabled:cursor-not-allowed disabled:bg-emerald-950 disabled:text-emerald-100/40"
                      >
                        {action === `open-${item.id}` ? "Opening..." : "Open paper"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty text="Funding opportunities еще не сканировались" />
        )}
      </section>

      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-semibold text-emerald-200"><ShieldCheck size={18} /> Hedge positions</h2>
        {positions.length > 0 ? (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {positions.map((item) => (
              <div key={item.id} className="rounded-xl border border-emerald-950 bg-black/20 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-semibold text-emerald-100">#{item.id} {item.symbol}</div>
                    <div className="text-xs text-emerald-100/50">{item.hedge_side} · {item.mode}</div>
                  </div>
                  <Badge good={item.status === "closed"} warn={item.status === "open"}>{item.status}</Badge>
                </div>
                <div className="mt-4 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
                  <Metric label="Notional" value={`${formatNumber(item.notional_usdt)} USDT`} />
                  <Metric label="Entry funding" value={formatPct(item.entry_funding_rate_pct)} />
                  <Metric label="Funding" value={`${formatNumber(item.funding_collected)} USDT`} />
                  <Metric label="Realized" value={item.realized_pnl == null ? "-" : `${formatNumber(item.realized_pnl)} USDT`} good={(item.realized_pnl ?? 0) > 0} warn={(item.realized_pnl ?? 0) < 0} />
                </div>
                <div className="mt-3 text-xs text-emerald-100/45">Opened: {formatDate(item.opened_at)} · Closed: {formatDate(item.closed_at)}</div>
              </div>
            ))}
          </div>
        ) : (
          <Empty text="Нет открытых или закрытых funding hedge positions" />
        )}
      </section>
    </AppShell>
  );
}

function Stat({ title, value, good, warn }: { title: string; value: any; good?: boolean; warn?: boolean }) {
  const tone = good ? "text-emerald-300" : warn ? "text-yellow-300" : "text-emerald-100";
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/50">{title}</div>
      <div className={`mt-3 break-words text-2xl font-bold ${tone}`}>{value}</div>
    </div>
  );
}

function Metric({ label, value, good, warn }: { label: string; value: any; good?: boolean; warn?: boolean }) {
  const tone = good ? "text-emerald-300" : warn ? "text-yellow-300" : "text-emerald-100";
  return (
    <div className="rounded-lg border border-emerald-950 bg-slate-950/50 p-3">
      <div className="text-xs text-emerald-100/45">{label}</div>
      <div className={`mt-1 font-semibold ${tone}`}>{value}</div>
    </div>
  );
}

function Badge({ children, good, warn }: { children: any; good?: boolean; warn?: boolean }) {
  const cls = good
    ? "border-emerald-700 bg-emerald-950 text-emerald-200"
    : warn
      ? "border-yellow-700 bg-yellow-950/40 text-yellow-200"
      : "border-red-700 bg-red-950/40 text-red-200";
  return <span className={`inline-flex rounded-full border px-3 py-1 text-xs font-semibold ${cls}`}>{children}</span>;
}

function Empty({ text }: { text: string }) {
  return <div className="rounded-xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50">{text}</div>;
}

function formatNumber(value: any) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(Math.abs(num) >= 100 ? 2 : 4);
}

function formatPct(value: any) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${num.toFixed(Math.abs(num) >= 10 ? 2 : 4)}%`;
}

function formatDate(value: any) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}
