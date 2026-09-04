"use client";

import { useEffect, useMemo, useState } from "react";
import GradeBadge from "../../components/GradeBadge";
import { RefreshCw } from "lucide-react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";

type SignalItem = any;

// Человекочитаемые ярлыки причин закрытия (синхронизация с правками бэка).
const CLOSE_REASON_LABELS: Record<string, string> = {
  tp2_reached: "TP2 достигнут",
  tp1_reached: "TP1 достигнут",
  stop_loss: "Стоп",
  breakeven_stop: "Безубыток-стоп (после TP1)",
  scalp_time_stop: "Скальп: тайм-стоп",
  low_grade_capital_release: "Слабый грейд: высвобождение капитала",
  manual_close: "Закрыто вручную (по рынку)",
  manual_cancel: "Отменено вручную",
  manual_profit_close: "Закрыто вручную (+)",
  manual_loss_close: "Закрыто вручную (−)",
  failed_setup_exit: "Сетап не подтвердился",
  breakeven_lock: "Безубыток-замок",
  scalp_breakeven_lock: "Скальп: безубыток-замок",
  scalp_flow_exit: "Скальп: выход по потоку",
  trend_ride_trailing_stop: "Трейл по тренду",
  adaptive_post_tp1_stop: "Трейл после TP1",
  trend_trailing_stop: "Трейл по тренду",
  adaptive_trailing_stop: "Адаптивный трейл",
  protective_trailing_stop: "Защитный трейл",
  protective_breakeven_profit_guard: "Защита безубытка",
  adaptive_mfe_capture: "Фиксация MFE",
  wide_stop_tp2_guard: "Защита TP2 (широкий стоп)",
  // (#trend-capture-band-2026-07-25) Ярус 2: фиксация в модальной полосе MFE.
  // До правки сделки с MFE 0.35–0.8% в тренде не имели механизма фиксации.
  trend_capture_band: "Трендовая фиксация (полоса MFE)",
  // (#tz-mfe-giveback-backstop-2026-09-02) ТЗ-выход смотрит только на слом
  // структуры (KAMA/ADX/OBV), не на отданную прибыль — бэкстоп фиксирует по
  // текущей цене сделку, которая отдала бОльшую часть значимого MFE.
  tz_mfe_giveback_backstop: "ТЗ: фиксация отданной прибыли",
  // (#progressive-tp2-2026-09-03) TP2 стал этапом, а не потолком: на нём
  // фиксируется доля остатка, хвост едет под трейлом.
  tp2_partial: "TP2: частичная фиксация",
  tp2_trail_stop: "Трейл после TP2",
  tp2_trail_giveback: "TP2: хвост отдал прибыль",
  // (#post-tp1-dead-zone-2026-09-03) Защита прибыли между TP1 и TP2.
  post_tp1_giveback_trail: "Фиксация отдачи после TP1",
};

function closeReasonLabel(code: string | null | undefined): string {
  if (!code) return "-";
  return CLOSE_REASON_LABELS[code] || code;
}

