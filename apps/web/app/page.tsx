"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import AppShell from "../components/AppShell";
import { apiGet, apiPost } from "../lib/api";
import { Activity, BarChart3, Bot, CreditCard, RefreshCw, ShieldCheck, TrendingUp } from "lucide-react";

export default function DashboardPage() {
  const [botState, setBotState] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [readiness, setReadiness] = useState<any>(null);
  const [dailyQuality, setDailyQuality] = useState<any>(null);
  const [orderbook, setOrderbook] = useState<any>(null);
  const [mlStats, setMlStats] = useState<any>(null);
  const [grid, setGrid] = useState<any>(null);
  const [funding, setFunding] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setLoading(true);
    try {
      const [state, summary, readinessData, dailyData] = await Promise.all([
        apiGet("/bot/state"),
        apiGet("/analytics/summary"),
        apiGet("/system/readiness"),
        apiGet("/analytics/daily-quality-report?hours=24"),
      ]);
      setBotState(state);
      setAnalytics(summary);
      setReadiness(readinessData);
      setDailyQuality(dailyData);
      // depth + ML: не валим дашборд, если движок off / эндпоинт недоступен
      apiGet("/orderbook/state").then(setOrderbook).catch(() => setOrderbook(null));
      apiGet("/ml/outcomes/stats").then(setMlStats).catch(() => setMlStats(null));
      // Три книги: Grid и Funding считаются раздельно — тянем их PnL отдельно, не валим дашборд
      apiGet("/grid/state").then(setGrid).catch(() => setGrid(null));
      apiGet("/funding-arb/summary").then(setFunding).catch(() => setFunding(null));
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

  // Три книги — realized PnL по движкам (раздельные карманы, статистики не смешиваем)
  const trendPnl = Number(analytics?.total_net_pnl_usdt ?? 0);
  const gridPnl = Number(grid?.realized_pnl_usdt ?? 0);
  const fundingPnl = Number(funding?.realized_pnl ?? 0);
  const totalPnl = trendPnl + gridPnl + fundingPnl;

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

      {/* Три книги — честный консолидированный PnL по движкам */}
      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-emerald-100/50">Три книги · realized PnL</h2>
          <span className="text-xs text-emerald-100/40">движки на раздельных карманах — статистики не смешиваются</span>
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <BookCard title="Trend" href="/signals" pnl={trendPnl} sub={`${analytics?.total_signals ?? 0} сигналов · WR ${analytics?.winrate ?? 0}%`} />
          <BookCard title="Grid" href="/grid" pnl={gridPnl} off={!grid} sub={grid ? `${grid?.active_cycles ?? 0} активных · ${grid?.closed_cycles ?? 0} закрыто` : "движок выкл/нет данных"} />
          <BookCard title="Funding" href="/funding" pnl={fundingPnl} off={!funding} sub={funding ? `${funding?.open_positions ?? 0} хеджей · unreal ${fmt(funding?.unrealized_pnl_estimate, 2)}` : "движок выкл/нет данных"} />
          <BookCard title="ИТОГО" pnl={totalPnl} total sub="сумма трёх книг" />
        </div>
      </section>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <StatCard title="Bot" value={bot?.status || "-"} subtitle={bot?.mode || "-"} icon={<Bot size={18} />} good={bot?.status === "running"} warn={bot?.status !== "running"} />
        <StatCard title="Net PnL" value={`${fmt(analytics?.total_net_pnl_usdt, 2)} USDT`} subtitle={`costs ${fmt(analytics?.total_costs_usdt, 2)} USDT`} icon={<BarChart3 size={18} />} good={(analytics?.total_net_pnl_usdt ?? 0) > 0} danger={(analytics?.total_net_pnl_usdt ?? 0) < 0} />
        <StatCard title="Readiness" value={readiness?.ready ? "READY" : "BLOCKED"} subtitle={`${blockers.length} blockers`} icon={<ShieldCheck size={18} />} good={readiness?.ready} danger={!readiness?.ready} />
        <StatCard title="Payments" value={readiness?.payments?.cash_collected ?? 0} subtitle={`paid ${readiness?.payments?.paid ?? 0}, pending ${readiness?.payments?.pending ?? 0}`} icon={<CreditCard size={18} />} good />
      </section>

      <section className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
        <MiniCard title="Signals" value={analytics?.total_signals ?? 0} link="/signals" />
        <MiniCard title="Winrate" value={`${analytics?.winrate ?? 0}%`} link="/analytics" />
        <MiniCard title="Active" value={analytics?.active_signals ?? 0} link="/positions" />
        <MiniCard title="Telegram SLA" value={`${readiness?.telegram_delivery?.sla_pct ?? 100}%`} link="/health" />
        <MiniCard title="Depth feed" value={orderbook?.enabled ? (orderbook?.stats?.freshest_age_sec != null ? `LIVE ${Number(orderbook.stats.freshest_age_sec).toFixed(1)}s` : "—") : "OFF"} link="/orderbook" />
        <MiniCard title="ML data" value={`${mlStats?.count ?? 0}/${mlStats?.target_for_training ?? 200}`} link="/orderbook" />
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

      {dailyQuality && (
        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <TrendingUp size={18} className="text-emerald-300" />
              <h2 className="text-xl font-semibold text-emerald-200">Daily Quality Report</h2>
              <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${dailyQuality.status === "ok" ? "bg-emerald-700 text-white" : "bg-yellow-600 text-black"}`}>
                {dailyQuality.status}
              </span>
            </div>
            <span className="text-xs text-emerald-100/50">24h window</span>
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
            <DailyCard label="Net PnL" value={`${fmt(dailyQuality.trading?.net_pnl_usdt, 2)} USDT`} good={(dailyQuality.trading?.net_pnl_usdt ?? 0) > 0} danger={(dailyQuality.trading?.net_pnl_usdt ?? 0) < 0} />
            <DailyCard label="Closed" value={dailyQuality.trading?.closed_count ?? 0} />
            <DailyCard label="Winrate" value={`${dailyQuality.trading?.winrate_pct ?? "-"}%`} good={(dailyQuality.trading?.winrate_pct ?? 0) >= 50} warn={(dailyQuality.trading?.winrate_pct ?? 100) < 45} />
            <DailyCard label="Failed Setup" value={`${dailyQuality.trading?.failed_setup_share_pct ?? 0}%`} danger={(dailyQuality.trading?.failed_setup_share_pct ?? 0) > 35} />
            <DailyCard label="TG SLA" value={`${dailyQuality.telegram_sla?.sla_pct ?? 100}%`} good={(dailyQuality.telegram_sla?.sla_pct ?? 100) >= 99} danger={(dailyQuality.telegram_sla?.sla_pct ?? 100) < 99} />
            <DailyCard label="Active" value={dailyQuality.active_signals?.total_active ?? 0} />
          </div>
          {(dailyQuality.issues?.length ?? 0) > 0 && (
            <div className="mt-4 space-y-2">
              {dailyQuality.issues.map((issue: string, i: number) => (
                <div key={i} className="rounded-lg border border-yellow-900/60 bg-yellow-950/20 px-3 py-2 text-sm text-yellow-100">
                  ⚠ {issue}
                </div>
              ))}
            </div>
          )}
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

function BookCard({ title, href, pnl, sub, total, off }: { title: string; href?: string; pnl: number; sub: string; total?: boolean; off?: boolean }) {
  const tone = pnl > 0 ? "text-emerald-300" : pnl < 0 ? "text-red-300" : "text-emerald-200";
  const valueCls = total ? (pnl >= 0 ? "text-emerald-200" : "text-red-200") : tone;
  const sign = pnl > 0 ? "+" : "";
  const box = `rounded-xl border p-4 ${total ? "border-emerald-600 bg-emerald-950/30" : "border-emerald-950 bg-black/20"}`;
  const inner = (
    <>
      <div className="flex items-center justify-between">
        <span className={`text-sm ${total ? "font-bold text-emerald-200" : "text-emerald-100/60"}`}>{title}</span>
        {off && <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-emerald-100/40">off</span>}
      </div>
      <div className={`mt-2 text-2xl font-bold ${valueCls}`}>{sign}{fmt(pnl, 2)} USDT</div>
      <div className="mt-1 text-xs text-emerald-100/50">{sub}</div>
    </>
  );
  return href ? (
    <Link href={href} className={`${box} transition hover:border-emerald-500`}>{inner}</Link>
  ) : (
    <div className={box}>{inner}</div>
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

function DailyCard({ label, value, good, warn, danger }: { label: string; value: any; good?: boolean; warn?: boolean; danger?: boolean }) {
  const cls = danger ? "text-red-300" : warn ? "text-yellow-300" : good ? "text-emerald-300" : "text-emerald-200";
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/20 p-3">
      <div className="text-xs text-emerald-100/50">{label}</div>
      <div className={`mt-1 text-lg font-bold ${cls}`}>{value ?? "-"}</div>
    </div>
  );
}
