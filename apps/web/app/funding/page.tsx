"use client";

import { useEffect, useMemo, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import {
  ArrowDownUp,
  PlayCircle,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
  X,
  AlertTriangle,
  CheckCircle2,
  Clock,
  DollarSign,
} from "lucide-react";

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
    const timer = setInterval(loadAll, 30000);
    return () => clearInterval(timer);
  }, []);

  const candidates = useMemo(
    () => opportunities.filter((item) => item.status === "candidate"),
    [opportunities]
  );
  const openPositions = useMemo(
    () => positions.filter((item) => item.status === "open"),
    [positions]
  );
  const closedPositions = useMemo(
    () => positions.filter((item) => item.status === "closed"),
    [positions]
  );

  async function scanNow() {
    setAction("scan");
    try {
      const res = await apiPost("/funding-arb/scan", {});
      if (res?.status === "error") alert(`Scan failed: ${res.error}`);
      await loadAll();
    } finally {
      setAction(null);
    }
  }

  async function evaluateExits() {
    setAction("exits");
    try {
      const res = await apiPost("/funding-arb/evaluate-exits", {});
      if (res?.status === "error") alert(`Exit evaluation failed: ${res.error}`);
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
        funding_periods: 3,
        persist: false,
      });
      setLastSmoke(res);
      if (res?.status === "error") alert(`Paper smoke failed: ${res.error}`);
      await loadAll();
    } finally {
      setAction(null);
    }
  }

  async function openPaper(opportunityId: number) {
    const amount = Number(notional);
    if (!Number.isFinite(amount) || amount <= 0) {
      alert("Введите положительный notional USDT");
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
      if (res?.status !== "ok") alert(`Open hedge failed: ${res?.error || "unknown"}`);
      await loadAll();
    } finally {
      setAction(null);
    }
  }

  const totalPnl = summary?.total_pnl_estimate ?? (
    (summary?.realized_pnl ?? 0) + (summary?.unrealized_pnl_estimate ?? 0)
  );

  return (
    <AppShell>
      {/* ── Header ── */}
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
            <ArrowDownUp />
            HTX Funding Arb
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-emerald-100/60">
            Spot long + USDT perpetual short. Доход: funding rate (8h периоды).
            Риск: basis change. Авто-открытие paper позиций при положительном net yield.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={loadAll}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-3 py-2 text-sm font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={15} />
            {loading ? "..." : "Обновить"}
          </button>
          <button
            onClick={scanNow}
            className="flex items-center gap-2 rounded-xl bg-cyan-700 px-3 py-2 text-sm font-semibold hover:bg-cyan-600"
          >
            <PlayCircle size={15} />
            {action === "scan" ? "Сканирование..." : "Scan"}
          </button>
          <button
            onClick={evaluateExits}
            className="flex items-center gap-2 rounded-xl bg-yellow-700 px-3 py-2 text-sm font-semibold hover:bg-yellow-600"
          >
            <ShieldCheck size={15} />
            {action === "exits" ? "Проверка..." : "Evaluate exits"}
          </button>
          <button
            onClick={runPaperSmoke}
            className="flex items-center gap-2 rounded-xl bg-purple-700 px-3 py-2 text-sm font-semibold hover:bg-purple-600"
          >
            <ShieldCheck size={15} />
            {action === "smoke" ? "Smoke..." : "Paper smoke"}
          </button>
        </div>
      </header>

      {/* ── Stats row ── */}
      <section className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <StatCard
          title="Engine"
          value={summary?.enabled ? "enabled" : "disabled"}
          good={summary?.enabled}
          warn={!summary?.enabled}
          sub={summary?.auto_open_paper ? "auto-open ON" : "manual only"}
        />
        <StatCard title="Symbols" value={(summary?.symbols || []).join(", ") || "-"} />
        <StatCard
          title="Candidates"
          value={candidates.length}
          good={candidates.length > 0}
          sub="positive edge"
        />
        <StatCard
          title="Open hedges"
          value={openPositions.length}
          warn={openPositions.length > 0}
          sub={`of ${summary?.open_positions ?? "?"} total`}
        />
        <StatCard
          title="Realized P&L"
          value={`${fmt(summary?.realized_pnl)} USDT`}
          good={(summary?.realized_pnl ?? 0) > 0}
          warn={(summary?.realized_pnl ?? 0) < 0}
        />
        <StatCard
          title="Total P&L est."
          value={`${fmt(totalPnl)} USDT`}
          good={totalPnl > 0}
          warn={totalPnl < 0}
          sub={`unrealized ~${fmt(summary?.unrealized_pnl_estimate)} USDT`}
        />
      </section>

      {/* ── Economics explainer ── */}
      <section className="rounded-2xl border border-emerald-900/60 bg-emerald-950/10 p-4">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-emerald-300">
          <TrendingUp size={15} />
          Как работает стратегия
        </div>
        <div className="grid grid-cols-1 gap-3 text-xs text-emerald-100/70 md:grid-cols-4">
          <div className="rounded-lg border border-emerald-900/50 bg-black/20 p-3">
            <div className="mb-1 font-semibold text-emerald-200">1. Позиция</div>
            Spot LONG + Perpetual SHORT на одинаковый объём в USDT. Рыночный риск хеджирован.
          </div>
          <div className="rounded-lg border border-emerald-900/50 bg-black/20 p-3">
            <div className="mb-1 font-semibold text-emerald-200">2. Доход</div>
            Каждые 8 часов шорт-позиция получает funding payment (когда rate &gt; 0). 0.03%/период = ~33% годовых.
          </div>
          <div className="rounded-lg border border-emerald-900/50 bg-black/20 p-3">
            <div className="mb-1 font-semibold text-emerald-200">3. Комиссии</div>
            Round-trip ≈ 0.5% нотионала (spot 0.2%×2 + perp 0.05%×2). Нужно держать достаточно периодов.
          </div>
          <div className="rounded-lg border border-emerald-900/50 bg-black/20 p-3">
            <div className="mb-1 font-semibold text-emerald-200">4. Выход</div>
            Закрываем когда funding сжался ниже порога или истёк max_hold. Выполняем автоматически в paper mode.
          </div>
        </div>
      </section>

      {/* ── Paper smoke result ── */}
      {lastSmoke && (
        <section className="rounded-2xl border border-purple-900 bg-purple-950/20 p-5">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-lg font-semibold text-purple-200">
              <ShieldCheck size={16} />
              Paper smoke result
            </h2>
            <button onClick={() => setLastSmoke(null)} className="text-purple-100/50 hover:text-purple-100">
              <X size={16} />
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
            <Metric label="Status" value={lastSmoke.status || "?"} good={lastSmoke.status === "ok"} />
            <Metric label="Periods" value={lastSmoke.position?.funding_periods ?? "-"} />
            <Metric
              label="Funding earned"
              value={`${fmt(lastSmoke.position?.funding_collected)} USDT`}
              good={(lastSmoke.position?.funding_collected ?? 0) > 0}
            />
            <Metric
              label="Fees paid"
              value={`${fmt(lastSmoke.position?.fees_paid)} USDT`}
              warn
            />
            <Metric
              label="Realized P&L"
              value={`${fmt(lastSmoke.position?.realized_pnl)} USDT`}
              good={(lastSmoke.position?.realized_pnl ?? 0) > 0}
              warn={(lastSmoke.position?.realized_pnl ?? 0) < 0}
            />
          </div>
          {lastSmoke.position && (
            <div className="mt-3 text-xs text-purple-100/50">
              Entry funding: {fmtPct(lastSmoke.position.entry_funding_rate_pct)} · Notional: {fmt(lastSmoke.position.notional_usdt)} USDT
            </div>
          )}
        </section>
      )}

      {/* ── Opportunities table ── */}
      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-emerald-200">
              <TrendingUp size={18} />
              Latest opportunities
            </h2>
            <p className="mt-1 text-xs text-emerald-100/50">
              Candidate = положительный net yield после комиссий и basis
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm text-emerald-100/60">
            Notional
            <input
              value={notional}
              onChange={(e) => setNotional(e.target.value)}
              className="w-24 rounded-lg border border-emerald-900 bg-slate-950 px-3 py-2 text-right text-emerald-100 outline-none focus:border-emerald-400"
            />
            <span>USDT</span>
          </label>
        </div>

        {opportunities.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-emerald-100/40">
                <tr className="border-b border-emerald-950">
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Symbol</th>
                  <th className="py-2 pr-3 text-right">Funding/8h</th>
                  <th className="py-2 pr-3 text-right">Annualized</th>
                  <th className="py-2 pr-3 text-right">Basis</th>
                  <th className="py-2 pr-3 text-right">Net yield/period</th>
                  <th className="py-2 pr-3 text-right">Break-even</th>
                  <th className="py-2 pr-3 text-right">Ann. net yield</th>
                  <th className="py-2 pr-3">Spot / Swap</th>
                  <th className="py-2 pr-3">Next funding</th>
                  <th className="py-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {opportunities.map((item) => {
                  const isCandidate = item.status === "candidate";
                  const netYield = Number(item.net_yield_per_period_pct ?? item.estimated_edge_pct ?? 0);
                  const annNet = Number(item.annualized_net_yield_pct ?? 0);
                  return (
                    <tr key={item.id} className="border-b border-emerald-950/50 hover:bg-emerald-950/10">
                      <td className="py-3 pr-3">
                        <StatusBadge status={item.status} />
                      </td>
                      <td className="py-3 pr-3 font-semibold text-emerald-100">{item.symbol}</td>
                      <td className="py-3 pr-3 text-right font-mono text-sm">
                        <span className={Number(item.funding_rate_pct) > 0 ? "text-emerald-300" : "text-red-300"}>
                          {fmtPct(item.funding_rate_pct)}
                        </span>
                      </td>
                      <td className="py-3 pr-3 text-right font-mono text-sm text-emerald-100/70">
                        {fmtPct(item.annualized_rate_pct)}
                      </td>
                      <td className="py-3 pr-3 text-right font-mono text-sm">
                        <span className={Number(item.basis_pct) > 0 ? "text-emerald-300" : "text-orange-300"}>
                          {fmtPct(item.basis_pct)}
                        </span>
                      </td>
                      <td className="py-3 pr-3 text-right font-mono text-sm font-semibold">
                        <span className={netYield > 0 ? "text-emerald-300" : netYield > -0.01 ? "text-yellow-300" : "text-red-400"}>
                          {fmtPct(netYield)}
                        </span>
                      </td>
                      <td className="py-3 pr-3 text-right text-sm text-emerald-100/60">
                        {item.break_even_periods != null ? `${item.break_even_periods}p` : "-"}
                      </td>
                      <td className="py-3 pr-3 text-right font-mono text-sm font-semibold">
                        <span className={annNet > 10 ? "text-emerald-300" : annNet > 0 ? "text-yellow-300" : "text-red-400"}>
                          {fmtPct(annNet)}
                        </span>
                      </td>
                      <td className="py-3 pr-3 text-xs text-emerald-100/50">
                        {fmt(item.spot_price)} / {fmt(item.swap_price)}
                      </td>
                      <td className="py-3 pr-3 text-xs text-emerald-100/50">
                        {formatDate(item.next_funding_at)}
                      </td>
                      <td className="py-3">
                        {isCandidate ? (
                          <button
                            onClick={() => openPaper(item.id)}
                            disabled={action === `open-${item.id}`}
                            className="flex items-center gap-1 rounded-lg bg-emerald-700 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
                          >
                            <DollarSign size={12} />
                            {action === `open-${item.id}` ? "..." : "Open paper"}
                          </button>
                        ) : (
                          <span className="text-xs text-emerald-100/30">{item.reject_reason ? "—" : "—"}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty text="Нет данных — нажмите «Scan» для поиска возможностей" />
        )}

        {/* Legend */}
        <div className="mt-4 flex flex-wrap gap-4 border-t border-emerald-950 pt-4 text-xs text-emerald-100/40">
          <span><span className="text-emerald-300">●</span> Net yield &gt; 0 = прибыльно после комиссий</span>
          <span><span className="text-orange-300">●</span> Basis &lt; 0 = spot дороже perp (менее выгодно)</span>
          <span><span className="text-yellow-300">●</span> Ann. net &gt; 5% = хорошая возможность</span>
          <span>Break-even = периодов для окупаемости комиссий</span>
        </div>
      </section>

      {/* ── Open positions ── */}
      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-xl font-semibold text-emerald-200">
            <ShieldCheck size={18} />
            Open hedges
            {openPositions.length > 0 && (
              <span className="rounded-lg bg-yellow-700 px-2 py-0.5 text-xs font-semibold text-black">
                {openPositions.length}
              </span>
            )}
          </h2>
          {openPositions.length > 0 && (
            <span className="text-xs text-emerald-100/50">
              Unrealized est: {fmt(summary?.unrealized_pnl_estimate)} USDT
            </span>
          )}
        </div>

        {openPositions.length > 0 ? (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {openPositions.map((item) => (
              <PositionCard key={item.id} item={item} isOpen />
            ))}
          </div>
        ) : (
          <Empty text="Нет открытых hedges" />
        )}
      </section>

      {/* ── Closed positions ── */}
      {closedPositions.length > 0 && (
        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 flex items-center gap-2 text-xl font-semibold text-emerald-200">
            <CheckCircle2 size={18} />
            Closed hedges ({closedPositions.length})
          </h2>
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            {closedPositions.slice(0, 10).map((item) => (
              <PositionCard key={item.id} item={item} isOpen={false} />
            ))}
          </div>
        </section>
      )}
    </AppShell>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function PositionCard({ item, isOpen }: { item: any; isOpen: boolean }) {
  return (
    <div className={`rounded-xl border p-4 ${isOpen ? "border-yellow-800/60 bg-yellow-950/10" : "border-emerald-950 bg-black/20"}`}>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-bold text-emerald-100">#{item.id} {item.symbol}</span>
            <span className={`rounded px-2 py-0.5 text-xs font-semibold ${item.mode === "paper" ? "bg-purple-900 text-purple-100" : "bg-emerald-700 text-white"}`}>
              {item.mode}
            </span>
            {isOpen && <span className="rounded bg-yellow-700 px-2 py-0.5 text-xs font-semibold text-black">open</span>}
            {!isOpen && item.realized_pnl != null && (
              <span className={`text-sm font-bold ${Number(item.realized_pnl) >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                {Number(item.realized_pnl) >= 0 ? "+" : ""}{fmt(item.realized_pnl)} USDT
              </span>
            )}
          </div>
          <div className="mt-1 text-xs text-emerald-100/40">{item.hedge_side}</div>
        </div>
        {isOpen && (
          <span className="flex items-center gap-1 text-xs text-yellow-300">
            <Clock size={11} />
            holding
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
        <Metric label="Notional" value={`${fmt(item.notional_usdt)} USDT`} />
        <Metric label="Entry funding" value={fmtPct(item.entry_funding_rate_pct)} />
        <Metric
          label="Funding earned"
          value={item.funding_collected != null ? `${fmt(item.funding_collected)} USDT` : "—"}
          good={(item.funding_collected ?? 0) > 0}
        />
        <Metric
          label="Realized"
          value={item.realized_pnl != null ? `${fmt(item.realized_pnl)} USDT` : "—"}
          good={(item.realized_pnl ?? 0) > 0}
          warn={(item.realized_pnl ?? 0) < 0}
        />
        <Metric label="Periods" value={item.funding_periods ?? "—"} />
        <Metric label="Fees paid" value={`${fmt(item.fees_paid)} USDT`} />
        <Metric label="Spot entry" value={fmt(item.spot_entry_price)} />
        <Metric label="Swap entry" value={fmt(item.swap_entry_price)} />
      </div>

      <div className="mt-3 text-xs text-emerald-100/35">
        {isOpen ? `Opened: ${formatDate(item.opened_at)}` : `${formatDate(item.opened_at)} → ${formatDate(item.closed_at)}`}
      </div>
    </div>
  );
}

function StatCard({
  title, value, sub, good, warn,
}: { title: string; value: any; sub?: string; good?: boolean; warn?: boolean }) {
  const tone = good ? "text-emerald-300" : warn ? "text-yellow-300" : "text-emerald-100";
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-4">
      <div className="text-xs text-emerald-100/50">{title}</div>
      <div className={`mt-2 break-words text-xl font-bold ${tone}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-emerald-100/35">{sub}</div>}
    </div>
  );
}

function Metric({ label, value, good, warn }: { label: string; value: any; good?: boolean; warn?: boolean }) {
  const tone = good ? "text-emerald-300" : warn ? "text-yellow-300" : "text-emerald-100";
  return (
    <div className="rounded-lg border border-emerald-950 bg-black/20 p-2">
      <div className="text-[10px] text-emerald-100/40">{label}</div>
      <div className={`mt-1 break-words text-sm font-semibold ${tone}`}>{value ?? "-"}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    candidate: "bg-emerald-700 text-white",
    disabled: "bg-slate-700 text-slate-200",
    below_funding_threshold: "bg-orange-900 text-orange-200",
    negative_funding: "bg-red-900 text-red-200",
    basis_too_wide: "bg-yellow-800 text-yellow-100",
    edge_too_low: "bg-red-800 text-red-100",
  };
  const cls = map[status] || "bg-slate-800 text-slate-200";
  return (
    <span className={`inline-block rounded-full px-2 py-1 text-[10px] font-semibold ${cls}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/40">
      {text}
    </div>
  );
}

// ── Formatters ────────────────────────────────────────────────────────────────

function fmt(value: any) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 10000) return n.toFixed(0);
  if (Math.abs(n) >= 100) return n.toFixed(2);
  return n.toFixed(4);
}

function fmtPct(value: any) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return `${n >= 0 ? "" : ""}${n.toFixed(Math.abs(n) >= 10 ? 2 : 4)}%`;
}

function formatDate(value: any) {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
