"use client";

/* (#backtest-page-2026-07-27) Витрина для того, что уже считалось, но было
   доступно только через curl: exit_replay (A/B выходов по записанным
   траекториям) и symbol_policy_replay.

   Почему это не «ещё одна красивая страница». Инструмент существовал с
   #audit-traj, но умел только scalp/range — то есть молча пропускал весь
   трендовый контур, 16 сделок из 18 на боевой выборке. Течь искали там, где
   её не видно. Профиль trend добавлен, и он сразу показал разницу между
   MIN_PROTECTIVE 1.80 и 0.40.

   Дисциплина страницы: сначала размер выборки и проверка на подгонку, только
   потом таблица вариантов. Порядок не косметический — таблицу вариантов без
   этих двух чисел читать нельзя. */

import { useEffect, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { FlaskConical, RefreshCw, TriangleAlert } from "lucide-react";

type Profile = "trend" | "scalp";

export default function BacktestPage() {
  const [profile, setProfile] = useState<Profile>("trend");
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await apiGet(`/ml/exit-replay?profile=${profile}&limit=2000`));
    } catch (e: any) {
      setError(String(e?.message || e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [profile]);

  const ok = data?.status === "ok";
  const n = data?.trades_replayed ?? 0;
  const overfit = data?.overfit_check;
  const isTrend = profile === "trend";

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
            <FlaskConical />
            Back test — выходы по реальным траекториям
          </h1>
          <p className="mt-2 max-w-3xl text-sm text-emerald-100/60">
            Перебор параметров выхода по записанным траекториям закрытых сделок. Replay может закрыть
            сделку только <b>раньше</b> факта и книжит выход по текущей точке — «улучшить задним
            числом» нельзя. Издержки у всех вариантов одинаковы, поэтому сравнение идёт по gross-%.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-xl border border-emerald-800 p-1">
            {(["trend", "scalp"] as Profile[]).map((p) => (
              <button
                key={p}
                onClick={() => setProfile(p)}
                className={
                  profile === p
                    ? "rounded-lg bg-emerald-400 px-3 py-1.5 text-sm font-bold text-slate-950"
                    : "rounded-lg px-3 py-1.5 text-sm font-semibold text-emerald-100/70 hover:text-emerald-50"
                }
              >
                {p === "trend" ? "Тренд / CRT" : "Скальп / рэйндж"}
              </button>
            ))}
          </div>
          <button
            onClick={load}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={16} />
            {loading ? "Считаю..." : "Пересчитать"}
          </button>
        </div>
      </header>

      {error && (
        <section className="rounded-2xl border border-red-900/70 bg-red-950/30 p-5 text-sm text-red-100">
          Эндпоинт недоступен: {error}
        </section>
      )}

      {data && !ok && (
        <section className="rounded-2xl border border-yellow-900/70 bg-yellow-950/20 p-5">
          <h2 className="mb-2 font-semibold text-yellow-200">Данных пока нет</h2>
          <p className="text-sm text-yellow-100/70">{data.message || "Нет сделок с траекторией."}</p>
          {data.skipped_no_trajectory > 0 && (
            <p className="mt-2 text-xs text-yellow-100/50">
              Пропущено без траектории: {data.skipped_no_trajectory}
            </p>
          )}
          {/* «Данных нет» обязано объяснять себя: пустой файл логгера и пустая
              БД — разные диагнозы, а выглядят одинаково. */}
          {data.sources && (
            <p className="mt-2 text-xs text-yellow-100/50">
              Источник: БД {data.sources.db_rows} строк · файл логгера {data.sources.file_rows} ·
              в работе {data.sources.used}.
            </p>
          )}
        </section>
      )}

      {ok && (
        <>
          {/* Размер выборки идёт ПЕРВЫМ и намеренно: всё остальное на странице
              читается только через него. */}
          <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Card
              title="Сделок в выборке"
              value={n}
              tone={n >= 100 ? "good" : n >= 30 ? "warn" : "bad"}
              note={
                n < 30
                  ? "меньше 30 — это не доказательство, а наблюдение"
                  : n < 100
                    ? "хватает на гипотезу, мало на решение"
                    : "выборка рабочая"
              }
            />
            <Card
              title="Факт (честный)"
              value={`${data.actual_total_pct}%`}
              tone={data.actual_total_pct > 0 ? "good" : "bad"}
              note={`${data.actual_avg_pct ?? "—"}% на сделку${
                data.phantom_fill_trades ? ` · фантомов снято: ${data.phantom_fill_trades}` : ""
              }`}
            />
            <Card
              title="Лучший вариант"
              value={`${data.best?.total_pct}%`}
              tone={data.best?.total_pct > data.actual_total_pct ? "good" : "warn"}
              note={`${data.best?.delta_vs_actual_pct >= 0 ? "+" : ""}${
                data.best?.delta_vs_actual_pct
              }% к факту · winrate ${data.best?.winrate_pct}%`}
            />
            <Card
              title="Текущий конфиг"
              value={data.current_total_pct != null ? `${data.current_total_pct}%` : "—"}
              tone={data.current_rank && data.current_rank <= 10 ? "good" : "warn"}
              note={
                data.current_rank
                  ? `место ${data.current_rank} из ${data.variants_count ?? "?"}`
                  : "текущих значений нет в сетке перебора"
              }
            />
          </section>

          {/* (#replay-partials-2026-09-05) Чем моделировали — прежде таблицы и
              прежде проверки на подгонку. До этой правки страница выглядела
              одинаково авторитетно и когда воспроизводила сегодняшний выход, и
              когда отвечала про лестницу от 27.07: частичные фиксации на TP1 и
              TP2 в модели отсутствовали вовсе. Читателю нужно знать, про какую
              машину ответ, прежде чем читать сам ответ. */}
          {data.exit_model && (
            <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
              <h2 className="text-lg font-semibold text-emerald-100">Что моделировали</h2>
              <p className="mt-2 text-sm text-emerald-100/70">
                Лестница: {(data.exit_model.ladder || []).join(" → ")}. Частичные фиксации берутся
                живые и одинаковые для всех вариантов: на TP1 закрывается{" "}
                <b>{Math.round((data.exit_model.tp1_partial_share ?? 0) * 100)}%</b>, на{" "}
                <b>{Math.round((data.exit_model.tp2_trigger_share ?? 0) * 100)}%</b> пути до TP2 —
                ещё <b>{Math.round((data.exit_model.tp2_partial_share ?? 0) * 100)}%</b> остатка.
                Перебирается только лестница.
              </p>
              {data.exit_model.trades_without_targets > 0 && (
                <p className="mt-2 text-xs text-yellow-200/80">
                  У {data.exit_model.trades_without_targets} из{" "}
                  {(data.exit_model.trades_with_targets ?? 0) +
                    data.exit_model.trades_without_targets}{" "}
                  сделок нет геометрии целей — они посчитаны по старой лестнице, без частичных
                  фиксаций. Частично смоделированная выборка не должна читаться как полная.
                </p>
              )}
              {data.sources && (
                <p className="mt-2 text-xs text-emerald-100/40">
                  Источник: БД {data.sources.db_rows} строк · файл логгера {data.sources.file_rows}
                  {data.sources.file_without_id > 0 &&
                    ` (без signal_id: ${data.sources.file_without_id})`}{" "}
                  · в работе {data.sources.used}.
                </p>
              )}
            </section>
          )}

          {/* Проверка на подгонку — до таблицы, а не после. */}
          {overfit && (
            <section
              className={`rounded-2xl border p-5 ${
                overfit.checked === false
                  ? "border-slate-700 bg-slate-900/40"
                  : overfit.robust
                    ? "border-emerald-900/70 bg-emerald-950/20"
                    : "border-red-900/70 bg-red-950/25"
              }`}
            >
              <h2 className="flex items-center gap-2 text-lg font-semibold text-emerald-100">
                {!overfit.robust && overfit.checked !== false && <TriangleAlert size={18} className="text-red-300" />}
                Проверка на подгонку
              </h2>
              {overfit.checked === false ? (
                <p className="mt-2 text-sm text-slate-300">{overfit.reason}</p>
              ) : (
                <>
                  <p className={`mt-2 text-sm ${overfit.robust ? "text-emerald-200" : "text-red-200"}`}>
                    {overfit.verdict}
                  </p>
                  <p className="mt-2 text-xs text-emerald-100/50">
                    Выборка режется хронологически пополам. Лидер, найденный на всей истории, обязан
                    оставаться в лидерах на каждой половине отдельно — иначе он обслуживает пару
                    удачных исходов, а не закономерность.
                  </p>
                  <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                    {(["first_half", "second_half"] as const).map((k) => (
                      <div key={k} className="rounded-xl border border-emerald-950 bg-black/25 p-3">
                        <div className="text-xs uppercase tracking-wide text-emerald-100/40">
                          {k === "first_half" ? "первая половина" : "вторая половина"} · {overfit[k]?.trades} сделок
                        </div>
                        <Row label="Место лидера" value={overfit[k]?.leader_rank ?? "не найден"} />
                        <Row label="Его результат" value={fmtPct(overfit[k]?.leader_total_pct)} />
                        <Row label="Лучший на половине" value={fmtPct(overfit[k]?.half_best_total_pct)} />
                      </div>
                    ))}
                  </div>
                </>
              )}
            </section>
          )}

          <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
            <h2 className="mb-1 text-xl font-semibold text-emerald-200">Варианты</h2>
            <p className="mb-4 text-sm text-emerald-100/50">
              {isTrend
                ? "Лестница выхода: замок безубытка → полоса захвата → ride-трейл. Ось min_prot содержит прежнее боевое 1.80 и правку 0.40 — разница между ними и есть измеренный эффект правки."
                : "Скальп-профиль: arm / giveback / time-stop."}
              {data.variants_count > (data.variants?.length ?? 0) &&
                ` Показаны ${data.variants?.length} из ${data.variants_count}.`}
            </p>

            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  <tr className="border-b border-emerald-900 text-left text-xs uppercase tracking-wide text-emerald-100/40">
                    <th className="py-2 pr-3">#</th>
                    {isTrend ? (
                      <>
                        <th className="pr-3">BE arm</th>
                        <th className="pr-3">BE floor</th>
                        <th className="pr-3">band arm</th>
                        <th className="pr-3">giveback</th>
                        <th className="pr-3">ride trail</th>
                        <th className="pr-3">min prot</th>
                      </>
                    ) : (
                      <>
                        <th className="pr-3">arm</th>
                        <th className="pr-3">giveback</th>
                        <th className="pr-3">time stop</th>
                      </>
                    )}
                    <th className="pr-3">итого</th>
                    <th className="pr-3">Δ к факту</th>
                    <th className="pr-3">winrate</th>
                  </tr>
                </thead>
                <tbody>
                  {(data.variants || []).map((v: any, i: number) => {
                    const isCurrent = sameConfig(v, data.current_config, isTrend);
                    return (
                      <tr
                        key={i}
                        className={`border-b border-emerald-950 ${
                          isCurrent ? "bg-cyan-950/30" : i === 0 ? "bg-emerald-950/25" : ""
                        }`}
                      >
                        <td className="py-2 pr-3 text-emerald-100/40">
                          {i + 1}
                          {isCurrent && <span className="ml-1 text-cyan-300">сейчас</span>}
                        </td>
                        {isTrend ? (
                          <>
                            <td className="pr-3">{v.be_arm_pct}</td>
                            <td className="pr-3">{v.be_floor_pct}</td>
                            <td className="pr-3">{v.band_arm_pct}</td>
                            <td className="pr-3">{v.band_giveback_share}</td>
                            <td className="pr-3">{v.ride_trail_share}</td>
                            <td className={`pr-3 ${v.min_protective_pct >= 1.8 ? "text-red-300" : "text-emerald-300"}`}>
                              {v.min_protective_pct}
                            </td>
                          </>
                        ) : (
                          <>
                            <td className="pr-3">{v.arm_pct}</td>
                            <td className="pr-3">{v.giveback_share}</td>
                            <td className="pr-3">{v.time_stop_min ?? "выкл"}</td>
                          </>
                        )}
                        <td className={`pr-3 font-semibold ${v.total_pct >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                          {v.total_pct}%
                        </td>
                        <td className={`pr-3 ${(v.delta_vs_actual_pct ?? 0) >= 0 ? "text-emerald-300/70" : "text-red-300/70"}`}>
                          {v.delta_vs_actual_pct != null
                            ? `${v.delta_vs_actual_pct >= 0 ? "+" : ""}${v.delta_vs_actual_pct}%`
                            : "—"}
                        </td>
                        <td className="pr-3 text-emerald-100/70">{v.winrate_pct}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          <section className="rounded-2xl border border-emerald-950 bg-black/20 p-4">
            <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-emerald-100/40">
              Как это считается
            </h3>
            <p className="text-xs leading-relaxed text-emerald-100/50">{data.note}</p>
          </section>
        </>
      )}
    </AppShell>
  );
}

function fmtPct(v: any) {
  return v == null ? "—" : `${v}%`;
}

function sameConfig(v: any, cur: any, isTrend: boolean): boolean {
  if (!cur) return false;
  const keys = isTrend
    ? ["be_arm_pct", "be_floor_pct", "band_arm_pct", "band_giveback_share", "ride_trail_share", "min_protective_pct"]
    : ["arm_pct", "giveback_share", "time_stop_min"];
  return keys.every((k) => v[k] === cur[k]);
}

function Row({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex items-center justify-between border-b border-emerald-950 py-1.5 text-sm last:border-b-0">
      <span className="text-emerald-100/60">{label}</span>
      <span className="font-semibold text-emerald-200">{value}</span>
    </div>
  );
}

function Card({ title, value, tone, note }: { title: string; value: any; tone: "good" | "warn" | "bad"; note?: string }) {
  const cls = tone === "good" ? "text-emerald-300" : tone === "warn" ? "text-yellow-300" : "text-red-300";
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${cls}`}>{value}</div>
      {note && <div className="mt-2 text-xs text-emerald-100/45">{note}</div>}
    </div>
  );
}