export default function SignalsPage() {
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [summaryData, setSummaryData] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const [statusFilter, setStatusFilter] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [gradeFilter, setGradeFilter] = useState("all");
  const [publicFilter, setPublicFilter] = useState("all");
  const [modeFilter, setModeFilter] = useState("all");

  async function loadSignals() {
    setLoading(true);

    try {
      // analytics/summary — ЕДИНЫЙ источник истины для сводных карточек (по ВСЕЙ
      // истории). Таблица сигналов — отдельный урезанный вид (limit=100).
      const [data, summary] = await Promise.all([
        apiGet("/signals?limit=100&offset=0"),
        apiGet("/analytics/summary").catch(() => null),
      ]);
      setSignals(Array.isArray(data) ? data : data?.items || []);
      setSummaryData(summary);
    } finally {
      setLoading(false);
    }
  }

  function confirmDanger(message: string) {
    return window.confirm(`⚠️ ${message}\n\nПродолжить?`);
  }

  // (#ux-errors-2026-07-09) Ошибки API больше не глотаются молча: 403 debug-гейта
  // в production раньше выглядел как «кнопка не работает».
  function reportActionError(e: any) {
    const msg = String(e?.message || e);
    if (msg.includes("debug_endpoints_disabled_in_production")) {
      alert(
        "Это debug-кнопка (инъекция тестовой цены) — в production она отключена.\n" +
        "Для реального закрытия используй «Закрыть по рынку»."
      );
      return;
    }
    alert(`Действие не выполнено: ${msg}`);
  }

  function assertOk(resp: any) {
    if (resp && resp.status === "error") {
      throw new Error(String(resp.error || "unknown_error"));
    }
    return resp;
  }

  async function testLifecyclePrice(id: number, price?: number | null) {
    if (price === undefined || price === null || Number.isNaN(Number(price))) {
      alert("Нет цены для lifecycle-теста");
      return;
    }

    if (!confirmDanger(`Lifecycle-test изменит состояние сигнала #${id} по цене ${price}.`)) return;

    try {
      assertOk(await apiPost("/robot/test-lifecycle-price", {
        signal_id: id,
        price: Number(price),
      }));
      await loadSignals();
    } catch (e) {
      reportActionError(e);
    }
  }

  async function closeSignal(id: number, result: number) {
    if (!confirmDanger(`Сигнал #${id} будет вручную закрыт с результатом ${result}%.`)) return;

    try {
      assertOk(await apiPost(`/signals/${id}/close`, {
        result_pct: result,
        reason: result > 0 ? "manual_profit_close" : "manual_loss_close",
      }));
      await loadSignals();
    } catch (e) {
      reportActionError(e);
    }
  }

  // (#manual-close-2026-07-09) Реальное ручное закрытие по живой рыночной цене —
  // полный lifecycle-путь (позиция, PnL с издержками, ML-метка). Работает в prod.
  async function closeSignalMarket(id: number) {
    if (!confirmDanger(`Сигнал #${id} будет закрыт ПО РЫНКУ (текущая цена, полный расчёт PnL).`)) return;

    try {
      assertOk(await apiPost(`/signals/${id}/close-market`));
      await loadSignals();
    } catch (e) {
      reportActionError(e);
    }
  }

  useEffect(() => {
    loadSignals();
  }, []);

  const filtered = useMemo(() => {
    return signals.filter((s) => {
      if (statusFilter !== "all" && s.status !== statusFilter) return false;
      if (sideFilter !== "all" && s.side !== sideFilter) return false;
      if (gradeFilter !== "all" && s.grade !== gradeFilter) return false;

      if (publicFilter === "public" && !s.is_public) return false;
      if (publicFilter === "private" && s.is_public) return false;
      if (modeFilter !== "all" && String(s.plan?.trade_mode || "") !== modeFilter) return false;

      return true;
    });
  }, [signals, statusFilter, sideFilter, gradeFilter, publicFilter, modeFilter]);

  const stats = useMemo(() => {
    const closed = signals.filter((s) => s.status === "closed");
    const wins = closed.filter((s) => Number(s.closed_net_pnl ?? s.result_pct ?? 0) > 0);
    const losses = closed.filter((s) => Number(s.closed_net_pnl ?? s.result_pct ?? 0) <= 0);

    const totalPct = closed.reduce((sum, s) => sum + Number(s.result_pct || 0), 0);
    const totalNet = closed.reduce((sum, s) => sum + Number(s.closed_net_pnl || 0), 0);
    const totalCosts = closed.reduce((sum, s) => sum + Number(s.closed_total_cost || 0), 0);

    return {
      total: signals.length,
      closed: closed.length,
      active: signals.filter((s) => ["published", "opened", "tp1", "breakeven"].includes(s.status)).length,
      expired: signals.filter((s) => s.status === "expired").length,
      rejected: signals.filter((s) => s.status === "rejected").length,
      wins: wins.length,
      losses: losses.length,
      winrate: closed.length ? ((wins.length / closed.length) * 100).toFixed(2) : "0.00",
      totalPct: totalPct.toFixed(4),
      totalNet: totalNet.toFixed(2),
      totalCosts: totalCosts.toFixed(2),
    };
  }, [signals]);

  return (
    <AppShell>

        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-emerald-300">
              Signals Journal
            </h1>
            <p className="text-sm text-emerald-100/70">
              Журнал сигналов, план сделки, результат закрытия и ручное управление
            </p>
          </div>

          <button
            onClick={loadSignals}
            className="flex items-center gap-2 rounded-xl bg-emerald-700 px-4 py-2 font-semibold text-black hover:bg-emerald-500"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
        </header>

        {/* Сводные карточки — из analytics/summary (вся история), единый источник
            с Dashboard/Analytics. Фолбэк на клиентский расчёт по таблице, если summary
            недоступен. Таблица ниже — урезанный вид (limit=100). */}
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
          <Card title="Сигналов" value={summaryData?.total_signals ?? stats.total} />
          <Card title="Активные" value={summaryData?.active_signals ?? stats.active} />
          <Card title="Закрыто" value={summaryData?.closed_signals ?? stats.closed} />
          <Card title="Expired" value={summaryData?.expired_signals ?? stats.expired} />
          <Card title="Rejected" value={summaryData?.rejected_signals ?? stats.rejected} />
          <Card title="Winrate" value={`${summaryData?.winrate ?? stats.winrate}%`} />
          <Card title="Итог %" value={`${summaryData?.total_result_pct ?? stats.totalPct}%`} valueClass={numClass(summaryData?.total_result_pct ?? stats.totalPct)} />
          <Card title="Net PnL" value={`${summaryData?.total_net_pnl_usdt != null ? Number(summaryData.total_net_pnl_usdt).toFixed(2) : stats.totalNet} USDT`} valueClass={numClass(summaryData?.total_net_pnl_usdt ?? stats.totalNet)} />
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 text-lg font-semibold text-emerald-200">
            Фильтры
          </h2>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <FilterSelect
              label="Status"
              value={statusFilter}
              onChange={setStatusFilter}
              options={["all", "published", "opened", "tp1", "breakeven", "closed", "expired", "rejected"]}
            />

            <FilterSelect
              label="Side"
              value={sideFilter}
              onChange={setSideFilter}
              options={["all", "long", "short"]}
            />

            <FilterSelect
              label="Grade"
              value={gradeFilter}
              onChange={setGradeFilter}
              options={["all", "A+", "A", "B", "C"]}
            />

            <FilterSelect
              label="Public"
              value={publicFilter}
              onChange={setPublicFilter}
              options={["all", "public", "private"]}
            />

            <FilterSelect
              label="Mode"
              value={modeFilter}
              onChange={setModeFilter}
              options={["all", "scalp", "trend"]}
            />
          </div>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-xl font-semibold text-emerald-200">
              Сигналы
            </h2>

            <span className="text-xs text-emerald-100/50">
              показано: {filtered.length} / {signals.length}
            </span>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {filtered.map((s) => (
              <SignalCard
                key={s.id}
                signal={s}
                onTestPrice={testLifecyclePrice}
                onCloseSignal={closeSignal}
                onCloseMarket={closeSignalMarket}
              />
            ))}

            {filtered.length === 0 && (
              <div className="rounded-2xl border border-emerald-950 bg-black/30 p-8 text-center text-emerald-100/50 xl:col-span-2">
                Сигналов по выбранным фильтрам нет
              </div>
            )}
          </div>
        </section>
    </AppShell>
  );
}

