"use client";

import { useEffect, useMemo, useState } from "react";
import ExchangeBadge from "../../components/ExchangeBadge";
import GradeBadge from "../../components/GradeBadge";
import ImpulseLatchLine from "../../components/ImpulseLatchLine";
import { RefreshCw } from "lucide-react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import { assertOk, reportActionError as reportError } from "../../lib/apiAction";
import { closeReasonLabel } from "../../lib/closeReasons";

type SignalItem = any;

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

  // Подсказка про debug-гейт остаётся здесь: она про кнопки ИМЕННО этой
  // страницы (инъекция цены), а не про действия вообще. Сами обработчики —
  // общие: страницы подписчиков и платежей своих не имели, и действия там
  // падали молча.
  function reportActionError(e: any) {
    reportError(e, {
      debug_endpoints_disabled_in_production:
        "Это debug-кнопка (инъекция тестовой цены) — в production она отключена. " +
        "Для реального закрытия используй «Закрыть по рынку».",
    });
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

  // (#manual-result-2026-09-07) Здесь стояло закрытие с ЗАДАННЫМ процентом, а
  // в разметке — две кнопки с зашитыми «+2.1%» и «−1.0%». Бэкенд выводит из
  // процента цену выхода и проводит сделку полным lifecycle: позиция, PnL,
  // Telegram, метка в trade_outcomes.jsonl. То есть выдуманный исход попадал в
  // журнал наравне с измеренными — в разбор по причинам, в форензику стопов, в
  // обучающую выборку ML. Гейта production у ручки нет, в отличие от кнопок
  // инъекции цены.
  //
  // Честное закрытие делает «Закрыть по рынку» (живая цена). Для неоткрытого
  // сигнала остаётся отмена: бэкенд переводит published → expired и результата
  // не считает вовсе.
  async function cancelSignal(id: number) {
    if (!confirmDanger(`Сигнал #${id} будет отменён (published → expired).`)) return;

    try {
      assertOk(await apiPost(`/signals/${id}/close`, {
        result_pct: 0,
        reason: "manual_cancel",
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

    // wins/losses/totalCosts тут считались и не выводились ни одной карточкой.
    return {
      total: signals.length,
      closed: closed.length,
      active: signals.filter((s) => ["published", "opened", "tp1", "breakeven"].includes(s.status)).length,
      expired: signals.filter((s) => s.status === "expired").length,
      rejected: signals.filter((s) => s.status === "rejected").length,
      winrate: closed.length ? ((wins.length / closed.length) * 100).toFixed(2) : "0.00",
      totalPct: totalPct.toFixed(4),
      totalNet: totalNet.toFixed(2),
    };
  }, [signals]);

  // (#phantom-fill-2026-07-25) Честный PnL — тот же, что на главной и в
  // аналитике. Здесь оставался сырой: ветка tp2_reached книжит полную цену TP2,
  // закрываясь на 92% пути, и завышение попадает ТОЛЬКО в выигрышные сделки.
  // Два экрана давали два разных числа за один и тот же период — ровно то, что
  // чинили на главной 25.07 и не довели до журнала.
  const honestWinrate = summaryData?.winrate_honest ?? summaryData?.winrate;
  const honestNet = summaryData?.total_net_pnl_honest_usdt ?? summaryData?.total_net_pnl_usdt;

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
          <Card title="Winrate" value={`${honestWinrate ?? stats.winrate}%`} />
          <Card title="Итог %" value={`${summaryData?.total_result_pct ?? stats.totalPct}%`} valueClass={numClass(summaryData?.total_result_pct ?? stats.totalPct)} />
          <Card title="Net PnL" value={`${honestNet != null ? Number(honestNet).toFixed(2) : stats.totalNet} USDT`} valueClass={numClass(honestNet ?? stats.totalNet)} />
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
                onCancelSignal={cancelSignal}
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
  onCancelSignal,
  onCloseMarket,
}: {
  signal: SignalItem;
  onTestPrice: (id: number, price?: number | null) => void;
  onCancelSignal: (id: number) => void;
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

              {/* Кнопки «+2.1%» и «−1.0%» вписывали в журнал выдуманный
                  результат — см. cancelSignal выше. Для открытой сделки честное
                  закрытие делает «Закрыть по рынку», для неоткрытой — отмена. */}
              {s.status === "published" && (
                <button
                  onClick={() => onCancelSignal(s.id)}
                  className="rounded-lg bg-yellow-700 px-3 py-1 text-xs font-bold text-slate-950 hover:bg-yellow-600"
                >
                  Отменить
                </button>
              )}
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
  // (#ml-badge-2026-09-07) Цвет — утверждение о результате. Значок красил
  // ≥0.6 зелёным, ≥0.45 жёлтым, ниже красным, хотя разделение этой оси на
  // закрытых сделках не измерялось ни разу.
  //
  // Тот же разбор этажом ниже уже снял палитру с GradeBadge — там замер
  // показал, что она была ПЕРЕВЁРНУТА. Рисовать вердикт на непроверенной оси в
  // том же файле, где соседняя ось на этом попалась, оснований нет. Тем более
  // что MLScorer подмешивается в уверенность с весом 0.3 независимо от
  // ML_MODE: на вход он влияет, а как именно — не замерено.
  if (!ml || ml.ml_score == null) return null;
  const score = Number(ml.ml_score);
  return (
    <span
      className="rounded-lg border border-slate-600 bg-slate-800 px-2 py-1 text-xs font-semibold text-slate-100"
      title={
        `P(win) по MLScorer = ${score.toFixed(3)}` +
        (ml.mode ? ` · режим ${ml.mode}` : "") +
        (ml.action ? ` · ${ml.action}` : "") +
        ". Разделение этой оси на закрытых сделках не измерено — цвет не ставится."
      }
    >
      ML {score.toFixed(2)}
    </span>
  );
}

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
            {/* Строка защёлки общая с лентой решений — см. ImpulseLatchLine. */}
            <ImpulseLatchLine latch={tz.impulse_latch} />

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
            {/* (#ml-blend-visible-2026-09-06) Шаг ML. На #475 карточка
                показывала 71.7 здесь и 60.67 в шапке — два разных числа под
                одним словом «уверенность». Разница в этом смешивании, и без
                него шапка выглядела опечаткой. */}
            {conf.ml_blend && (
              <div
                className="mt-1 text-[11px] text-yellow-200/70"
                title="MLScorer смешивается в уверенность с весом 0.3 НЕЗАВИСИМО от ML_MODE. Именно это число попадает в грейд и в гейт входа."
              >
                после ML: {fmt(conf.ml_blend.after_ml, 1)}{" "}
                <span className="text-emerald-100/40">
                  (ML {fmt(conf.ml_blend.ml_confidence, 0)} · вес{" "}
                  {fmt(conf.ml_blend.weight, 2)} · режим {conf.ml_blend.ml_mode})
                </span>
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

        {/* (#shadow-verdict-2026-09-06) В shadow `allowed` всегда true — гейт
            считает, но не блокирует. Карточка красила по нему, поэтому зелёным
            выходили и сделки, где частота вдвое ниже требуемой: у #482 стояло
            17.4% против нужных 29.8% и при этом зелёный «пропущено».
            Собственный вердикт гейта лежит рядом, в `would_block`. */}
        {reach && (
          <div>
            <span className="text-emerald-100/50">Достижимость TP2: </span>
            <span
              className={
                reach.would_block == null
                  ? "text-emerald-200"
                  : reach.would_block
                  ? "text-yellow-300"
                  : "text-emerald-300"
              }
            >
              {fmt((reach.tp2_hit_rate ?? 0) * 100, 1)}% / нужно {fmt((reach.required_hit_rate ?? 0) * 100, 1)}%
            </span>
            {reach.tp2_hit_rate_uncensored != null && (
              <div
                className="mt-1 text-[11px] text-emerald-100/50"
                title="Та же частота без наших собственных ранних выходов. Разрыв с сырой — цена, которую вход платит за решения контура выхода."
              >
                без цензуры {fmt(reach.tp2_hit_rate_uncensored * 100, 1)}%
                {reach.uncensored_sample != null && ` на ${reach.uncensored_sample} набл.`}
              </div>
            )}
            {/* У сделок старше 06.09 вердикта в плане нет. Отсутствие замера не
                должно выглядеть как «гейт пропустил бы». */}
            {reach.would_block != null && (
              <div className={`mt-1 text-[11px] ${reach.would_block ? "text-yellow-200/70" : "text-emerald-100/50"}`}>
                {reach.would_block ? "гейт остановил бы" : "гейт пропустил бы"}
                {reach.reason === "mode_shadow" && (
                  <span className="text-emerald-100/40"> · режим наблюдения</span>
                )}
              </div>
            )}
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