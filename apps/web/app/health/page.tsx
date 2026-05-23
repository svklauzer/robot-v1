"use client";

import { useEffect, useRef, useState } from "react";
import Nav from "../../components/Nav";
import { apiGet, apiPost } from "../../lib/api";
import {
  Activity,
  Bot,
  Database,
  RefreshCw,
  Radio,
  ShieldCheck,
  Signal,
  Wallet,
} from "lucide-react";

export default function HealthPage() {
  const [health, setHealth] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [signals, setSignals] = useState<any[]>([]);
  const [events, setEvents] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const loadingRef = useRef(false);

  async function loadAll() {
    if (loadingRef.current) return;

    loadingRef.current = true;
    setLoading(true);

    try {
      const [healthRes, analyticsRes, positionsRes, signalsRes, eventsRes] =
        await Promise.all([
          apiGet("/system/health"),
          apiGet("/analytics/summary"),
          apiGet("/positions"),
          apiGet("/signals?limit=10&offset=0"),
          apiGet("/intelligence/events?limit=10"),
        ]);

      setHealth(healthRes);
      setAnalytics(analyticsRes);

      setPositions(Array.isArray(positionsRes) ? positionsRes : []);

      if (Array.isArray(signalsRes)) {
        setSignals(signalsRes);
      } else {
        setSignals(signalsRes?.items || []);
      }

      if (Array.isArray(eventsRes)) {
        setEvents(eventsRes);
      } else {
        setEvents(eventsRes?.items || []);
      }
    } finally {
      loadingRef.current = false;
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

  async function testTelegramOwner() {
    if (!window.confirm("⚠️ Будет отправлен тестовый Telegram alert владельцу.\n\nПродолжить?")) return;

    await apiPost("/system/test-telegram-owner");
    alert("Owner Telegram test отправлен");
  }

  useEffect(() => {
    loadAll();
    const timer = setInterval(loadAll, 5000);
    return () => clearInterval(timer);
  }, []);

  const bot = health?.bot;
  const market = health?.market;
  const loops = health?.loops;
  const healthSignals = health?.signals;
  const subscribers = health?.subscribers;
  const exposure = analytics?.exposure || {};

  const openPositions = Array.isArray(positions)
    ? positions.filter((p: any) => p.status === "open")
    : [];

  const latestSignal = signals?.[0];
  const latestEvent = events?.[0];

  return (
    <main className="min-h-screen p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <Nav />

        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-emerald-300">
              System Health
            </h1>
            <p className="text-sm text-emerald-100/70">
              Монитор состояния API, робота, рынка, циклов и риска
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
          </div>
        </header>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <HealthCard
            icon={<Database size={18} />}
            title="API"
            value={health?.api?.ok ? "online" : "offline"}
            status={health?.api?.ok ? "good" : "bad"}
            subtitle={`${health?.api?.env || "-"} / ${health?.api?.mode || "-"}`}
          />

          <HealthCard
            icon={<Bot size={18} />}
            title="Bot"
            value={bot?.status || "-"}
            status={bot?.status === "running" ? "good" : "warn"}
            subtitle={bot?.mode || "-"}
          />

          <HealthCard
            icon={<Activity size={18} />}
            title="Market"
            value={market?.ok ? "online" : "offline"}
            status={market?.ok ? "good" : "bad"}
            subtitle={`${market?.source || "-"} / ${formatNumber(market?.last)}`}
          />

          <HealthCard
            icon={<Signal size={18} />}
            title="Active Signals"
            value={analytics?.active_signals ?? healthSignals?.opened ?? 0}
            status={(analytics?.active_signals ?? 0) > 0 ? "warn" : "good"}
            subtitle={`opened ${healthSignals?.opened ?? 0}, tp1 ${healthSignals?.tp1 ?? 0}`}
          />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-5">
          <Metric title="Total Signals" value={analytics?.total_signals ?? healthSignals?.total ?? 0} />
          <Metric title="Closed" value={analytics?.closed_signals ?? healthSignals?.closed ?? 0} />
          <Metric title="Expired" value={analytics?.expired_signals ?? healthSignals?.expired ?? 0} />
          <Metric title="Rejected" value={analytics?.rejected_signals ?? 0} />
          <Metric title="Winrate" value={`${analytics?.winrate ?? 0}%`} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-5">
          <Metric title="Net PnL" value={`${formatNumber(analytics?.total_net_pnl_usdt)} USDT`} danger={analytics?.total_net_pnl_usdt < 0} />
          <Metric title="Avg PnL" value={`${formatNumber(analytics?.avg_net_pnl_usdt)} USDT`} danger={analytics?.avg_net_pnl_usdt < 0} />
          <Metric title="Costs" value={`${formatNumber(analytics?.total_costs_usdt)} USDT`} />
          <Metric title="Wins" value={analytics?.wins ?? 0} />
          <Metric title="Losses" value={analytics?.losses ?? 0} danger={(analytics?.losses ?? 0) > (analytics?.wins ?? 0)} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <HealthCard
            icon={<Wallet size={18} />}
            title="Used Margin"
            value={`${formatNumber(exposure?.used_margin)} USDT`}
            status={(exposure?.used_margin ?? 0) > 0 ? "warn" : "good"}
            subtitle={`free ${formatNumber(exposure?.free_margin)} USDT`}
          />

          <HealthCard
            icon={<ShieldCheck size={18} />}
            title="Max Margin"
            value={`${formatNumber(exposure?.max_allowed_margin)} USDT`}
            status="good"
            subtitle="risk exposure limit"
          />

          <HealthCard
            icon={<Activity size={18} />}
            title="Open Positions"
            value={openPositions.length}
            status={openPositions.length > 0 ? "warn" : "good"}
            subtitle={`total positions ${positions.length}`}
          />

          <HealthCard
            icon={<Signal size={18} />}
            title="Subscribers"
            value={subscribers?.active ?? 0}
            status={(subscribers?.active ?? 0) > 0 ? "good" : "warn"}
            subtitle={`expired ${subscribers?.expired ?? 0}, blocked ${subscribers?.blocked ?? 0}`}
          />
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-emerald-200">
                Управление роботом
              </h2>
              <p className="text-sm text-emerald-100/50">
                Быстрые действия для проверки состояния
              </p>
            </div>

            <div className="flex flex-wrap gap-3">
              <button
                onClick={startBot}
                className="rounded-xl bg-emerald-500 px-4 py-2 font-semibold text-black hover:bg-emerald-400"
              >
                Start Bot
              </button>

              <button
                onClick={stopBot}
                className="rounded-xl bg-red-500 px-4 py-2 font-semibold text-black hover:bg-red-400"
              >
                Stop Bot
              </button>
            </div>
          </div>
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Panel title="Background Loops">
            <LoopRow
              title="Robot Loop"
              enabled={loops?.robot_loop?.enabled}
              created={loops?.robot_loop?.task_created}
              done={loops?.robot_loop?.task_done}
            />
            <LoopRow
              title="Subscription Loop"
              enabled={loops?.subscription_loop?.enabled}
              created={loops?.subscription_loop?.task_created}
              done={loops?.subscription_loop?.task_done}
            />
          </Panel>

          <Panel title="Market Status">
            <InfoRow label="Status" value={market?.ok ? "ok" : "error"} />
            <InfoRow label="Symbol" value={market?.symbol || "-"} />
            <InfoRow label="Last" value={formatNumber(market?.last)} />
            <InfoRow label="Source" value={market?.source || "-"} />
            {market?.error && <InfoRow label="Error" value={market.error} danger />}
          </Panel>
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Panel title="Latest Signal">
            {latestSignal ? (
              <>
                <InfoRow label="ID" value={latestSignal.id} />
                <InfoRow label="Symbol" value={latestSignal.symbol} />
                <InfoRow label="Status" value={latestSignal.status} />
                <InfoRow label="Grade" value={latestSignal.grade || "-"} />
                <InfoRow label="Reason" value={latestSignal.rationale || "-"} />
                <InfoRow label="Created" value={formatTime(latestSignal.created_at)} />
              </>
            ) : (
              <Empty text="Сигналов пока нет" />
            )}
          </Panel>

          <Panel title="Latest Intelligence Event">
            {latestEvent ? (
              <>
                <InfoRow label="Symbol" value={latestEvent.symbol} />
                <InfoRow label="Status" value={latestEvent.status} />
                <InfoRow label="Decision" value={latestEvent.decision} />
                <InfoRow label="Action" value={latestEvent.action || "-"} />
                <InfoRow label="Regime" value={latestEvent.regime || "-"} />
                <InfoRow label="Time" value={formatTime(latestEvent.created_at)} />
              </>
            ) : (
              <Empty text="Событий пока нет" />
            )}
          </Panel>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-xl font-semibold text-emerald-200">
              Open Positions
            </h2>
            <span className="text-sm text-emerald-100/50">
              {openPositions.length} active
            </span>
          </div>

          {openPositions.length > 0 ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {openPositions.map((p: any) => (
                <div
                  key={p.id}
                  className="rounded-2xl border border-emerald-950 bg-black/20 p-4"
                >
                  <div className="mb-3 flex items-center justify-between">
                    <div className="text-lg font-bold text-emerald-300">
                      {p.symbol}
                    </div>
                    <span className="rounded-lg bg-emerald-900 px-2 py-1 text-xs font-semibold text-emerald-100">
                      {p.status}
                    </span>
                  </div>

                  <InfoRow label="Side" value={p.side} />
                  <InfoRow label="Qty" value={p.qty} />
                  <InfoRow label="Entry" value={p.entry_price} />
                  <InfoRow label="Mark" value={p.mark_price} />
                  <InfoRow
                    label="Unrealized PnL"
                    value={`${formatNumber(p.unrealized_pnl)} USDT`}
                    danger={Number(p.unrealized_pnl) < 0}
                  />
                  <InfoRow label="Signal ID" value={p.signal_id ?? "-"} />
                </div>
              ))}
            </div>
          ) : (
            <Empty text="Открытых позиций нет" />
          )}
        </section>
      </div>
    </main>
  );
}

function HealthCard({
  icon,
  title,
  value,
  subtitle,
  status,
}: {
  icon: any;
  title: string;
  value: any;
  subtitle?: string;
  status: "good" | "warn" | "bad";
}) {
  const statusClass =
    status === "good"
      ? "text-emerald-300"
      : status === "warn"
        ? "text-yellow-300"
        : "text-red-300";

  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-emerald-400">{icon}</div>
        <StatusDot status={status} />
      </div>
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${statusClass}`}>{value}</div>
      {subtitle && (
        <div className="mt-2 text-xs text-emerald-100/50">{subtitle}</div>
      )}
    </div>
  );
}

function Metric({
  title,
  value,
  danger = false,
}: {
  title: string;
  value: any;
  danger?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${danger ? "text-red-300" : "text-emerald-200"}`}>
        {value}
      </div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: any }) {
  return (
    <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <h2 className="mb-4 text-xl font-semibold text-emerald-200">{title}</h2>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function InfoRow({
  label,
  value,
  danger = false,
}: {
  label: string;
  value: any;
  danger?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-emerald-950 py-2 text-sm">
      <span className="text-emerald-100/60">{label}</span>
      <span className={`text-right ${danger ? "text-red-300" : "text-emerald-100"}`}>
        {String(value ?? "-")}
      </span>
    </div>
  );
}

function LoopRow({
  title,
  enabled,
  created,
  done,
}: {
  title: string;
  enabled: boolean;
  created: boolean;
  done: boolean;
}) {
  const ok = enabled && created && done === false;

  return (
    <div className="rounded-xl border border-emerald-950 bg-black/20 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="font-semibold text-emerald-200">{title}</div>
        <StatusDot status={ok ? "good" : "bad"} />
      </div>

      <InfoRow label="Enabled" value={String(enabled)} />
      <InfoRow label="Task created" value={String(created)} />
      <InfoRow label="Task done" value={String(done)} danger={done === true} />
    </div>
  );
}

function StatusDot({ status }: { status: "good" | "warn" | "bad" }) {
  const cls =
    status === "good"
      ? "bg-emerald-400"
      : status === "warn"
        ? "bg-yellow-400"
        : "bg-red-500";

  return <span className={`h-3 w-3 rounded-full ${cls}`} />;
}

function Empty({ text }: { text: string }) {
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50">
      {text}
    </div>
  );
}

function formatNumber(value: any) {
  const n = Number(value);

  if (!Number.isFinite(n)) return "-";

  return n.toFixed(2);
}

function formatTime(value: string | null | undefined) {
  if (!value) return "-";

  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;

  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}