function SignalCard({
  signal: s,
  onTestPrice,
  onCloseSignal,
  onCloseMarket,
}: {
  signal: SignalItem;
  onTestPrice: (id: number, price?: number | null) => void;
  onCloseSignal: (id: number, result: number) => void;
  onCloseMarket: (id: number) => void;
}) {
  const isActive = ["published", "opened", "tp1", "breakeven"].includes(s.status);
  const isClosed = s.status === "closed";
  const plan = s.plan || {};

  return (
    <article className="rounded-2xl border border-emerald-950 bg-black/40 p-4">
      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-emerald-100/50">#{s.id}</span>
            <span className="text-lg font-bold text-emerald-100">{s.symbol}</span>
            <span className={s.side === "long" ? "text-sm text-emerald-300" : "text-sm text-red-300"}>
              {s.side}
            </span>
            <StatusBadge status={s.status} />
            <GradeBadge grade={s.grade} />
            <ExchangeBadge exchange={s.exchange} />
            <MlBadge ml={plan.ml} />
          </div>

          <div className="mt-2 max-w-full truncate text-xs text-emerald-100/50">
            {s.rationale || "-"}
          </div>
        </div>

        <div className="text-right text-xs text-emerald-100/50">
          <div>Conf: <span className="text-emerald-200">{fmt(s.confidence, 2)}%</span></div>
          <div>Public: <span className={s.is_public ? "text-emerald-300" : "text-yellow-300"}>{s.is_public ? "yes" : "no"}</span></div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <InfoBox title="Entry" value={`${fmt(s.entry_zone?.from, 4)} - ${fmt(s.entry_zone?.to, 4)}`} />
        <InfoBox title="Stop" value={fmt(s.stop_price, 4)} />
        <InfoBox title="TP1" value={fmt(s.tp?.tp1, 4)} />
        <InfoBox title="TP2" value={fmt(s.tp?.tp2, 4)} />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <InfoBox title="Qty" value={fmt(s.qty ?? plan.qty, 6)} />
        <InfoBox title="Margin" value={`${fmt(s.required_margin ?? plan.required_margin, 4)} USDT`} />
        <InfoBox title="RR TP1" value={fmt(s.net_rr_tp1 ?? plan.net_rr_tp1, 4)} />
        <InfoBox title="RR TP2" value={fmt(s.net_rr_tp2 ?? plan.net_rr_tp2, 4)} />
      </div>

      {/* (#ui-audit-2026-09-03) Диагностика решения. Всё это лежало в plan_json
          и не выводилось — а именно эти три величины сейчас определяют судьбу
          входа: tz_shadow стал ENFORCE-условием (adx_rising), r_mult показывает
          задуманную цель до обрезки, missed_profit — сколько оставили на столе. */}
      <TradeDiagnostics plan={plan} />

      <div className="mt-3 rounded-xl border border-emerald-950 bg-black/30 p-3">
        <div className="mb-2 flex items-center justify-between text-xs">
          <span className="font-semibold text-emerald-300">Trade Plan</span>
          {/* (#conv-pnl-rescale-2026-07-11) Числа плана = ВСЯ позиция при закрытии
              на уровне; на TP1 реально фиксируется 50% (строка ниже). */}
          <span className="text-emerald-100/40">$ = вся позиция · на TP1 фиксируется 50%</span>
        </div>

        <div className="grid grid-cols-1 gap-2 text-xs md:grid-cols-3">
          <div>
            <span className="text-emerald-100/50">TP1: </span>
            <span className="text-emerald-300">{fmt(s.net_pnl_tp1 ?? plan.net_pnl_tp1, 4)} USDT</span>
          </div>

          <div>
            <span className="text-emerald-100/50">TP2: </span>
            <span className="text-emerald-300">{fmt(s.net_pnl_tp2 ?? plan.net_pnl_tp2, 4)} USDT</span>
          </div>

          <div>
            <span className="text-emerald-100/50">SL: </span>
            <span className="text-red-300">{fmt(s.net_pnl_stop ?? plan.net_pnl_stop, 4)} USDT</span>
          </div>
        </div>

        {/* (#tp1-partial-2026-07-09) Реализованная частичная фиксация на TP1 */}
        {plan.tp1_partial && (
          <div className="mt-2 rounded-lg border border-emerald-800/60 bg-emerald-950/30 px-3 py-2 text-xs">
            <span className="font-semibold text-emerald-300">TP1 частично зафиксирован: </span>
            <span className="text-emerald-100/80">
              {fmt(plan.tp1_partial.closed_qty, 6)} @ {fmt(plan.tp1_partial.exit_price, 6)}
            </span>
            <span className={(plan.tp1_partial.net_pnl ?? 0) < 0 ? "ml-2 text-red-300" : "ml-2 text-emerald-300"}>
              net {fmt(plan.tp1_partial.net_pnl, 4)} USDT
            </span>
            <span className="ml-2 text-emerald-100/50">
              остаток {fmt(plan.tp1_partial.remaining_qty, 6)}
            </span>
          </div>
        )}

        {/* (#progressive-tp2-2026-09-03) TP2 — этап, а не потолок: доля остатка
            зафиксирована, хвост едет под трейлом с подтянутым стопом. */}
        {plan.tp2_partial && (
          <div className="mt-2 rounded-lg border border-cyan-800/60 bg-cyan-950/30 px-3 py-2 text-xs">
            <span className="font-semibold text-cyan-300">TP2 зафиксирован частично: </span>
            <span className="text-emerald-100/80">
              {fmt(plan.tp2_partial.closed_qty, 6)} @ {fmt(plan.tp2_partial.exit_price, 6)}
            </span>
            <span className={(plan.tp2_partial.net_pnl ?? 0) < 0 ? "ml-2 text-red-300" : "ml-2 text-emerald-300"}>
              net {fmt(plan.tp2_partial.net_pnl, 4)} USDT
            </span>
            <span className="ml-2 text-emerald-100/50">
              хвост {fmt(plan.tp2_partial.remaining_qty, 6)}
            </span>
            <div className="mt-1 text-cyan-100/60">
              Пик хвоста {fmt(plan.tp2_partial.peak_price, 6)} · трейл {fmt(plan.tp2_partial.buffer_pct, 3)}%
            </div>
          </div>
        )}
      </div>

      {isClosed && (
        <div className="mt-3 rounded-xl border border-emerald-950 bg-black/30 p-3">
          <div className="mb-2 text-xs font-semibold text-emerald-300">
            Close Result
          </div>

          <div className="grid grid-cols-1 gap-2 text-xs md:grid-cols-4">
            <div>
              <span className="text-emerald-100/50">Result: </span>
              <span className={numClass(s.result_pct)}>{fmt(s.result_pct, 4)}%</span>
            </div>

            <div>
              <span className="text-emerald-100/50">Net: </span>
              <span className={numClass(s.closed_net_pnl)}>{fmt(s.closed_net_pnl, 4)} USDT</span>
            </div>

            <div>
              <span className="text-emerald-100/50">Exit: </span>
              <span className="text-emerald-200">{fmt(s.closed_exit_price, 4)}</span>
            </div>

            <div>
              <span className="text-emerald-100/50">Costs: </span>
              <span className="text-yellow-300">{fmt(s.closed_total_cost, 4)} USDT</span>
            </div>
          </div>

          <div className="mt-2 text-xs text-emerald-100/60">
            Reason: <span className="text-emerald-200">{closeReasonLabel(s.closed_reason)}</span>
          </div>
        </div>
      )}

      <div className="mt-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="text-xs text-emerald-100/40">
          Created: {s.created_at || "-"}
        </div>

        <div className="flex flex-wrap gap-2">
          {isActive ? (
            <>
              {s.status === "published" && (
                <button
                  onClick={() => onTestPrice(s.id, s.entry_zone?.from)}
                  className="rounded-lg bg-cyan-700 px-3 py-1 text-xs font-semibold hover:bg-cyan-600"
                >
                  Entry
                </button>
              )}

              {s.status === "opened" && (
                <>
                  <button
                    onClick={() => onTestPrice(s.id, s.tp?.tp1)}
                    className="rounded-lg bg-blue-700 px-3 py-1 text-xs font-semibold hover:bg-blue-600"
                  >
                    TP1
                  </button>

                  <button
                    onClick={() => onTestPrice(s.id, s.stop_price)}
                    className="rounded-lg bg-red-700 px-3 py-1 text-xs font-semibold hover:bg-red-600"
                  >
                    Stop
                  </button>
                </>
              )}

              {(s.status === "tp1" || s.status === "breakeven") && (
                <>
                  <button
                    onClick={() => onTestPrice(s.id, s.tp?.tp2)}
                    className="rounded-lg bg-emerald-700 px-3 py-1 text-xs font-semibold hover:bg-emerald-600"
                  >
                    TP2
                  </button>

                  <button
                    onClick={() => onTestPrice(s.id, s.entry_zone?.from)}
                    className="rounded-lg bg-yellow-700 px-3 py-1 text-xs font-semibold hover:bg-yellow-600"
                  >
                    BE
                  </button>
                </>
              )}

              {/* (#manual-close-2026-07-09) Боевая кнопка: реальное закрытие по
                  живой цене через полный lifecycle (работает и в production).
                  Кнопки уровней (Entry/TP1/Stop/TP2/BE) — debug-инъекция цены,
                  в production отключены гейтом и теперь честно об этом скажут. */}
              {s.status !== "published" && (
                <button
                  onClick={() => onCloseMarket(s.id)}
                  className="rounded-lg bg-orange-600 px-3 py-1 text-xs font-bold text-slate-950 hover:bg-orange-500"
                >
                  Закрыть по рынку
                </button>
              )}

              <button
                onClick={() => onCloseSignal(s.id, 2.1)}
                className="rounded-lg bg-emerald-700 px-3 py-1 text-xs font-semibold hover:bg-emerald-600"
              >
                +2.1%
              </button>

              <button
                onClick={() => onCloseSignal(s.id, -1.0)}
                className="rounded-lg bg-red-700 px-3 py-1 text-xs font-semibold hover:bg-red-600"
              >
                -1.0%
              </button>
            </>
          ) : (
            <span className="text-xs text-emerald-100/40">{s.status}</span>
          )}
        </div>
      </div>
    </article>
  );
}

