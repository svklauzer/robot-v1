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
  const [symbolPerf, setSymbolPerf] = useState<any>(null);
  const [validationGates, setValidationGates] = useState<any>(null);
  const [mfeMae, setMfeMae] = useState<any>(null);
  const [mfeWindow, setMfeWindow] = useState<string>("168");
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setLoading(true);
    try {
      const mfeQs = mfeWindow === "all" ? "" : `&window_hours=${mfeWindow}`;
      const [summaryData, qualityData, readinessData, rootCauseData, symbolPerfData, validationData, mfeMaeData] = await Promise.all([
        apiGet("/analytics/summary"),
        apiGet("/analytics/signal-quality"),
        apiGet("/system/readiness"),
        apiGet("/analytics/outcome-root-cause?reason=failed_setup_exit&limit=500"),
        apiGet("/analytics/symbol-performance?lookback=12"),
        apiGet("/analytics/validation-gates"),
        apiGet(`/analytics/mfe-mae?limit=500${mfeQs}`).catch(() => null),
      ]);
      setSummary(summaryData);
      setQuality(qualityData);
      setReadiness(readinessData);
      setRootCause(rootCauseData);
      setSymbolPerf(symbolPerfData);
      setValidationGates(validationData);
      setMfeMae(mfeMaeData);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, [mfeWindow]);

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

        {/* (#analytics-window-2026-07-10) Явные окна выборки: Net PnL/Winrate —
            ВСЯ история (единый источник истины), quality-метрики — последние 200
            закрытых. Failed setup красный только если причина ЖИВАЯ (7д). */}
        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
          <StatCard title="Net PnL (вся история)" value={`${summary?.total_net_pnl_usdt ?? 0} USDT`} danger={(summary?.total_net_pnl_usdt ?? 0) < 0} good={(summary?.total_net_pnl_usdt ?? 0) > 0} />
          <StatCard title="Winrate (вся история)" value={`${summary?.winrate ?? 0}%`} warn={(summary?.winrate ?? 0) < 45} good={(summary?.winrate ?? 0) >= 50} />
          <StatCard title="Failed setup (посл. 200)" value={quality?.by_reason?.failed_setup_exit ?? 0} danger={(quality?.by_reason?.failed_setup_exit ?? 0) > 0 && rootCause?.is_active !== false} good={(quality?.by_reason?.failed_setup_exit ?? 0) > 0 && rootCause?.is_active === false} />
          <StatCard title="Positive→Negative (посл. 200)" value={`${quality?.positive_then_negative_rate ?? 0}%`} warn={(quality?.positive_then_negative_rate ?? 0) > 25} />
          <StatCard title="MFE capture (посл. 200)" value={`${quality?.mfe_capture_rate ?? 0}%`} good={(quality?.mfe_capture_count ?? 0) > 0} />
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
            <Metric label="VIP SLA" value={`${readiness?.telegram_delivery?.vip_sla_pct ?? 100}%`} />
            <Metric label="Sent" value={readiness?.telegram_delivery?.sent ?? 0} />
            <Metric label="VIP queued" value={readiness?.telegram_delivery?.vip_queued ?? 0} />
            <Metric label="Failed" value={readiness?.telegram_delivery?.failed ?? 0} />
            {readiness?.telegram_delivery?.last_error && (
              <p className="mt-3 text-xs text-red-200">{readiness.telegram_delivery.last_error}</p>
            )}
          </Panel>

          <Panel title="Profit gates" icon={<AlertTriangle size={18} />}>
            <Metric label="Closed validation signals" value={`${validationGates?.closed_count ?? 0} / ${validationGates?.min_closed ?? readiness?.required_gates?.closed_validation_signals ?? 200}`} warn={!validationGates?.gates?.min_closed_outcomes} />
            {/* (#analytics-window-2026-07-10) Без фолбэка на total: rolling-окно
                (последние 50) и вся история — разные числа, подмена вводила бы
                в заблуждение при недоступном validation-эндпоинте. */}
            <Metric label="Rolling net PnL" value={validationGates?.net_pnl_usdt != null ? `${validationGates.net_pnl_usdt} USDT` : "—"} good={validationGates?.gates?.rolling_net_pnl_positive} warn={validationGates != null && !validationGates?.gates?.rolling_net_pnl_positive} />
            <Metric label="Failed setup" value={`${validationGates?.failed_setup_share_pct ?? 0}% / max ${validationGates?.failed_setup_max_pct ?? 35}%`} warn={!validationGates?.gates?.failed_setup_below_threshold} />
            <Metric label="Positive→Negative" value={`${validationGates?.positive_then_negative_rate_pct ?? 0}% / max ${validationGates?.positive_then_negative_max_pct ?? 25}%`} warn={!validationGates?.gates?.positive_then_negative_below_threshold} />
            <Metric label="MFE capture enabled" value={readiness?.required_gates?.adaptive_mfe_capture_enabled ? "yes" : "no"} />
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

        <section className="rounded-2xl border border-yellow-900/70 bg-yellow-950/10 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-yellow-200">Per-symbol profitability guard</h2>
              <p className="text-sm text-yellow-100/60">
                История по символам за {symbolPerf?.window_hours ? `${Math.round(symbolPerf.window_hours / 24)} дн.` : "30 дн."} (витрина).
                «no_history» = нет закрытий в окне; живой guard судит по 24ч. Решения публикации тут НЕ меняются.
              </p>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center text-sm">
              <div className="rounded-lg border border-red-900/50 px-3 py-2 text-red-200">Blocked<br />{symbolPerf?.blocked_count ?? 0}</div>
              <div className="rounded-lg border border-yellow-900/50 px-3 py-2 text-yellow-200">Reduced<br />{symbolPerf?.reduced_count ?? 0}</div>
              <div className="rounded-lg border border-emerald-900/50 px-3 py-2 text-emerald-200">OK<br />{symbolPerf?.ok_count ?? 0}</div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {(symbolPerf?.items || []).slice(0, 9).map((item: any) => (
              <div key={item.symbol} className="rounded-xl border border-yellow-900/50 bg-black/20 p-4">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="font-semibold text-yellow-100">{item.symbol}</h3>
                  <span className={item.classification === "blocked" ? "text-red-300" : item.classification === "reduced" ? "text-yellow-300" : "text-emerald-300"}>
                    {item.classification}
                  </span>
                </div>
                <Metric label="Reason" value={symbolReasonLabel(item.reason)} />
                <Metric label="Closed (окно)" value={item.closed_count ?? 0} />
                <Metric label="Risk x" value={item.risk_multiplier} />
                <Metric label="Net PnL" value={`${item.total_net_pnl} USDT`} />
                <Metric label="Failed setup" value={item.failed_setup_count} />
                <p className="mt-3 text-xs text-yellow-50/70">{item.action}</p>
              </div>
            ))}
          </div>
        </section>

        {/* (#mfe-mae-2026-07-10) MFE/MAE по символам × regime + динамика — замена
            ручного мониторинга по Telegram-отчётам. */}
        <section className="rounded-2xl border border-cyan-900/70 bg-cyan-950/10 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="text-xl font-semibold text-cyan-200">MFE / MAE по режимам</h2>
              <p className="text-sm text-cyan-100/60">
                MFE — пиковый ход в плюс, MAE — пиковая просадка. Edge = MFE/|MAE| (качество входов),
                Capture — доля пика, забранная закрытием (качество выходов). Выборка: {mfeMae?.sample_count ?? "—"} закрытых.
              </p>
            </div>
            <label className="text-sm text-cyan-100/70">
              Окно
              <select
                value={mfeWindow}
                onChange={(e) => setMfeWindow(e.target.value)}
                className="ml-2 rounded-xl border border-cyan-800 bg-black/40 px-3 py-2 text-cyan-100 outline-none focus:border-cyan-400"
              >
                <option value="24">24 часа</option>
                <option value="72">3 дня</option>
                <option value="168">7 дней</option>
                <option value="720">30 дней</option>
                <option value="all">вся история</option>
              </select>
            </label>
          </div>

          {!mfeMae && <p className="text-sm text-cyan-100/50">Нет данных (эндпоинт недоступен или нет закрытий в окне).</p>}

          {mfeMae && (
            <div className="space-y-6">
              <div>
                <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-cyan-100/50">По режимам</h3>
                <div className="space-y-1">
                  {(mfeMae.by_regime || []).map((r: any) => (
                    <MfeMaeRow key={r.regime} label={r.regime} item={r} maxAbs={mfeScale(mfeMae)} />
                  ))}
                </div>
              </div>

              <div>
                <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-cyan-100/50">Символ × режим</h3>
                <div className="space-y-1">
                  {(mfeMae.by_symbol_regime || []).map((r: any) => (
                    <MfeMaeRow key={`${r.symbol}|${r.regime}`} label={`${r.symbol.replace("/USDT", "")} · ${r.regime}`} item={r} maxAbs={mfeScale(mfeMae)} />
                  ))}
                </div>
              </div>

              {(mfeMae.daily || []).length >= 2 && (
                <div>
                  <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-cyan-100/50">
                    Динамика по дням (avg MFE — зелёная, avg MAE — красная, Capture % — пунктир)
                  </h3>
                  <DailyDynamics daily={mfeMae.daily} />
                </div>
              )}
            </div>
          )}
        </section>

        <section className="rounded-2xl border border-red-900/70 bg-red-950/20 p-5">
          <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <h2 className="flex items-center gap-2 text-xl font-semibold text-red-200">
                Failed setup root cause
                {/* (#root-cause-recency-2026-07-10) Причина не живая → это архив, не алерт */}
                {rootCause?.is_active === false && (
                  <span className="rounded-lg bg-slate-700 px-2 py-0.5 text-xs font-semibold text-slate-200">
                    исторический хвост
                  </span>
                )}
                {rootCause?.is_active === true && (
                  <span className="rounded-lg bg-red-600 px-2 py-0.5 text-xs font-semibold text-white">
                    активна (7д: {rootCause?.recent_count_7d})
                  </span>
                )}
              </h2>
              <p className="text-sm text-red-100/60">
                Где именно утёк PnL по failed_setup_exit. Выборка: последние {rootCause?.sample_closed_signals ?? "—"} закрытых (вся история).
                {rootCause?.last_occurrence_at && (
                  <> Последний случай: {String(rootCause.last_occurrence_at).slice(0, 10)}.</>
                )}
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
              <Metric label="Net по причине (вся выборка)" value={`${rootCause?.target_net_pnl_usdt ?? 0} USDT`} />
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

