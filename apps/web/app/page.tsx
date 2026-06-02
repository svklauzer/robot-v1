"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import AppShell from "../components/AppShell";
import { apiGet, apiPost } from "../lib/api";
import { Activity, BarChart3, Bot, CreditCard, RefreshCw, ShieldCheck } from "lucide-react";

export default function DashboardPage() {
  const [botState, setBotState] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [readiness, setReadiness] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setLoading(true);
    try {
      const [state, summary, readinessData] = await Promise.all([
        apiGet("/bot/state"),
        apiGet("/analytics/summary"),
        apiGet("/system/readiness"),
      ]);
      setBotState(state);
      setAnalytics(summary);
      setReadiness(readinessData);
    } finally {
      setLoading(false);
    }
  }

  async function startBot() {
    await apiPost("/bot/start");
    await loadAll();
  }

  async function stopBot() {
    await apiPost("/bot/stop");
    await loadAll();
  }

  useEffect(() => {
    loadAll();
  }, []);

  const bot = botState?.bot;
  const blockers = readiness?.blockers || [];

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-emerald-300">Finmt Owner Dashboard</h1>
          <p className="mt-2 text-sm text-emerald-100/70">
            Сводка по роботу, прибыли, подпискам и readiness. Детальные операции вынесены в профильные разделы.
          </p>
        </div>

        <div className="flex flex-wrap gap-3">
          <button
            onClick={loadAll}
            className="flex items-center gap-2 rounded-xl bg-emerald-900 px-4 py-2 text-sm font-semibold hover:bg-emerald-800"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
          <button
            onClick={startBot}
            className="rounded-xl bg-emerald-500 px-4 py-2 text-sm font-bold text-slate-950 hover:bg-emerald-400"
          >
            Start
          </button>
          <button
            onClick={stopBot}
            className="rounded-xl bg-red-500 px-4 py-2 text-sm font-bold text-slate-950 hover:bg-red-400"
          >
            Stop
          </button>
        </div>
      </header>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard title="Bot" value={bot?.status || "-"} subtitle={bot?.mode || "-"} icon={<Bot size={18} />} good={bot?.status === "running"} warn={bot?.status !== "running"} />
        <StatCard title="Net PnL" value={`${fmt(analytics?.total_net_pnl_usdt, 2)} USDT`} subtitle={`costs ${fmt(analytics?.total_costs_usdt, 2)} USDT`} icon={<BarChart3 size={18} />} good={(analytics?.total_net_pnl_usdt ?? 0) > 0} danger={(analytics?.total_net_pnl_usdt ?? 0) < 0} />
        <StatCard title="Readiness" value={readiness?.ready ? "READY" : "BLOCKED"} subtitle={`${blockers.length} blockers`} icon={<ShieldCheck size={18} />} good={readiness?.ready} danger={!readiness?.ready} />
        <StatCard title="Payments" value={readiness?.payments?.cash_collected ?? 0} subtitle={`paid ${readiness?.payments?.paid ?? 0}, pending ${readiness?.payments?.pending ?? 0}`} icon={<CreditCard size={18} />} good />
      </section>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <MiniCard title="Signals" value={analytics?.total_signals ?? 0} link="/signals" />
        <MiniCard title="Winrate" value={`${analytics?.winrate ?? 0}%`} link="/analytics" />
        <MiniCard title="Active" value={analytics?.active_signals ?? 0} link="/positions" />
        <MiniCard title="Telegram SLA" value={`${readiness?.telegram_delivery?.sla_pct ?? 100}%`} link="/health" />
      </section>

      {blockers.length > 0 && (
        <section className="rounded-2xl border border-red-900/70 bg-red-950/20 p-5">
          <h2 className="mb-3 flex items-center gap-2 text-xl font-semibold text-red-200">
            <Activity size={18} />
            Go-live blockers
          </h2>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {blockers.map((blocker: string, idx: number) => (
              <div key={idx} className="rounded-xl border border-red-900/70 bg-black/20 p-3 text-sm text-red-100">
                {blocker}
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <QuickLink href="/analytics" title="Profit analytics" text="PnL, quality, readiness gates и reason breakdown." />
        <QuickLink href="/payments" title="Payments" text="Создание checkout и подтверждение оплат VIP." />
        <QuickLink href="/health" title="System health" text="API, loops, market, Telegram delivery и production blockers." />
      </section>
    </AppShell>
  );
}

function StatCard({ title, value, subtitle, icon, good, warn, danger }: { title: string; value: any; subtitle: string; icon: any; good?: boolean; warn?: boolean; danger?: boolean }) {
  const valueClass = danger ? "text-red-300" : warn ? "text-yellow-300" : good ? "text-emerald-300" : "text-emerald-200";

  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="mb-3 flex items-center justify-between text-emerald-100/60">
        <span className="text-sm">{title}</span>
        {icon}
      </div>
      <div className={`text-2xl font-bold ${valueClass}`}>{value}</div>
      <div className="mt-1 text-xs text-emerald-100/50">{subtitle}</div>
    </div>
  );
}

function MiniCard({ title, value, link }: { title: string; value: any; link: string }) {
  return (
    <Link href={link} className="rounded-2xl border border-emerald-900 bg-black/30 p-5 transition hover:border-emerald-500 hover:bg-emerald-950/30">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className="mt-2 text-2xl font-bold text-emerald-200">{value}</div>
    </Link>
  );
}

function QuickLink({ href, title, text }: { href: string; title: string; text: string }) {
  return (
    <Link href={href} className="rounded-2xl border border-emerald-900 bg-black/30 p-5 transition hover:border-emerald-500 hover:bg-emerald-950/30">
      <div className="text-lg font-semibold text-emerald-200">{title}</div>
      <p className="mt-2 text-sm text-emerald-100/60">{text}</p>
    </Link>
  );
}

function fmt(value: any, digits = 2) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "0.00";
}
