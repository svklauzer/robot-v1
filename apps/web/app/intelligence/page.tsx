"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { RefreshCw } from "lucide-react";

const IMPORTANT_DECISIONS = [
  "ready_to_publish",
  "published_signal_created",
  "signal_published",

  "active_signal_already_exists",

  "wait_better_entry_rr",
  "candidate_but_wait_confirmation",

  "net_rr_too_low",
  "required_margin_exceeds_balance",
  "required_margin_exceeds_free_margin",
  "max_active_signals_reached",
  "trade_plan_rejected",

  "quality_grade_too_low",
  "setup_quality_too_low",

  "watch_long",
  "watch_short",
  "watch_expired",
  "watch_cooldown",

  "short_candidate_but_shorts_disabled",
  "skip_no_trade_conditions",
];

export default function IntelligencePage() {
  const [scanData, setScanData] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [scanStatusFilter, setScanStatusFilter] = useState("actionable");

  const [analytics, setAnalytics] = useState<any>(null);
  const [funnel, setFunnel] = useState<any>(null);

  const loadingRef = useRef(false);

  async function loadScan() {
    if (loadingRef.current) return;

    loadingRef.current = true;
    setLoading(true);

    try {
      const [scanResponse, eventsResponse, analyticsResponse, funnelResponse] = await Promise.all([
        apiGet("/intelligence/scan"),
        apiGet("/intelligence/events?limit=80"),
        apiGet("/analytics/summary"),
        apiGet("/intelligence/funnel?limit=120"),
      ]);

      if (scanResponse?.status !== "busy") {
        if (scanResponse?.status === "ok" && Array.isArray(scanResponse.results)) {
          setScanData(scanResponse);
        }
      }

      if (Array.isArray(eventsResponse)) {
        setEvents(eventsResponse);
      } else if (Array.isArray(eventsResponse?.items)) {
        setEvents(eventsResponse.items);
      } else if (Array.isArray(eventsResponse?.events)) {
        setEvents(eventsResponse.events);
      }

      setAnalytics(analyticsResponse);
      setFunnel(funnelResponse);
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  useEffect(() => {
    loadScan();
    const timer = setInterval(loadScan, 10000);
    return () => clearInterval(timer);
  }, []);

  const results = Array.isArray(scanData?.results) ? scanData.results : [];

  const filteredResults = useMemo(() => {
    if (scanStatusFilter === "all") return results;

    return results.filter((item: any) => {
      const status = String(item.status || "").toLowerCase();
      const decision = String(item.decision || "").toLowerCase();

      if (scanStatusFilter === "actionable") {
        return status !== "hold" || decision !== "skip_no_trade_conditions";
      }

      return status === scanStatusFilter;
    });
  }, [results, scanStatusFilter]);

  const importantEvents = useMemo(() => {
    return groupDecisionEvents(
      events.filter((e: any) => IMPORTANT_DECISIONS.includes(e.decision))
    );
  }, [events]);

  const stats = useMemo(() => {
    const avgConfidence = avg(
      results
        .map((r: any) => Number(r.effective_confidence ?? r.confidence_hint))
        .filter((v: number) => Number.isFinite(v))
    );

    const avgSetup = avg(
      results
        .map((r: any) => Number(r.setup_quality?.final_score))
        .filter((v: number) => Number.isFinite(v))
    );

    const exposureBlocked = importantEvents.filter(
      (e: any) =>
        e.decision === "active_signal_already_exists" ||
        e.decision === "required_margin_exceeds_free_margin" ||
        e.decision === "max_active_signals_reached"
    ).length;

    return {
      total: results.length,
      watch: results.filter((r: any) => r.status === "watch").length,
      hold: results.filter((r: any) => r.status === "hold").length,
      wait: results.filter((r: any) => r.status === "wait").length,
      candidate: results.filter((r: any) => r.status === "candidate").length,

      published: results.filter((r: any) => r.status === "published").length,
      activeSignals: analytics?.active_signals ?? 0,

      blocked: results.filter((r: any) => r.status === "blocked").length,
      rejected: results.filter((r: any) => r.status === "rejected").length,
      avgConfidence,
      avgSetup,
      exposureBlocked,
    };
  }, [results, importantEvents]);

  return (
    <AppShell>

        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-emerald-300">
              Market Intelligence
            </h1>
            <p className="text-sm text-emerald-100/70">
              Диагностика решений робота по монетам, сетапам, watch-сценариям и ExposureGuard
            </p>
          </div>

          <button
            onClick={loadScan}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
        </header>

        <section className="grid grid-cols-2 gap-4 md:grid-cols-5">
          <Card title="Symbols" value={stats.total} />
          <Card title="Watch" value={stats.watch} />
          <Card title="Hold" value={stats.hold} />
          <Card title="Wait" value={stats.wait} />
          <Card title="Candidates" value={stats.candidate} />
          <Card title="Scan Published" value={stats.published} />
          <Card title="Active Signals" value={stats.activeSignals} />
          <Card title="TG Failed" value={analytics?.telegram_failed_signals ?? funnel?.signals?.telegram_failed ?? 0} />
          <Card title="Blocked" value={stats.blocked} />
          <Card title="Avg Conf" value={`${stats.avgConfidence}%`} />
        </section>

        {funnel && <FunnelPanel funnel={funnel} />}

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-emerald-200">
                Scan Results
              </h2>
              <p className="text-xs text-emerald-100/50">
                Текущая картина по каждой монете
              </p>
            </div>

            <div className="flex flex-col gap-2 text-xs text-emerald-100/60 sm:flex-row sm:items-center">
              <span>автообновление каждые 10 секунд</span>
              <select
                value={scanStatusFilter}
                onChange={(e) => setScanStatusFilter(e.target.value)}
                className="rounded-xl border border-emerald-800 bg-black/40 px-3 py-2 text-emerald-100 outline-none focus:border-emerald-400"
              >
                <option value="actionable">actionable first</option>
                <option value="all">all statuses</option>
                <option value="candidate">candidate</option>
                <option value="wait">wait</option>
                <option value="watch">watch</option>
                <option value="blocked">blocked</option>
                <option value="rejected">rejected</option>
                <option value="hold">hold</option>
              </select>
              <span>показано {filteredResults.length} / {results.length}</span>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {filteredResults.map((r: any) => (
              <ScanCard key={r.symbol} item={r} />
            ))}

            {filteredResults.length === 0 && (
              <div className="rounded-2xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50 xl:col-span-2">
                Данных по выбранному фильтру нет
              </div>
            )}
          </div>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-emerald-200">
                Decision Events
              </h2>
              <p className="text-xs text-emerald-100/50">
                Сгруппированные важные решения робота
              </p>
            </div>

            <div className="flex flex-wrap gap-2 text-xs">
              <MiniPill label="events" value={importantEvents.length} />
              <MiniPill label="exposure blocks" value={stats.exposureBlocked} danger />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {importantEvents.map((e: any) => (
              <EventCard
                key={`${e.symbol}-${e.status}-${e.decision}-${e.action}`}
                event={e}
              />
            ))}

            {importantEvents.length === 0 && (
              <div className="rounded-2xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50 lg:col-span-2">
                Событий пока нет
              </div>
            )}
          </div>
        </section>
    </AppShell>
  );
}

function FunnelPanel({ funnel }: { funnel: any }) {
  const reasons = Array.isArray(funnel?.diagnosis?.reasons) ? funnel.diagnosis.reasons : [];
  const actions = Array.isArray(funnel?.diagnosis?.actions) ? funnel.diagnosis.actions : [];
  const blockers = Array.isArray(funnel?.events?.top_blockers) ? funnel.events.top_blockers : [];

  return (
    <section className="rounded-2xl border border-amber-700/60 bg-amber-950/20 p-5">
      <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 className="text-xl font-semibold text-amber-200">
            Candidate → Published/Open Funnel
          </h2>
          <p className="text-xs text-amber-100/60">
            Почему кандидаты не доходят до published/open: readonly scan, bot status, production gates, Telegram delivery.
          </p>
        </div>

        <div className="flex flex-wrap gap-2 text-xs">
          <MiniPill label="bot" value={funnel?.bot?.status || "-"} danger={!funnel?.bot?.running} />
          <MiniPill label="ready" value={funnel?.events?.ready_candidates ?? 0} />
          <MiniPill label="active" value={funnel?.signals?.active_like ?? 0} />
          <MiniPill label="tg failed" value={funnel?.telegram_delivery?.failed ?? 0} danger={Number(funnel?.telegram_delivery?.failed || 0) > 0} />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-xl border border-amber-800/50 bg-black/20 p-4 lg:col-span-2">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-amber-200/80">
            Diagnosis
          </div>
          <ul className="space-y-2 text-sm text-amber-50/80">
            {reasons.map((reason: string, idx: number) => (
              <li key={`${reason}-${idx}`} className="rounded-lg border border-amber-900/50 bg-black/20 p-3">
                {reason}
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded-xl border border-amber-800/50 bg-black/20 p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-amber-200/80">
            Next actions
          </div>
          <ul className="space-y-2 text-sm text-amber-50/80">
            {actions.map((action: string, idx: number) => (
              <li key={`${action}-${idx}`} className="rounded-lg border border-amber-900/50 bg-black/20 p-3">
                {action}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {blockers.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2 text-xs">
          {blockers.map((blocker: any) => (
            <span key={blocker.decision} className="rounded-full border border-amber-800/70 bg-black/20 px-3 py-1 text-amber-100/80">
              {blocker.decision}: {blocker.count}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

function ScanCard({ item }: { item: any }) {
  const scores = item.scores || {};
  const setup = item.setup_quality || {};
  const exposure = item.exposure;
  const plan = item.plan;

  return (
    <article className="rounded-2xl border border-emerald-900 bg-black/30 p-4">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-xl font-bold text-emerald-300">{item.symbol}</h3>
            <StatusBadge status={item.status} />
            {item.grade && <GradeBadge grade={item.grade} />}
          </div>

          <div className="mt-1 text-xs text-emerald-100/50">
            {item.reason || item.decision || "-"}
          </div>
        </div>

        <div className="text-right text-xs text-emerald-100/50">
          <div>age</div>
          <div className="font-semibold text-emerald-200">
            {item.watch_age_minutes != null ? `${item.watch_age_minutes}m` : "-"}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <Metric label="Action" value={item.action || "-"} />
        <Metric label="Regime" value={item.regime || "-"} />
        <Metric label="Confidence" value={item.effective_confidence ?? item.confidence_hint ?? "-"} />
        <Metric label="Setup" value={setup.final_score ?? "-"} />
        <Metric label="Radar" value={item.radar_state || "-"} />
        <Metric label="Escalation" value={item.escalation_state || "-"} />
      </div>

      <div className="mt-4 rounded-xl border border-emerald-950 bg-black/20 p-3">
        <div className="mb-2 text-xs font-semibold text-emerald-300">
          Scores
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs">
          <Score label="Trend" value={scores.trend} />
          <Score label="Momentum" value={scores.momentum} />
          <Score label="Volume" value={scores.volume} />
          <Score label="Structure" value={scores.structure} />
          <Score label="Volatility" value={scores.volatility} />
          <Score label="Total" value={scores.total} />
        </div>
      </div>

      {item.entry_zone && (
        <div className="mt-4 rounded-xl border border-emerald-950 bg-black/20 p-3">
          <div className="mb-2 text-xs font-semibold text-emerald-300">
            Trade Levels
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs">
            <Metric label="Entry" value={`${item.entry_zone[0]} - ${item.entry_zone[1]}`} />
            <Metric label="Stop" value={item.stop_price ?? "-"} />
            <Metric label="TP1" value={item.tp?.tp1 ?? "-"} />
            <Metric label="TP2" value={item.tp?.tp2 ?? "-"} />
          </div>
        </div>
      )}

      {plan && (
        <div className="mt-4 rounded-xl border border-emerald-950 bg-black/20 p-3">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-xs font-semibold text-emerald-300">
              Trade Plan
            </div>
            <PlanBadge valid={plan.is_valid} />
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs">
            <Metric label="Qty" value={plan.qty ?? "-"} />
            <Metric label="Margin" value={fmtUsdt(plan.required_margin)} />
            <Metric label="Net TP1" value={fmtUsdt(plan.net_pnl_tp1)} good />
            <Metric label="Net TP2" value={fmtUsdt(plan.net_pnl_tp2)} good />
            <Metric label="Net Stop" value={fmtUsdt(plan.net_pnl_stop)} danger />
            <Metric label="RR TP2" value={plan.net_rr_tp2 ?? "-"} />
          </div>
        </div>
      )}

      {exposure && (
        <div className="mt-4 rounded-xl border border-emerald-950 bg-black/20 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-xs font-semibold text-emerald-300">
              ExposureGuard
            </div>
            <span
              className={`rounded-lg px-2 py-1 text-[11px] font-semibold ${
                exposure.allowed
                  ? "bg-emerald-700 text-white"
                  : "bg-red-800 text-white"
              }`}
            >
              {exposure.allowed ? "allowed" : exposure.reason || "blocked"}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs">
            <Metric label="Used" value={fmtUsdt(exposure.used_margin)} />
            <Metric label="Free" value={fmtUsdt(exposure.free_margin)} />
            <Metric label="Required" value={fmtUsdt(exposure.required_margin)} />
            <Metric label="Active" value={exposure.active_signals_count ?? "-"} />
          </div>
        </div>
      )}

      <div className="mt-4 border-t border-emerald-950 pt-3">
        <div className="mb-2 text-xs font-semibold text-emerald-300">
          Timeframes
        </div>

        <div className="grid grid-cols-1 gap-2">
          {item.timeframes &&
            Object.entries(item.timeframes).map(([tf, ctx]: any) => (
              <TimeframeMini key={tf} tf={tf} ctx={ctx} />
            ))}
        </div>
      </div>

      <div className="mt-4 text-xs text-emerald-100/50">
        Decision:{" "}
        <span className="text-emerald-200">
          {decisionLabel(item.decision || "-")}
        </span>
      </div>

      {item.escalation_reason && (
        <div className="mt-1 text-xs text-emerald-100/50">
          Reason: <span className="text-emerald-200">{item.escalation_reason}</span>
        </div>
      )}
    </article>
  );
}

function EventCard({ event }: { event: any }) {
  const payload = event.payload || {};
  const exposure = payload.exposure;
  const plan = payload.plan;

  return (
    <article className="rounded-2xl border border-emerald-900 bg-black/30 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-bold text-emerald-300">
              {event.symbol || "-"}
            </h3>
            <StatusBadge status={event.status} />
            {event.count && event.count > 1 && (
              <span className="rounded-lg bg-emerald-950 px-2 py-1 text-xs font-semibold text-emerald-200">
                ×{event.count}
              </span>
            )}
          </div>

          <div className="mt-1 text-xs text-emerald-100/50">
            {formatTime(event.created_at)}
          </div>
        </div>

        <DecisionBadge decision={event.decision || "-"} />
      </div>

      <p className="mb-4 text-sm leading-relaxed text-emerald-100/75">
        {decisionExplanation(event)}
      </p>

      <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-3">
        <Metric label="Action" value={event.action ?? payload.action ?? "-"} />
        <Metric label="Regime" value={event.regime ?? payload.regime ?? "-"} />
        <Metric label="Radar" value={event.radar_state ?? payload.radar_state ?? "-"} />
        <Metric label="Conf" value={event.confidence_hint ?? payload.confidence_hint ?? "-"} />
        <Metric label="Setup" value={event.setup_score ?? payload.setup_quality?.final_score ?? "-"} />
        <Metric label="Age" value={payload.watch_age_minutes ? `${payload.watch_age_minutes}m` : "-"} />
      </div>

      {plan && (
        <div className="mt-4 rounded-xl border border-emerald-950 bg-black/20 p-3">
          <div className="mb-2 text-xs font-semibold text-emerald-300">
            Plan Snapshot
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs">
            <Metric label="Margin" value={fmtUsdt(plan.required_margin)} />
            <Metric label="RR TP2" value={plan.net_rr_tp2 ?? "-"} />
            <Metric label="Net TP2" value={fmtUsdt(plan.net_pnl_tp2)} good />
            <Metric label="Net Stop" value={fmtUsdt(plan.net_pnl_stop)} danger />
          </div>
        </div>
      )}

      {exposure && (
        <div className="mt-4 rounded-xl border border-emerald-950 bg-black/20 p-3">
          <div className="mb-2 text-xs font-semibold text-emerald-300">
            Exposure
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs">
            <Metric label="Used" value={fmtUsdt(exposure.used_margin)} />
            <Metric label="Free" value={fmtUsdt(exposure.free_margin)} />
            <Metric label="Required" value={fmtUsdt(exposure.required_margin)} />
            <Metric label="Active" value={exposure.active_signals_count ?? "-"} />
          </div>
        </div>
      )}
    </article>
  );
}

function Card({ title, value }: { title: string; value: any }) {
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className="mt-2 text-2xl font-bold text-emerald-200">{value}</div>
    </div>
  );
}

function Metric({
  label,
  value,
  good,
  danger,
}: {
  label: string;
  value: any;
  good?: boolean;
  danger?: boolean;
}) {
  const valueClass = danger
    ? "text-red-300"
    : good
      ? "text-emerald-300"
      : "text-emerald-100";

  return (
    <div className="rounded-lg border border-emerald-950 bg-black/20 p-2">
      <div className="text-[11px] text-emerald-100/45">{label}</div>
      <div className={`mt-1 break-words font-semibold ${valueClass}`}>
        {value === null || value === undefined || value === "" ? "-" : String(value)}
      </div>
    </div>
  );
}

function Score({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-emerald-950 py-1">
      <span className="text-emerald-100/50">{label}</span>
      <span className="font-semibold text-emerald-100">
        {value ?? "-"}
      </span>
    </div>
  );
}

function TimeframeMini({ tf, ctx }: { tf: string; ctx: any }) {
  return (
    <div className="rounded-lg border border-emerald-950 bg-black/20 p-2 text-xs">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-bold text-emerald-300">{tf}</span>
        <span className="text-emerald-100/60">
          RSI {ctx?.rsi14 ?? "-"}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-1 text-[11px]">
        <Tiny label="Trend" value={ctx?.trend} />
        <Tiny label="Mom" value={ctx?.momentum} />
        <Tiny label="Vol" value={ctx?.volume_state} />
      </div>
    </div>
  );
}

function Tiny({ label, value }: { label: string; value: any }) {
  return (
    <div>
      <div className="text-emerald-100/40">{label}</div>
      <div className="truncate text-emerald-100">{value || "-"}</div>
    </div>
  );
}

function MiniPill({
  label,
  value,
  danger,
}: {
  label: string;
  value: any;
  danger?: boolean;
}) {
  return (
    <span
      className={`rounded-lg px-2 py-1 font-semibold ${
        danger ? "bg-red-900 text-red-100" : "bg-emerald-950 text-emerald-200"
      }`}
    >
      {label}: {value}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "candidate"
      ? "bg-emerald-500 text-black"
      : status === "published"
        ? "bg-emerald-600 text-white"
        : status === "watch"
          ? "bg-cyan-700 text-white"
          : status === "wait"
            ? "bg-yellow-500 text-black"
            : status === "hold"
              ? "bg-yellow-700 text-black"
              : status === "rejected"
                ? "bg-red-800 text-white"
                : status === "blocked"
                  ? "bg-red-700 text-white"
                  : status === "error"
                    ? "bg-red-950 text-red-100"
                    : "bg-emerald-950 text-emerald-200";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {status || "-"}
    </span>
  );
}

function GradeBadge({ grade }: { grade: string }) {
  const cls =
    grade === "A+"
      ? "bg-emerald-500 text-black"
      : grade === "A"
        ? "bg-emerald-800 text-emerald-100"
        : grade === "B"
          ? "bg-yellow-600 text-black"
          : "bg-red-700 text-white";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {grade}
    </span>
  );
}

function PlanBadge({ valid }: { valid: boolean }) {
  return (
    <span
      className={`rounded-lg px-2 py-1 text-[11px] font-semibold ${
        valid ? "bg-emerald-700 text-white" : "bg-red-800 text-white"
      }`}
    >
      {valid ? "valid" : "invalid"}
    </span>
  );
}

function decisionLabel(code: string | null | undefined) {
  const map: Record<string, string> = {
    ready_to_publish: "Готов к публикации",
    published_signal_created: "Сигнал создан",
    published_by_priority_queue: "Опубликован (priority queue)",
    signal_published: "Сигнал опубликован",

    priority_queue_wait_next_scan: "Отложен (следующий скан)",
    deferred_by_scan_limit: "Отложен (лимит скана)",

    active_signal_already_exists: "Активный сигнал уже существует",

    wait_better_entry_rr: "Ждём лучший вход по RR",
    candidate_but_wait_confirmation: "Ждём подтверждение",

    net_rr_too_low: "RR ниже минимума",
    required_margin_exceeds_balance: "Недостаточно баланса",
    required_margin_exceeds_free_margin: "Недостаточно свободной маржи",
    max_active_signals_reached: "Лимит активных сигналов",
    trade_plan_rejected: "TradePlan отклонил",

    setup_quality_too_low: "Слабый сетап",
    quality_grade_too_low: "Grade ниже порога",
    grade_c_learning_only: "Grade C — только обучение",
    grade_c_blocked_before_signal_create: "Grade C заблокирован",

    watch_long: "Watch LONG",
    watch_short: "Watch SHORT",
    watch_expired: "Watch истёк",
    watch_cooldown: "Watch cooldown",

    symbol_cooldown_losing_streak: "Cooldown: серия убытков",
    symbol_cooldown_failed_setup_streak: "Cooldown: failed setup серия",
    symbol_negative_expectancy_blocked: "Заблокирован: отрицательное матожидание",
    symbol_weak_reduce_risk: "Слабый символ: риск снижен",
    reentry_cooldown_active: "Cooldown повторного входа",

    skip_no_trade_conditions: "Нет условий",
    short_candidate_but_shorts_disabled: "Short отключён",
    learning_setup_too_low: "Сетап ниже порога обучения",
    learning_wait_more_confirmation: "Ждём подтверждение сетапа",
  };

  if (!code) return "-";
  return map[code] || code;
}

function DecisionBadge({ decision }: { decision: string }) {
  const cls =
    decision === "ready_to_publish" || decision === "published_signal_created"
      ? "bg-emerald-500 text-black"
      : decision === "wait_better_entry_rr" || decision === "net_rr_too_low"
        ? "bg-orange-600 text-white"
        : decision === "active_signal_already_exists"
          ? "bg-blue-700 text-white"
          : decision === "required_margin_exceeds_balance" ||
              decision === "required_margin_exceeds_free_margin" ||
              decision === "max_active_signals_reached"
            ? "bg-orange-800 text-white"
            : decision === "candidate_but_wait_confirmation"
              ? "bg-yellow-500 text-black"
              : decision === "watch_long" || decision === "watch_short"
                ? "bg-cyan-700 text-white"
                : decision === "watch_cooldown"
                  ? "bg-purple-700 text-white"
                  : decision === "quality_grade_too_low" ||
                      decision === "setup_quality_too_low" ||
                      decision === "short_candidate_but_shorts_disabled"
                    ? "bg-red-700 text-white"
                    : decision === "skip_no_trade_conditions"
                      ? "bg-emerald-950 text-emerald-200"
                      : "bg-slate-700 text-white";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {decisionLabel(decision)}
    </span>
  );
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

function fmtUsdt(value: any) {
  if (value === null || value === undefined || value === "") return "-";

  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);

  return `${n.toFixed(4)} USDT`;
}

function avg(values: number[]) {
  if (!values.length) return 0;
  return Number((values.reduce((sum, v) => sum + v, 0) / values.length).toFixed(2));
}

function decisionExplanation(e: any) {
  const decision = e?.decision;
  const payload = e?.payload || {};
  const plan = payload?.plan;

  if (decision === "ready_to_publish") {
    return "Сигнал прошёл фильтры качества и TradePlan. Можно публиковать.";
  }

  if (decision === "published_signal_created" || decision === "signal_published") {
    return "Робот создал и опубликовал сигнал.";
  }

  if (decision === "required_margin_exceeds_balance") {
    return `TradePlan отклонил: требуемая маржа ${plan?.required_margin ?? "-"} USDT превышает доступный баланс или буфер.`;
  }

  if (decision === "required_margin_exceeds_free_margin") {
    const exposure = payload?.exposure;
    const required = exposure?.required_margin ?? plan?.required_margin;
    const free = exposure?.free_margin;
    const used = exposure?.used_margin;
    const maxAllowed = exposure?.max_allowed_margin;

    return `Публикация заблокирована ExposureGuard: требуется ${required ?? "-"} USDT, свободно ${free ?? "-"} USDT, используется ${used ?? "-"} USDT из лимита ${maxAllowed ?? "-"} USDT.`;
  }

  if (decision === "max_active_signals_reached") {
    const exposure = payload?.exposure;
    return `Публикация заблокирована ExposureGuard: достигнут лимит активных сигналов. Активных сигналов: ${exposure?.active_signals_count ?? "-"}.`;
  }

  if (decision === "wait_better_entry_rr") {
    const rr = plan?.net_rr_tp2;
    const stop = plan?.net_pnl_stop;
    const tp2 = plan?.net_pnl_tp2;

    return `Тренд и сетап есть, но вход сейчас даёт слабый RR. Ждём откат/лучший вход. RR TP2: ${rr ?? "-"}, риск ${stop ?? "-"} USDT, прибыль TP2 ${tp2 ?? "-"} USDT.`;
  }

  if (decision === "active_signal_already_exists") {
    return "Новый сигнал качественный, но по этой монете уже есть активный сигнал. Повторную публикацию блокируем.";
  }

  if (decision === "net_rr_too_low") {
    const rr = plan?.net_rr_tp2;
    const stop = plan?.net_pnl_stop;
    const tp2 = plan?.net_pnl_tp2;

    if (rr !== undefined) {
      return `TradePlan отклонил: RR TP2 = ${rr}, риск ${stop ?? "-"} USDT, прибыль TP2 ${tp2 ?? "-"} USDT.`;
    }

    return "TradePlan отклонил: соотношение риск/прибыль слишком низкое.";
  }

  if (decision === "quality_grade_too_low") {
    const grade = payload?.grade;
    const score = e?.setup_score ?? payload?.setup_quality?.final_score;
    return `Сетап подтверждён, но grade ${grade || "-"} ниже порога публикации. Setup score: ${score ?? "-"}.`;
  }

  if (decision === "setup_quality_too_low") {
    const score = e?.setup_score ?? payload?.setup_quality?.final_score;
    const comment = payload?.setup_quality?.comment;
    return `Качество сетапа недостаточное. Setup score: ${score ?? "-"}${comment ? `, ${comment}` : ""}.`;
  }

  if (decision === "candidate_but_wait_confirmation") {
    const score = e?.setup_score ?? payload?.setup_quality?.final_score;
    return `Есть кандидат, но робот ждёт подтверждение. Setup score: ${score ?? "-"}.`;
  }

  if (decision === "watch_long") {
    const age = payload?.watch_age_minutes;
    return `Робот наблюдает LONG-сценарий. Подтверждения для входа пока нет${age ? `, возраст watch ${age} мин.` : "."}`;
  }

  if (decision === "watch_short") {
    const age = payload?.watch_age_minutes;
    return `Робот наблюдает SHORT-сценарий. Подтверждения для входа пока нет${age ? `, возраст watch ${age} мин.` : "."}`;
  }

  if (decision === "watch_expired") {
    return "Watch-сценарий истёк без подтверждения входа.";
  }

  if (decision === "watch_cooldown") {
    return payload?.reason
      ? `Монета недавно вышла из watch по expiry. Повторный вход временно заблокирован: ${payload.reason}.`
      : "Монета недавно вышла из watch по expiry. Повторный вход временно заблокирован cooldown-фильтром.";
  }

  if (decision === "skip_no_trade_conditions") {
    return "Нет торговых условий: робот не видит качественного кандидата.";
  }

  if (decision === "short_candidate_but_shorts_disabled") {
    return "Найден SHORT-кандидат, но short-сделки отключены текущим режимом.";
  }

  return payload?.reason || e?.decision || "-";
}

function groupDecisionEvents(events: any[]) {
  const map = new Map<string, any>();

  for (const event of events || []) {
    const key = [
      event.symbol || "-",
      event.status || "-",
      event.decision || "-",
      event.action || "-",
    ].join("|");

    const existing = map.get(key);

    if (!existing) {
      map.set(key, {
        ...event,
        count: 1,
        first_created_at: event.created_at,
        last_created_at: event.created_at,
      });
      continue;
    }

    existing.count += 1;

    const currentTime = new Date(event.created_at).getTime();
    const existingTime = new Date(existing.created_at).getTime();

    if (!Number.isNaN(currentTime) && !Number.isNaN(existingTime)) {
      if (currentTime > existingTime) {
        map.set(key, {
          ...event,
          count: existing.count,
          first_created_at: existing.first_created_at,
          last_created_at: event.created_at,
        });
      }
    }
  }

  return Array.from(map.values()).sort((a, b) => {
    const at = new Date(a.created_at).getTime();
    const bt = new Date(b.created_at).getTime();

    if (Number.isNaN(at) || Number.isNaN(bt)) return 0;

    return bt - at;
  });
}