"use client";

import { useEffect, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { Activity, AlertTriangle, BarChart3, RefreshCw, ShieldCheck } from "lucide-react";

export default function AnalyticsPage() {
  const [summary, setSummary] = useState<any>(null);
  const [quality, setQuality] = useState<any>(null);
  const [readiness, setReadiness] = useState<any>(null);
  const [rootCause, setRootCause] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setLoading(true);
    try {
      const [summaryData, qualityData, readinessData, rootCauseData] = await Promise.all([
        apiGet("/analytics/summary"),
        apiGet("/analytics/signal-quality"),
        apiGet("/system/readiness"),
        apiGet("/analytics/outcome-root-cause?reason=failed_setup_exit&limit=500"),
      ]);
      setSummary(summaryData);
      setQuality(qualityData);
      setReadiness(readinessData);
      setRootCause(rootCauseData);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  const blockers = readiness?.blockers || [];

  return (
    <AppShell>
        <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
              <BarChart3 />
              Profit & Readiness Analytics
            </h1>
            <p className="mt-2 text-emerald-100/60">
              Контроль PnL, качества сигналов, Telegram delivery и go-live gates.
            </p>
          </div>

          <button
            onClick={loadAll}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
        </header>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <StatCard title="Net PnL" value={`${summary?.total_net_pnl_usdt ?? 0} USDT`} danger={(summary?.total_net_pnl_usdt ?? 0) < 0} good={(summary?.total_net_pnl_usdt ?? 0) > 0} />
          <StatCard title="Winrate" value={`${summary?.winrate ?? 0}%`} warn={(summary?.winrate ?? 0) < 45} good={(summary?.winrate ?? 0) >= 50} />
          <StatCard title="Failed setup" value={quality?.by_reason?.failed_setup_exit ?? 0} danger={(quality?.by_reason?.failed_setup_exit ?? 0) > 0} />
          <StatCard title="Positive→Negative" value={`${quality?.positive_then_negative_rate ?? 0}%`} warn={(quality?.positive_then_negative_rate ?? 0) > 25} />
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Panel title="Production readiness" icon={<ShieldCheck size={18} />}>
            <div className={readiness?.ready ? "text-emerald-300" : "text-red-300"}>
              {readiness?.ready ? "READY" : "BLOCKED"}
            </div>
            <div className="mt-4 space-y-2">
              {blockers.length === 0 && <p className="text-sm text-emerald-100/60">Блокеров нет.</p>}
              {blockers.map((item: string, idx: number) => (
                <div key={idx} className="rounded-xl border border-red-900/70 bg-red-950/30 p-3 text-sm text-red-100">
                  {item}
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="Telegram delivery 24h" icon={<Activity size={18} />}>
            <Metric label="SLA" value={`${readiness?.telegram_delivery?.sla_pct ?? 100}%`} />
            <Metric label="Sent" value={readiness?.telegram_delivery?.sent ?? 0} />
            <Metric label="Failed" value={readiness?.telegram_delivery?.failed ?? 0} />
            {readiness?.telegram_delivery?.last_error && (
              <p className="mt-3 text-xs text-red-200">{readiness.telegram_delivery.last_error}</p>
            )}
          </Panel>

          <Panel title="Profit gates" icon={<AlertTriangle size={18} />}>
            <Metric label="Closed validation signals" value={readiness?.required_gates?.closed_validation_signals ?? 200} />
            <Metric label="Failed setup max" value={`${readiness?.required_gates?.failed_setup_exit_share_max_pct ?? 35}%`} />
            <Metric label="Positive→Negative max" value={`${readiness?.required_gates?.positive_then_negative_max_pct ?? 25}%`} />
            <Metric label="Telegram SLA min" value={`${readiness?.required_gates?.telegram_delivery_sla_min_pct ?? 99}%`} />
          </Panel>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 text-xl font-semibold text-emerald-200">Reason breakdown</h2>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {Object.entries(quality?.by_reason || {}).map(([reason, count]) => (
              <div key={reason} className="rounded-xl border border-emerald-950 bg-black/20 p-4">
                <div className="text-sm text-emerald-100/50">{reason}</div>
                <div className="mt-1 text-2xl font-bold text-emerald-200">{String(count)}</div>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-2xl border border-red-900/70 bg-red-950/20 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-red-200">Failed setup root cause</h2>
              <p className="text-sm text-red-100/60">
                Roadmap Phase 1: где именно утек PnL по failed_setup_exit.
              </p>
            </div>
            <div className="text-right text-sm text-red-100/70">
              <div>{rootCause?.target_count ?? 0} / {rootCause?.sample_closed_signals ?? 0} closed</div>
              <div className="font-semibold text-red-200">{rootCause?.target_share_pct ?? 0}% share</div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="rounded-xl border border-red-900/60 bg-black/20 p-4">
              <h3 className="mb-3 font-semibold text-red-100">Worst symbols</h3>
              {(rootCause?.worst_symbols || []).slice(0, 5).map((item: any) => (
                <Metric key={item.key} label={`${item.key} (${item.count})`} value={`${item.net_pnl_usdt} USDT`} />
              ))}
            </div>

            <div className="rounded-xl border border-red-900/60 bg-black/20 p-4">
              <h3 className="mb-3 font-semibold text-red-100">Lifecycle leak</h3>
              <Metric label="Positive→Negative" value={`${rootCause?.metrics?.positive_then_negative_rate ?? 0}%`} />
              <Metric label="Avg MFE" value={`${rootCause?.metrics?.avg_mfe_pct ?? "-"}%`} />
              <Metric label="Avg missed" value={`${rootCause?.metrics?.avg_missed_profit_pct ?? "-"}%`} />
              <Metric label="Target net" value={`${rootCause?.target_net_pnl_usdt ?? 0} USDT`} />
            </div>

            <div className="rounded-xl border border-red-900/60 bg-black/20 p-4">
              <h3 className="mb-3 font-semibold text-red-100">Actions</h3>
              <div className="space-y-2 text-sm text-red-50/80">
                {(rootCause?.recommendations || []).map((item: string, idx: number) => (
                  <div key={idx} className="rounded-lg border border-red-900/50 bg-black/20 p-2">{item}</div>
                ))}
              </div>
            </div>
          </div>
        </section>
    </AppShell>
  );
}

function Panel({ title, icon, children }: { title: string; icon: any; children: any }) {
  return (
    <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <h2 className="mb-4 flex items-center gap-2 text-xl font-semibold text-emerald-200">
        {icon}
        {title}
      </h2>
      {children}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex items-center justify-between border-b border-emerald-950 py-2 text-sm last:border-b-0">
      <span className="text-emerald-100/60">{label}</span>
      <span className="font-semibold text-emerald-200">{value}</span>
    </div>
  );
}

function StatCard({ title, value, good, warn, danger }: { title: string; value: any; good?: boolean; warn?: boolean; danger?: boolean }) {
  const valueClass = danger
    ? "text-red-300"
    : warn
      ? "text-yellow-300"
      : good
        ? "text-emerald-300"
        : "text-emerald-200";

  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${valueClass}`}>{value}</div>
    </div>
  );
}