const SYMBOL_REASON_LABELS: Record<string, string> = {
  no_history: "нет закрытий в окне",
  small_history_ok: "мало истории — ок",
  small_history_last_stop_reduce_risk: "после стопа — риск снижен",
  symbol_near_breakeven_mild_reduce: "около безубытка — лёгкое снижение",
  symbol_gives_back_profit_reduce_risk: "отдаёт прибыль — риск снижен",
  symbol_negative_expectancy_blocked: "отрицательное ожидание — блок",
  symbol_cooldown_losing_streak: "серия убытков — cooldown",
  symbol_cooldown_failed_setup_streak: "серия failed setup — cooldown",
  ok: "ок",
};

function symbolReasonLabel(code: any): string {
  const key = String(code || "");
  return SYMBOL_REASON_LABELS[key] || key || "-";
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

function Metric({ label, value, good, warn }: { label: string; value: any; good?: boolean; warn?: boolean }) {
  const valueClass = warn ? "text-yellow-300" : good ? "text-emerald-300" : "text-emerald-200";
  return (
    <div className="flex items-center justify-between border-b border-emerald-950 py-2 text-sm last:border-b-0">
      <span className="text-emerald-100/60">{label}</span>
      <span className={`font-semibold ${valueClass}`}>{value}</span>
    </div>
  );
}

// (#mfe-mae-2026-07-10) Масштаб баров: максимум |MFE|/|MAE| по всем строкам.
function mfeScale(data: any): number {
  let m = 0.5;
  for (const list of [data?.by_regime || [], data?.by_symbol_regime || []]) {
    for (const r of list) {
      m = Math.max(m, Math.abs(r.avg_mfe_pct || 0), Math.abs(r.avg_mae_pct || 0));
    }
  }
  return m;
}

function MfeMaeRow({ label, item, maxAbs }: { label: string; item: any; maxAbs: number }) {
  const mfeW = Math.min(Math.abs(item.avg_mfe_pct || 0) / maxAbs * 100, 100);
  const maeW = Math.min(Math.abs(item.avg_mae_pct || 0) / maxAbs * 100, 100);
  const netCls = (item.net_pnl_usdt ?? 0) < 0 ? "text-red-300" : "text-emerald-300";
  const edgeCls = (item.edge_ratio ?? 0) >= 1.5 ? "text-emerald-300" : (item.edge_ratio ?? 0) >= 1.0 ? "text-yellow-300" : "text-red-300";

  return (
    <div className="grid grid-cols-1 items-center gap-2 rounded-lg border border-cyan-950/60 bg-black/20 px-3 py-2 md:grid-cols-[190px_1fr_260px]">
      <div className="truncate text-sm font-semibold text-cyan-100">
        {label} <span className="font-normal text-cyan-100/40">×{item.count}</span>
      </div>
      {/* Зеркальные бары вокруг нулевой оси: MAE влево (красный), MFE вправо (зелёный) */}
      <div className="flex h-5 items-center">
        <div className="flex h-3 w-1/2 justify-end overflow-hidden rounded-l bg-red-950/30">
          <div className="h-full rounded-l bg-red-400/80" style={{ width: `${maeW}%` }} title={`MAE ${item.avg_mae_pct}%`} />
        </div>
        <div className="h-5 w-px bg-cyan-100/30" />
        <div className="flex h-3 w-1/2 overflow-hidden rounded-r bg-emerald-950/30">
          <div className="h-full rounded-r bg-emerald-400/80" style={{ width: `${mfeW}%` }} title={`MFE ${item.avg_mfe_pct}%`} />
        </div>
      </div>
      <div className="flex flex-wrap gap-x-3 text-xs text-cyan-100/70">
        <span className="text-red-300">{item.avg_mae_pct}%</span>
        <span className="text-emerald-300">+{item.avg_mfe_pct}%</span>
        <span className={edgeCls}>edge {item.edge_ratio ?? "—"}</span>
        <span>capt {item.capture_rate_pct != null ? `${item.capture_rate_pct}%` : "—"}</span>
        <span className={netCls}>{item.net_pnl_usdt} USDT</span>
      </div>
    </div>
  );
}

function DailyDynamics({ daily }: { daily: any[] }) {
  const W = 720, H = 160, PAD = 28;
  const rows = daily.slice(-30);
  const maxAbs = Math.max(0.5, ...rows.map((d) => Math.max(Math.abs(d.avg_mfe_pct || 0), Math.abs(d.avg_mae_pct || 0))));
  const x = (i: number) => PAD + (i / Math.max(rows.length - 1, 1)) * (W - PAD * 2);
  const y = (v: number) => H / 2 - (v / maxAbs) * (H / 2 - PAD / 2);
  const yCap = (v: number) => H - PAD / 2 - (Math.max(Math.min(v, 100), -100) / 200 + 0.5) * (H - PAD);
  const line = (get: (d: any) => number, fy: (v: number) => number) =>
    rows.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${fy(get(d)).toFixed(1)}`).join(" ");

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-40 w-full min-w-[560px]">
        {/* нулевая ось */}
        <line x1={PAD} y1={H / 2} x2={W - PAD} y2={H / 2} stroke="rgba(148,233,213,0.25)" strokeDasharray="3 3" />
        <text x={4} y={y(maxAbs) + 4} fill="rgba(148,233,213,0.5)" fontSize="10">+{maxAbs.toFixed(1)}%</text>
        <text x={4} y={H / 2 + 4} fill="rgba(148,233,213,0.5)" fontSize="10">0</text>
        <text x={4} y={y(-maxAbs) + 4} fill="rgba(148,233,213,0.5)" fontSize="10">−{maxAbs.toFixed(1)}%</text>
        <path d={line((d) => d.avg_mfe_pct || 0, y)} fill="none" stroke="rgb(52,211,153)" strokeWidth="2" />
        <path d={line((d) => d.avg_mae_pct || 0, y)} fill="none" stroke="rgb(248,113,113)" strokeWidth="2" />
        <path d={line((d) => d.capture_rate_pct ?? 0, yCap)} fill="none" stroke="rgba(103,232,249,0.8)" strokeWidth="1.5" strokeDasharray="5 4" />
        {rows.map((d, i) => (
          <g key={d.date}>
            <circle cx={x(i)} cy={y(d.avg_mfe_pct || 0)} r="2.5" fill="rgb(52,211,153)" />
            <circle cx={x(i)} cy={y(d.avg_mae_pct || 0)} r="2.5" fill="rgb(248,113,113)" />
            {(i % Math.ceil(rows.length / 8) === 0 || i === rows.length - 1) && (
              <text x={x(i)} y={H - 2} textAnchor="middle" fill="rgba(148,233,213,0.5)" fontSize="9">
                {String(d.date).slice(5)}
              </text>
            )}
          </g>
        ))}
      </svg>
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
