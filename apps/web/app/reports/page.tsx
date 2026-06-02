"use client";

import { useEffect, useMemo, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import { Send, RefreshCw, Trophy, Flame, Clock, BarChart3 } from "lucide-react";

export default function ReportsPage() {
  const [period, setPeriod] = useState(24);
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState<string | null>(null);

  async function loadSummary(hours = period) {
    setLoading(true);
    try {
      const data = await apiGet(`/reports/summary?hours=${hours}`);
      setSummary(data);
    } finally {
      setLoading(false);
    }
  }

  async function changePeriod(hours: number) {
    setPeriod(hours);
    await loadSummary(hours);
  }

  async function sendReport(type: "all" | "free" | "vip" | "owner") {
    const endpointMap: Record<typeof type, string> = {
      all: "/reports/send-all",
      free: "/reports/send-free",
      vip: "/reports/send-vip",
      owner: "/reports/send-owner",
    };

    const labelMap: Record<typeof type, string> = {
      all: "Отчёты отправлены во все каналы",
      free: "FREE отчёт отправлен",
      vip: "VIP отчёт отправлен",
      owner: "Owner отчёт отправлен",
    };

    const confirmMap: Record<typeof type, string> = {
      all: "Будет отправлен отчёт во все каналы: FREE, VIP и Owner.",
      free: "Будет отправлен отчёт в FREE канал.",
      vip: "Будет отправлен отчёт в VIP канал.",
      owner: "Будет отправлен owner-отчёт.",
    };

    if (!window.confirm(`⚠️ ${confirmMap[type]}\n\nПродолжить?`)) return;

    setSending(type);

    try {
      await apiPost(`${endpointMap[type]}?hours=${period}`);
      alert(labelMap[type]);
    } finally {
      setSending(null);
    }
  }
  useEffect(() => {
    loadSummary(24);
  }, []);

  const stats = useMemo(() => {
    const closed = Number(summary?.closed_signals ?? 0);
    const wins = Number(summary?.wins ?? 0);
    const losses = Number(summary?.losses ?? 0);
    const total = Number(summary?.total_signals ?? 0);

    return {
      total,
      closed,
      wins,
      losses,
      winrate: summary?.winrate ?? 0,
      resultPct: summary?.total_result_pct ?? 0,
      hours: summary?.hours ?? period,
      netPnl: summary?.total_net_pnl_usdt,
      avgPnl: summary?.avg_net_pnl_usdt,
      costs: summary?.total_costs_usdt,
      active: summary?.active_signals,
      expired: summary?.expired_signals,
      rejected: summary?.rejected_signals,
    };
  }, [summary, period]);

  return (
    <AppShell>

        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-emerald-300">Reports</h1>
            <p className="text-sm text-emerald-100/70">
              Отчёты по сигналам для FREE, VIP и владельца
            </p>
          </div>

          <button
            onClick={() => loadSummary()}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
        </header>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-emerald-200">
                Период отчёта
              </h2>
              <p className="text-sm text-emerald-100/50">
                Активный период: {stats.hours}ч
              </p>
            </div>

            <div className="flex flex-wrap gap-3">
              <PeriodButton active={period === 24} onClick={() => changePeriod(24)}>
                24 часа
              </PeriodButton>

              <PeriodButton active={period === 168} onClick={() => changePeriod(168)}>
                7 дней
              </PeriodButton>

              <PeriodButton active={period === 720} onClick={() => changePeriod(720)}>
                30 дней
              </PeriodButton>
            </div>
          </div>
        </section>

        <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
          <StatCard title="Сигналов" value={stats.total} />
          <StatCard title="Закрыто" value={stats.closed} />
          <StatCard title="Победы" value={stats.wins} tone="positive" />
          <StatCard title="Убытки" value={stats.losses} tone="negative" />
          <StatCard title="Winrate" value={`${stats.winrate}%`} />

          <StatCard
            title="Итог %"
            value={`${stats.resultPct}%`}
            tone={Number(stats.resultPct) >= 0 ? "positive" : "negative"}
          />

          <StatCard
            title="Net PnL"
            value={formatUsdt(stats.netPnl)}
            tone={Number(stats.netPnl ?? 0) >= 0 ? "positive" : "negative"}
          />

          <StatCard
            title="Avg PnL"
            value={formatUsdt(stats.avgPnl)}
            tone={Number(stats.avgPnl ?? 0) >= 0 ? "positive" : "negative"}
          />

          <StatCard title="Costs" value={formatUsdt(stats.costs)} />
          <StatCard title="Период" value={`${stats.hours}ч`} />
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel
            title="Лучшая сделка"
            subtitle="Максимальный положительный результат за выбранный период"
            icon={<Trophy size={18} />}
          >
            <SignalPreview signal={summary?.best} mode="best" />
          </Panel>

          <Panel
            title="Худшая сделка"
            subtitle="Самая слабая сделка за выбранный период"
            icon={<Flame size={18} />}
          >
            <SignalPreview signal={summary?.worst} mode="worst" />
          </Panel>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-emerald-200">
                Отправка отчётов
              </h2>
              <p className="text-sm text-emerald-100/50">
                Отправка отчёта за текущий период: {stats.hours}ч
              </p>
            </div>

            <div className="flex items-center gap-2 text-sm text-emerald-100/50">
              <Clock size={15} />
              {loading ? "данные обновляются" : "данные актуальны"}
            </div>
          </div>

          <div className="flex flex-wrap gap-3">
            <SendButton
              label="Send All"
              tone="indigo"
              loading={sending === "all"}
              onClick={() => sendReport("all")}
            />

            <SendButton
              label="FREE"
              tone="sky"
              loading={sending === "free"}
              onClick={() => sendReport("free")}
            />

            <SendButton
              label="VIP"
              tone="emerald"
              loading={sending === "vip"}
              onClick={() => sendReport("vip")}
            />

            <SendButton
              label="Owner"
              tone="purple"
              loading={sending === "owner"}
              onClick={() => sendReport("owner")}
            />
          </div>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center gap-2">
            <BarChart3 size={18} className="text-emerald-300" />
            <h2 className="text-xl font-semibold text-emerald-200">
              Сводка отчёта
            </h2>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <InfoBox label="Всего сигналов" value={stats.total} />
            <InfoBox label="Закрытых сделок" value={stats.closed} />
            <InfoBox label="Winrate" value={`${stats.winrate}%`} />
            <InfoBox label="Победы / Убытки" value={`${stats.wins} / ${stats.losses}`} />
            <InfoBox label="Итоговый результат" value={`${stats.resultPct}%`} />
            <InfoBox label="Период отчёта" value={`${stats.hours}ч`} />
          </div>
        </section>
    </AppShell>
  );
}

function PeriodButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={
        active
          ? "rounded-xl border border-emerald-300 bg-emerald-400 px-4 py-2 font-semibold text-black shadow-lg shadow-emerald-900/30"
          : "rounded-xl border border-emerald-900 bg-emerald-950 px-4 py-2 font-semibold text-emerald-100 hover:bg-emerald-900"
      }
    >
      {children}
    </button>
  );
}