function Card({
  title,
  value,
  valueClass = "text-emerald-200",
}: {
  title: string;
  value: any;
  valueClass?: string;
}) {
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-4">
      <div className="text-xs text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-xl font-bold ${valueClass}`}>{value}</div>
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
}) {
  return (
    <label className="space-y-1">
      <div className="text-xs text-emerald-100/60">{label}</div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-xl border border-emerald-800 bg-black px-3 py-2 text-sm text-emerald-100 outline-none"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

function InfoBox({ title, value }: { title: string; value: any }) {
  return (
    <div className="rounded-xl border border-emerald-950 bg-black/30 p-3">
      <div className="text-[11px] text-emerald-100/50">{title}</div>
      <div className="mt-1 break-words text-sm font-semibold text-emerald-100">
        {value ?? "-"}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${statusClass(status)}`}>
      {status || "-"}
    </span>
  );
}

function MlBadge({ ml }: { ml?: any }) {
  // ml = { mode, ml_score, action }. Показываем только когда ML что-то посчитал.
  if (!ml || ml.ml_score == null) return null;
  const score = Number(ml.ml_score);
  const cls =
    score >= 0.6 ? "bg-emerald-600 text-white" : score >= 0.45 ? "bg-yellow-600 text-black" : "bg-red-700 text-white";
  return (
    <span
      className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}
      title={`ML ${ml.mode}: P(win)=${score.toFixed(3)}${ml.action ? " · " + ml.action : ""}`}
    >
      ML {score.toFixed(2)}
    </span>
  );
}

// Виды импульса на человеческом языке: разница между ними — это разница между
// «сила тренда развернулась вверх» и «осциллятор пересёк сигнальную».
const IMPULSE_KIND: Record<string, string> = {
  adx_turned_up: "ADX развернулся вверх",
  stoch_crossed: "Stoch пересёк сигнальную",
};

function TradeDiagnostics({ plan }: { plan: any }) {
  const tz = plan?.tz_shadow;
  const dyn = plan?.tp2_dynamic || plan?.setup_quality?.tp2_dynamic;
  const life = plan?.lifecycle;
  const reach = plan?.tp_reach;
  const conf = plan?.confidence;

  const hasTz = tz && tz.evaluated;
  const missed = life?.missed_profit_pct;

  if (!hasTz && !dyn && missed == null && !reach && !conf) return null;

  return (
    <div className="mt-3 rounded-xl border border-emerald-950 bg-black/30 p-3 text-xs">
      <div className="mb-2 font-semibold text-emerald-300">Диагностика решения</div>

      <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
        {hasTz && (
          <div>
            <span className="text-emerald-100/50">Условия ТЗ: </span>
            {tz.would_pass ? (
              <span className="text-emerald-300">все пройдены</span>
            ) : (
              <span className="text-yellow-300">
                не пройдено {(tz.failed || []).length}
              </span>
            )}
            {/* (#entry-impulse-2026-09-04) Кандидат — состояние, живущее сутками;
                условия ТЗ — события длиной в бар. Требовать их одновременно
                значит почти всегда опаздывать: 68 отказов из 71 по adx_not_rising.
                Защёлка помнит импульс, пока состояние подтверждается, и её
                возраст объясняет, ПОЧЕМУ вход прошёл при падающем ADX. */}
            {tz.impulse_latch && (
              <div
                className="mt-1 text-[11px]"
                title="Импульс на младшем ТФ случается раньше, чем тренд проступит на 4h и 1h. Защёлка держит событие, пока состояние подтверждается; ни одно условие при этом не ослаблено."
              >
                <span className="text-emerald-100/50">Импульс: </span>
                {tz.impulse_latch.live ? (
                  <span className="text-emerald-300">
                    {IMPULSE_KIND[tz.impulse_latch.impulse?.kind] || tz.impulse_latch.impulse?.kind}
                    {tz.impulse_latch.impulse?.age_sec != null &&
                      ` ${Math.round(tz.impulse_latch.impulse.age_sec / 60)} мин назад`}
                  </span>
                ) : (
                  <span className="text-emerald-100/40">не было в окне</span>
                )}
                {tz.impulse_latch.mode === "shadow" && (
                  <span className="text-emerald-100/30"> · наблюдение</span>
                )}
              </div>
            )}

            {!tz.would_pass && (tz.failed || []).length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {(tz.failed as string[]).map((f) => (
                  <span
                    key={f}
                    className={`rounded px-1.5 py-0.5 text-[10px] ${
                      f.startsWith("adx_not_rising")
                        ? "bg-red-900/70 text-red-200"
                        : "bg-emerald-950 text-emerald-100/70"
                    }`}
                    title={f.startsWith("adx_not_rising") ? "условие ENFORCE — блокирует вход" : "наблюдение"}
                  >
                    {f}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {/* (#confidence-ratchet-2026-09-04) Уверенность — не одно число, а две
            ноги: оценка рынка и чек-лист сетапа. Пока в карточке стояло только
            итоговое значение, «обе ноги согласны и высоки» выглядело так же,
            как «ноги спорят, взяли большую» — а это и наполняло ведро A. */}
        {conf && (
          <div>
            <span className="text-emerald-100/50">Уверенность: </span>
            <span className="text-emerald-200">{fmt(conf.effective, 1)}</span>
            {conf.setup_leg != null ? (
              <div
                className="mt-1 text-[11px] text-emerald-100/60"
                title="Итог — среднее двух ног. Раньше бралась большая, поэтому расхождение поднимало уверенность вместо того, чтобы её снижать."
              >
                рынок {fmt(conf.base, 1)} · чек-лист {fmt(conf.setup_leg, 1)}
                {conf.leg_gap != null && (
                  <span className={Math.abs(conf.leg_gap) >= 15 ? "text-yellow-300" : ""}>
                    {" "}· расхождение {fmt(conf.leg_gap, 1)}
                  </span>
                )}
              </div>
            ) : (
              <div className="mt-1 text-[11px] text-emerald-100/40" title="Чек-лист не дотянул до порога ветки — уверенность равна оценке рынка.">
                только оценка рынка
              </div>
            )}
          </div>
        )}

        {dyn && (
          <div>
            <span className="text-emerald-100/50">TP2 множитель: </span>
            <span className="text-emerald-200">{fmt(dyn.r_mult, 2)}R</span>
            <span className="text-emerald-100/40"> (база {fmt(dyn.base_r_mult, 1)})</span>
          </div>
        )}

        {reach && (
          <div>
            <span className="text-emerald-100/50">Достижимость TP2: </span>
            <span className={reach.allowed ? "text-emerald-300" : "text-yellow-300"}>
              {fmt((reach.tp2_hit_rate ?? 0) * 100, 1)}% / нужно {fmt((reach.required_hit_rate ?? 0) * 100, 1)}%
            </span>
          </div>
        )}

        {missed != null && (
          <div>
            <span className="text-emerald-100/50">Оставлено на столе: </span>
            <span className={Number(missed) > 0.5 ? "text-yellow-300" : "text-emerald-100/70"}>
              {fmt(missed, 2)}%
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function ExchangeBadge({ exchange }: { exchange?: string | null }) {
  // (#okx-satellite-exchange-routing-2026-09-02) Метка, на какой бирже сигнал
  // реально открыт — не путать с текущей ACTIVE_EXCHANGE, которая может отличаться.
  const ex = (exchange || "htx").toLowerCase();
  const cls = ex === "okx" ? "bg-sky-700 text-white" : "bg-slate-700 text-white";
  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold uppercase ${cls}`}>
      {ex}
    </span>
  );
}


function statusClass(status?: string | null) {
  if (status === "opened") return "bg-blue-700 text-white";
  if (status === "published") return "bg-cyan-700 text-white";
  if (status === "tp1" || status === "breakeven") return "bg-emerald-700 text-white";
  if (status === "closed") return "bg-emerald-950 text-emerald-200";
  if (status === "expired") return "bg-yellow-700 text-black";
  if (status === "rejected") return "bg-red-800 text-white";
  return "bg-emerald-950 text-emerald-200";
}

function numClass(value: any) {
  const n = Number(value || 0);
  if (n > 0) return "text-emerald-300";
  if (n < 0) return "text-red-300";
  return "text-emerald-100/70";
}

function fmt(value: any, digits = 4) {
  if (value === null || value === undefined || value === "") return "-";

  const n = Number(value);

  if (Number.isNaN(n)) return String(value);

  return n.toFixed(digits).replace(/\.?0+$/, "");
}