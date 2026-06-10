"use client";

import { useEffect, useRef, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import { Activity, Bot, Database, RefreshCw, Radio, ShieldAlert, ShieldCheck, Wifi } from "lucide-react";

export default function HealthPage() {
  const [health, setHealth] = useState<any>(null);
  const [readiness, setReadiness] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [killSwitchSmoke, setKillSwitchSmoke] = useState<any>(null);
  const loadingRef = useRef(false);

  async function loadAll() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      const [healthRes, readinessRes] = await Promise.all([
        apiGet("/system/health"),
        apiGet("/system/readiness"),
      ]);
      setHealth(healthRes);
      setReadiness(readinessRes);
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  async function testTelegramOwner() {
    if (!window.confirm("⚠️ Будет отправлен тестовый Telegram alert владельцу.\n\nПродолжить?")) return;
    await apiPost("/system/test-telegram-owner");
    await loadAll();
    alert("Owner Telegram test отправлен");
  }

  async function setKillSwitch(enabled: boolean) {
    const text = enabled
      ? "⚠️ Включить kill switch и остановить робота до ручного отключения?"
      : "Отключить kill switch и разрешить запуск при прохождении safety gates?";
    if (!window.confirm(text)) return;
    await apiPost("/system/kill-switch", { enabled, reason: enabled ? "owner_health_page" : "owner_resume" });
    await loadAll();
  }

  async function runKillSwitchSmoke() {
    if (!window.confirm("Запустить dry-run проверку kill switch? Состояние будет откатано транзакцией.")) return;
    const result = await apiPost("/system/kill-switch-smoke", { reason: "owner_health_page_smoke" });
    setKillSwitchSmoke(result);
    await loadAll();
  }

  useEffect(() => {
    loadAll();
    const timer = setInterval(loadAll, 5000);
    return () => clearInterval(timer);
  }, []);

  const bot = health?.bot;
  const market = readiness?.market_connectivity || health?.market;
  const exchangeReconciliation = readiness?.exchange_reconciliation || health?.exchange_reconciliation || {};
  const loops = health?.loops;
  const delivery = readiness?.telegram_delivery || health?.telegram_delivery || {};
  // /system/readiness is the product go-live source of truth: it includes
  // profit and Telegram SLA gates that /system/health may not treat as blockers.
  const production = readiness || health?.production_readiness || {};
  const liveSafety = readiness?.live_safety || health?.live_safety || {};
  const mlOutcomes = readiness?.ml_outcomes || health?.ml_outcomes || {};
  const fundingArb = readiness?.funding_arb || health?.funding_arb || {};
  const blockers = readiness?.blockers || health?.production_readiness?.blockers || [];

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-emerald-300">System Health</h1>
          <p className="mt-2 text-sm text-emerald-100/70">
            Технический мониторинг API, фоновых циклов, рынка, Telegram delivery и production blockers.
          </p>
        </div>

        <div className="flex flex-wrap gap-3">
          <button
            onClick={loadAll}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
          <button
            onClick={testTelegramOwner}
            className="flex items-center gap-2 rounded-xl bg-cyan-700 px-4 py-2 font-semibold hover:bg-cyan-600"
          >
            <Radio size={16} />
            Telegram Test
          </button>
          <button
            onClick={() => setKillSwitch(!liveSafety?.kill_switch_enabled)}
            className={
              liveSafety?.kill_switch_enabled
                ? "flex items-center gap-2 rounded-xl bg-emerald-700 px-4 py-2 font-semibold hover:bg-emerald-600"
                : "flex items-center gap-2 rounded-xl bg-red-700 px-4 py-2 font-semibold hover:bg-red-600"
            }
          >
            <ShieldAlert size={16} />
            {liveSafety?.kill_switch_enabled ? "Resume" : "Kill switch"}
          </button>
          <button onClick={runKillSwitchSmoke} className="flex items-center gap-2 rounded-xl bg-purple-700 px-4 py-2 font-semibold hover:bg-purple-600">
            <ShieldCheck size={16} />
            Kill smoke
          </button>
        </div>
      </header>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <HealthCard icon={<Database size={18} />} title="API" value={health?.api?.ok ? "online" : "offline"} status={health?.api?.ok ? "good" : "bad"} subtitle={`${health?.api?.env || "-"} / ${health?.api?.mode || "-"}`} />
        <HealthCard icon={<Bot size={18} />} title="Bot" value={bot?.status || "-"} status={bot?.status === "running" ? "good" : "warn"} subtitle={bot?.mode || "-"} />
        <HealthCard icon={<Wifi size={18} />} title="Market" value={market?.ok ? "online" : "offline"} status={market?.ok ? "good" : "bad"} subtitle={`${market?.source || "-"} / ${formatNumber(market?.last)}`} />
        <HealthCard icon={<ShieldCheck size={18} />} title="Readiness" value={production?.ready ? "ready" : "blocked"} status={production?.ready ? "good" : "bad"} subtitle={`${blockers.length} blockers`} />
        <HealthCard icon={<ShieldAlert size={18} />} title="Live safety" value={liveSafety?.blocked ? "blocked" : "clear"} status={liveSafety?.blocked ? "bad" : "good"} subtitle={`day loss ${liveSafety?.daily_loss_pct ?? 0}% / max ${liveSafety?.max_daily_loss_pct ?? "-"}%`} />
      </section>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        <Panel title="Background loops">
          <LoopRow title="Robot Loop" enabled={loops?.robot_loop?.enabled} created={loops?.robot_loop?.task_created} done={loops?.robot_loop?.task_done} />
          <LoopRow title="Subscription Loop" enabled={loops?.subscription_loop?.enabled} created={loops?.subscription_loop?.task_created} done={loops?.subscription_loop?.task_done} />
          <LoopRow title="Telegram Delivery" enabled={loops?.telegram_delivery_loop?.enabled} created={loops?.telegram_delivery_loop?.task_created} done={loops?.telegram_delivery_loop?.task_done} />
          <LoopRow title="Payment Reconciliation" enabled={loops?.payment_reconciliation_loop?.enabled} created={loops?.payment_reconciliation_loop?.task_created} done={loops?.payment_reconciliation_loop?.task_done} />
          <LoopRow title="Funding Arb Loop" enabled={loops?.funding_arb_loop?.enabled} created={loops?.funding_arb_loop?.task_created} done={loops?.funding_arb_loop?.task_done} />
        </Panel>

        <Panel title="Live safety">
          <InfoRow label="Kill switch" value={liveSafety?.kill_switch_enabled ? "enabled" : "disabled"} danger={liveSafety?.kill_switch_enabled} />
          <InfoRow label="24h Net PnL" value={`${liveSafety?.daily_net_pnl_usdt ?? 0} USDT`} danger={(liveSafety?.daily_net_pnl_usdt ?? 0) < 0} />
          <InfoRow label="Daily loss" value={`${liveSafety?.daily_loss_pct ?? 0}%`} danger={liveSafety?.daily_loss_blocked} />
          <InfoRow label="Max daily loss" value={`${liveSafety?.max_daily_loss_pct ?? "-"}%`} />
          {liveSafety?.kill_switch_reason && <InfoRow label="Reason" value={liveSafety.kill_switch_reason} danger={liveSafety?.kill_switch_enabled} />}
          {killSwitchSmoke && <InfoRow label="Smoke" value={killSwitchSmoke?.status === "ok" ? "passed dry-run" : (killSwitchSmoke?.error || "failed")} danger={killSwitchSmoke?.status !== "ok"} />}
        </Panel>

        <Panel title="ML outcomes">
          <InfoRow label="Status" value={mlOutcomes?.freshness_status || mlOutcomes?.status || "unknown"} danger={Boolean(mlOutcomes?.stale) || !["ok", "empty"].includes(mlOutcomes?.status)} />
          <InfoRow label="Rows" value={mlOutcomes?.total ?? 0} />
          <InfoRow label="Latest logged" value={formatDate(mlOutcomes?.latest_logged_at)} danger={Boolean(mlOutcomes?.stale)} />
          <InfoRow label="Age" value={mlOutcomes?.latest_age_hours == null ? "-" : `${mlOutcomes.latest_age_hours}h / ${mlOutcomes.latest_age_days ?? "-"}d`} danger={Boolean(mlOutcomes?.stale)} />
          <InfoRow label="Stale after" value={`${mlOutcomes?.stale_after_hours ?? 72}h`} />
          <InfoRow label="Parse errors" value={mlOutcomes?.parse_errors ?? 0} danger={(mlOutcomes?.parse_errors ?? 0) > 0} />
          <InfoRow label="Source" value={shortPath(mlOutcomes?.source_path)} />
        </Panel>

        <Panel title="Telegram delivery 24h">
          <InfoRow label="SLA" value={`${delivery?.sla_pct ?? 100}%`} />
          <InfoRow label="VIP SLA" value={`${delivery?.vip_sla_pct ?? 100}%`} danger={(delivery?.vip_failed ?? 0) > 0} />
          <InfoRow label="Sent" value={delivery?.sent ?? 0} />
          <InfoRow label="VIP sent" value={delivery?.vip_sent ?? 0} />
          <InfoRow label="Queued" value={delivery?.queued ?? 0} danger={(delivery?.queued ?? 0) > 0} />
          <InfoRow label="VIP queued" value={delivery?.vip_queued ?? 0} danger={(delivery?.vip_queued ?? 0) > 0} />
          <InfoRow label="Retryable" value={delivery?.retryable ?? 0} danger={(delivery?.retryable ?? 0) > 0} />
          <InfoRow label="Failed" value={delivery?.failed ?? 0} danger={(delivery?.failed ?? 0) > 0} />
          {delivery?.last_error && <InfoRow label="Last error" value={delivery.last_error} danger />}
        </Panel>

        <Panel title="HTX funding arb">
          <InfoRow label="Status" value={fundingArb?.enabled ? "enabled" : "disabled"} danger={!fundingArb?.enabled} />
          <InfoRow label="Symbols" value={(fundingArb?.symbols || []).join(", ") || "-"} />
          <InfoRow label="Open hedges" value={fundingArb?.open_positions ?? 0} danger={(fundingArb?.open_positions ?? 0) > 0} />
          <InfoRow label="Closed hedges" value={fundingArb?.closed_positions ?? 0} />
          <InfoRow label="Realized P&L" value={`${fundingArb?.realized_pnl ?? 0} USDT`} danger={(fundingArb?.realized_pnl ?? 0) < 0} />
          <InfoRow label="Latest scans" value={(fundingArb?.latest_opportunities || []).length} />
        </Panel>

        <Panel title="Market connectivity">
          <InfoRow label="Status" value={market?.ok ? "ok" : "blocked"} danger={!market?.ok} />
          <InfoRow label="Symbol" value={market?.symbol || "-"} />
          <InfoRow label="Last" value={formatNumber(market?.last)} />
          <InfoRow label="Spread" value={market?.spread_pct == null ? "-" : `${market.spread_pct}%`} danger={(market?.spread_pct ?? 0) > 0.75} />
          <InfoRow label="Latency" value={market?.latency_ms == null ? "-" : `${market.latency_ms} ms`} danger={(market?.latency_ms ?? 0) > 5000} />
          <InfoRow label="Source" value={market?.source || "-"} danger={market?.source === "mock"} />
          {(market?.blockers || []).map((blocker: string, idx: number) => (
            <InfoRow key={idx} label="Breaker" value={blocker} danger />
          ))}
          {market?.error && <InfoRow label="Error" value={market.error} danger />}
        </Panel>

        <Panel title="Exchange reconciliation">
          <InfoRow label="Status" value={exchangeReconciliation?.status || "unknown"} danger={!exchangeReconciliation?.ok && exchangeReconciliation?.status !== "disabled"} />
          <InfoRow label="Enabled" value={String(Boolean(exchangeReconciliation?.enabled))} danger={Boolean(exchangeReconciliation?.live_enabled) && !exchangeReconciliation?.enabled} />
          <InfoRow label="Local orders" value={exchangeReconciliation?.counts?.local_open_orders ?? 0} />
          <InfoRow label="Exchange orders" value={exchangeReconciliation?.counts?.exchange_open_orders ?? 0} />
          <InfoRow label="Local positions" value={exchangeReconciliation?.counts?.local_live_positions ?? 0} />
          <InfoRow label="Exchange positions" value={exchangeReconciliation?.counts?.exchange_positions ?? 0} />
          {(exchangeReconciliation?.blockers || []).map((blocker: string, idx: number) => (
            <InfoRow key={idx} label="Breaker" value={blocker} danger />
          ))}
          {exchangeReconciliation?.error && <InfoRow label="Error" value={exchangeReconciliation.error} danger />}
        </Panel>
      </section>

      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-semibold text-emerald-200">
          <Activity size={18} />
          Production blockers
        </h2>
        {blockers.length > 0 ? (
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {blockers.map((blocker: string, idx: number) => (
              <div key={idx} className="rounded-xl border border-red-900/70 bg-red-950/30 p-3 text-sm text-red-100">
                {blocker}
              </div>
            ))}
          </div>
        ) : (
          <Empty text="Блокеров нет" />
        )}
      </section>
    </AppShell>
  );
}