function StatCard({
  title,
  value,
  tone = "neutral",
}: {
  title: string;
  value: any;
  tone?: "neutral" | "positive" | "negative";
}) {
  const valueClass =
    tone === "positive"
      ? "text-emerald-300"
      : tone === "negative"
        ? "text-red-300"
        : "text-emerald-200";

  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${valueClass}`}>{value ?? "-"}</div>
    </div>
  );
}

function Panel({
  title,
  subtitle,
  icon,
  children,
}: {
  title: string;
  subtitle?: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="mb-4 flex items-start gap-3">
        {icon && <div className="mt-1 text-emerald-300">{icon}</div>}
        <div>
          <h2 className="text-xl font-semibold text-emerald-200">{title}</h2>
          {subtitle && <p className="text-sm text-emerald-100/50">{subtitle}</p>}
        </div>
      </div>

      {children}
    </div>
  );
}

function SignalPreview({
  signal,
  mode,
}: {
  signal: any;
  mode: "best" | "worst";
}) {
  if (!signal) {
    return (
      <div className="rounded-2xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50">
        Нет данных
      </div>
    );
  }

  const result = Number(signal.result_pct ?? 0);
  const pnl = signal.closed_net_pnl ?? signal.net_pnl ?? signal.pnl;
  const costs = signal.closed_total_cost ?? signal.total_cost;
  const plan = signal.plan || signal.plan_json || {};
  const entry = signal.entry_zone || signal.entry_zone_json;
  const tp = signal.tp || signal.tp_json;

  return (
    <div className="rounded-2xl border border-emerald-950 bg-black/20 p-4">
      <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-lg font-bold text-emerald-300">
              #{signal.id} {signal.symbol}
            </span>
            <StatusBadge status={signal.status} />
            <GradeBadge grade={signal.grade} />
          </div>

          <div className="mt-1 text-xs text-emerald-100/50">
            {signal.rationale || signal.closed_reason || "report_signal"}
          </div>
        </div>

        <div
          className={
            result >= 0
              ? "text-right text-xl font-bold text-emerald-300"
              : "text-right text-xl font-bold text-red-300"
          }
        >
          {result}%
          {pnl !== null && pnl !== undefined && (
            <div className={Number(pnl) >= 0 ? "text-sm text-emerald-300" : "text-sm text-red-300"}>
              {formatUsdt(pnl)}
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MiniMetric label="Side" value={signal.side || "-"} />
        <MiniMetric label="Entry" value={formatEntry(entry)} />
        <MiniMetric label="Stop" value={signal.stop_price ?? "-"} />
        <MiniMetric label="TP" value={formatTp(tp)} />

        <MiniMetric label="Qty" value={signal.qty ?? plan.qty ?? "-"} />
        <MiniMetric label="Margin" value={formatUsdt(signal.required_margin ?? plan.required_margin)} />
        <MiniMetric label="RR TP2" value={signal.net_rr_tp2 ?? plan.net_rr_tp2 ?? "-"} />
        <MiniMetric label="Conf" value={signal.confidence != null ? `${signal.confidence}%` : "-"} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
        <PnlLine label="TP1" value={signal.net_pnl_tp1 ?? plan.net_pnl_tp1} />
        <PnlLine label="TP2" value={signal.net_pnl_tp2 ?? plan.net_pnl_tp2} />
        <PnlLine label="SL" value={signal.net_pnl_stop ?? plan.net_pnl_stop} />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
        <InfoBox label="Exit" value={signal.closed_exit_price ?? "-"} />
        <InfoBox label="Costs" value={formatUsdt(costs)} />
        <InfoBox label="Close reason" value={signal.closed_reason || "-"} />
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-emerald-950 pt-3 text-xs text-emerald-100/50">
        <span>{mode === "best" ? "best signal in period" : "worst signal in period"}</span>
        <span>{signal.created_at ? formatDate(signal.created_at) : "-"}</span>
      </div>
    </div>
  );
}

function MiniMetric({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/30 p-3">
      <div className="text-xs text-emerald-100/50">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold text-emerald-100">
        {value ?? "-"}
      </div>
    </div>
  );
}

function PnlLine({ label, value }: { label: string; value: any }) {
  const n = Number(value);

  return (
    <div className="rounded-xl border border-emerald-950 bg-black/30 p-3">
      <div className="text-xs text-emerald-100/50">{label}</div>
      <div
        className={
          !Number.isFinite(n)
            ? "mt-1 text-sm font-semibold text-emerald-100"
            : n >= 0
              ? "mt-1 text-sm font-semibold text-emerald-300"
              : "mt-1 text-sm font-semibold text-red-300"
        }
      >
        {value === null || value === undefined ? "-" : `${value} USDT`}
      </div>
    </div>
  );
}

function InfoBox({ label, value }: { label: string; value: any }) {
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/20 p-3">
      <div className="text-xs text-emerald-100/50">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold text-emerald-100">
        {value ?? "-"}
      </div>
    </div>
  );
}

function SendButton({
  label,
  tone,
  loading,
  onClick,
}: {
  label: string;
  tone: "indigo" | "sky" | "emerald" | "purple";
  loading: boolean;
  onClick: () => void;
}) {
  const cls: Record<typeof tone, string> = {
    indigo: "bg-indigo-400 hover:bg-indigo-300",
    sky: "bg-sky-400 hover:bg-sky-300",
    emerald: "bg-emerald-400 hover:bg-emerald-300",
    purple: "bg-purple-400 hover:bg-purple-300",
  };

  return (
    <button
      onClick={onClick}
      disabled={loading}
      className={`flex items-center gap-2 rounded-xl px-4 py-2 font-semibold text-black disabled:cursor-not-allowed disabled:opacity-60 ${cls[tone]}`}
    >
      <Send size={16} />
      {loading ? "Отправка..." : label}
    </button>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  const cls =
    status === "closed"
      ? "bg-emerald-800 text-emerald-100"
      : status === "opened"
        ? "bg-cyan-700 text-white"
        : status === "tp1"
          ? "bg-blue-700 text-white"
          : status === "expired"
            ? "bg-yellow-700 text-black"
            : status === "rejected"
              ? "bg-red-800 text-white"
              : "bg-emerald-950 text-emerald-200";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {status || "-"}
    </span>
  );
}

function GradeBadge({ grade }: { grade?: string | null }) {
  const cls =
    grade === "A+"
      ? "bg-emerald-500 text-black"
      : grade === "A"
        ? "bg-emerald-800 text-emerald-100"
        : grade === "B"
          ? "bg-yellow-600 text-black"
          : grade === "C"
            ? "bg-red-700 text-white"
            : "bg-emerald-950 text-emerald-200";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {grade || "-"}
    </span>
  );
}

function formatEntry(entry: any) {
  if (!entry) return "-";

  if (Array.isArray(entry)) {
    return `${entry[0]} - ${entry[1]}`;
  }

  if (entry.from !== undefined || entry.to !== undefined) {
    return `${entry.from ?? "-"} - ${entry.to ?? "-"}`;
  }

  return String(entry);
}

function formatTp(tp: any) {
  if (!tp) return "-";

  if (Array.isArray(tp)) {
    return tp.join(" / ");
  }

  if (tp.tp1 !== undefined || tp.tp2 !== undefined) {
    return `${tp.tp1 ?? "-"} / ${tp.tp2 ?? "-"}`;
  }

  return String(tp);
}

function formatUsdt(value: any) {
  if (value === null || value === undefined || value === "") return "-";

  const n = Number(value);

  if (!Number.isFinite(n)) return String(value);

  return `${n.toFixed(4)} USDT`;
}

function formatDate(value: string | null | undefined) {
  if (!value) return "-";

  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;

  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}