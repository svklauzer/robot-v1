"use client";

import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";

type SignalItem = any;

// Человекочитаемые ярлыки причин закрытия (синхронизация с правками бэка).
const CLOSE_REASON_LABELS: Record<string, string> = {
  tp2_reached: "TP2 достигнут",
  tp1_reached: "TP1 достигнут",
  stop_loss: "Стоп",
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
};

function closeReasonLabel(code: string | null | undefined): string {
  if (!code) return "-";
  return CLOSE_REASON_LABELS[code] || code;
}

export default function SignalsPage() {
  const [signals, setSignals] = useState<SignalItem[]>([]);
  const [loading, setLoading] = useState(false);

  const [statusFilter, setStatusFilter] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [gradeFilter, setGradeFilter] = useState("all");
  const [publicFilter, setPublicFilter] = useState("all");
  const [modeFilter, setModeFilter] = useState("all");

  async function loadSignals() {
    setLoading(true);

    try {
      const data = await apiGet("/signals?limit=100&offset=0");
      setSignals(Array.isArray(data) ? data : data?.items || []);
    } finally {
      setLoading(false);
    }
  }

  function confirmDanger(message: string) {
    return window.confirm(`⚠️ ${message}\n\nПродолжить?`);
  }

  async function testLifecyclePrice(id: number, price?: number | null) {
    if (price === undefined || price === null || Number.isNaN(Number(price))) {
      alert("Нет цены для lifecycle-теста");
      return;
    }

    if (!confirmDanger(`Lifecycle-test изменит состояние сигнала #${id} по цене ${price}.`)) return;

    await apiPost("/robot/test-lifecycle-price", {
      signal_id: id,
      price: Number(price),
    });

    await loadSignals();
  }

  async function closeSignal(id: number, result: number) {
    if (!confirmDanger(`Сигнал #${id} будет вручную закрыт с результатом ${result}%.`)) return;

    await apiPost(`/signals/${id}/close`, {
      result_pct: result,
      reason: result > 0 ? "manual_profit_close" : "manual_loss_close",
    });

    await loadSignals();
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

        <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
          <Card title="Сигналов" value={stats.total} />
          <Card title="Активные" value={stats.active} />
          <Card title="Закрыто" value={stats.closed} />
          <Card title="Expired" value={stats.expired} />
          <Card title="Rejected" value={stats.rejected} />
          <Card title="Winrate" value={`${stats.winrate}%`} />
          <Card title="Итог %" value={`${stats.totalPct}%`} valueClass={numClass(stats.totalPct)} />
          <Card title="Net PnL" value={`${stats.totalNet} USDT`} valueClass={numClass(stats.totalNet)} />
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
}: {
  signal: SignalItem;
  onTestPrice: (id: number, price?: number | null) => void;
  onCloseSignal: (id: number, result: number) => void;
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

      <div className="mt-3 rounded-xl border border-emerald-950 bg-black/30 p-3">
        <div className="mb-2 text-xs font-semibold text-emerald-300">
          Trade Plan
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

function GradeBadge({ grade }: { grade?: string | null }) {
  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${gradeClass(grade)}`}>
      {grade || "-"}
    </span>
  );
}

function gradeClass(grade?: string | null) {
  if (grade === "A+") return "bg-emerald-500 text-black";
  if (grade === "A") return "bg-emerald-800 text-emerald-100";
  if (grade === "B") return "bg-yellow-600 text-black";
  if (grade === "C") return "bg-red-700 text-white";
  return "bg-emerald-950 text-emerald-200";
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