function HealthCard({ icon, title, value, subtitle, status }: { icon: any; title: string; value: any; subtitle: string; status: "good" | "warn" | "bad" }) {
  const tone = status === "good" ? "text-emerald-300" : status === "warn" ? "text-yellow-300" : "text-red-300";
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="mb-3 flex items-center justify-between text-emerald-100/60">
        <span className="text-sm">{title}</span>
        {icon}
      </div>
      <div className={`text-2xl font-bold ${tone}`}>{value}</div>
      <div className="mt-1 text-xs text-emerald-100/50">{subtitle}</div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: any }) {
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <h2 className="mb-4 text-xl font-semibold text-emerald-200">{title}</h2>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function LoopRow({ title, enabled, created, done }: { title: string; enabled: any; created: any; done: any }) {
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/20 p-3">
      <InfoRow label={title} value={enabled ? "enabled" : "disabled"} danger={!enabled} />
      <InfoRow label="Task created" value={String(Boolean(created))} />
      <InfoRow label="Task done" value={String(Boolean(done))} danger={Boolean(done)} />
    </div>
  );
}

function InfoRow({ label, value, danger }: { label: string; value: any; danger?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-emerald-950 py-2 text-sm last:border-b-0">
      <span className="text-emerald-100/50">{label}</span>
      <span className={danger ? "text-right font-semibold text-red-300" : "text-right font-semibold text-emerald-100"}>{value}</span>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="rounded-xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50">{text}</div>;
}

function formatNumber(value: any) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(4);
}

function formatDate(value: any) {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toISOString().replace("T", " ").replace(".000Z", "Z");
}

function shortPath(value: any) {
  if (!value) return "-";
  const text = String(value);
  return text.length > 36 ? `...${text.slice(-33)}` : text;
}
