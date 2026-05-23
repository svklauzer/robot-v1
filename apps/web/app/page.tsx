// /C:\Users\svk\robot-v1\apps\web\app\page.tsx

"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { Play, Square, Send, RefreshCw } from "lucide-react";
import Nav from "../components/Nav";


export default function DashboardPage() {
  const [botState, setBotState] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [signals, setSignals] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  async function loadAll() {
    setLoading(true);
    try {
      const [state, summary, sigs] = await Promise.all([
        apiGet("/bot/state"),
        apiGet("/analytics/summary"),
        apiGet("/signals?limit=10&offset=0"),
      ]);

      setBotState(state);
      setAnalytics(summary);

      if (Array.isArray(sigs)) {
        setSignals(sigs);
      } else {
        setSignals(sigs?.items || []);
      }
    } finally {
      setLoading(false);
    }
  }

  function confirmDanger(message: string) {
    return window.confirm(`⚠️ ${message}\n\nПродолжить?`);
  }

  async function startBot() {
    await apiPost("/bot/start");
    await loadAll();
  }

  async function stopBot() {
    await apiPost("/bot/stop");
    await loadAll();
  }

  async function forceLiveNearSignal() {
    if (!confirmDanger("Будет создан и отправлен LIVE NEAR сигнал в Telegram.")) return;

    await apiPost("/robot/force-live-near-signal");
    await loadAll();
  }

  async function forceSignal() {
    if (!confirmDanger("Будет создан и отправлен тестовый PAPER сигнал в Telegram.")) return;

    await apiPost("/robot/force-paper-signal");
    await loadAll();
  }

  async function runLifecycleOnce() {
    await apiPost("/robot/run-lifecycle-once");
    await loadAll();
  }  

  async function forceScalpSignal() {
    if (!confirmDanger("Будет создан SCALP lifecycle test сигнал и может быть отправлен в Telegram.")) return;

    await apiPost("/robot/force-scalp-signal");
    await loadAll();
  }

  async function sendAllReports() {
    if (!confirmDanger("Будут отправлены отчёты во все каналы: FREE, VIP и Owner.")) return;

    await apiPost("/reports/send-all?hours=24");
    alert("Отчёты отправлены");
  }

  async function sendVipReport() {
    if (!confirmDanger("Будет отправлен VIP отчёт.")) return;

    await apiPost("/reports/send-vip?hours=24");
    alert("VIP отчёт отправлен");
  }

  async function sendFreeReport() {
    if (!confirmDanger("Будет отправлен FREE отчёт.")) return;

    await apiPost("/reports/send-free?hours=24");
    alert("FREE отчёт отправлен");
  }

  async function checkExpirations() {
    await apiPost("/subscribers/check-expirations");
    await loadAll();
    alert("Проверка подписок выполнена");
  }

  async function forceValidTradeSignal() {
    if (!confirmDanger("Будет создан valid trade signal и может быть отправлен в Telegram.")) return;

    const res = await apiPost("/robot/force-valid-trade-signal");

    if (res?.status === "rejected_before_publish") {
      alert(`Сигнал отклонён до публикации: ${res.reason}`);
    }

    await loadAll();
  }  
  
  async function testLifecyclePrice(id: number, price?: number | null) {
    if (price === undefined || price === null || Number.isNaN(Number(price))) {
      alert("Нет цены для lifecycle-теста");
      return;
    }

    await apiPost("/robot/test-lifecycle-price", {
      signal_id: id,
      price: Number(price)
    });

    await loadAll();
  }

  async function closeSignal(id: number, result: number) {
    if (!confirmDanger(`Сигнал #${id} будет вручную закрыт с результатом ${result}%.`)) return;

    await apiPost(`/signals/${id}/close`, {
      result_pct: result,
      reason: result > 0 ? "manual_profit_close" : "manual_loss_close"
    });

    await loadAll();
  }

  useEffect(() => {
    loadAll();
    const timer = setInterval(loadAll, 5000);
    return () => clearInterval(timer);
  }, []);

  const bot = botState?.bot;
  const safeSignals = Array.isArray(signals) ? signals : [];

  function gradeClass(grade?: string | null) {
    if (grade === "A+") return "bg-emerald-500 text-black";
    if (grade === "A") return "bg-emerald-800 text-emerald-100";
    if (grade === "B") return "bg-yellow-600 text-black";
    if (grade === "C") return "bg-red-700 text-white";
    return "bg-emerald-950 text-emerald-200";
  }

  function resultClass(value?: number | null) {
    if (value === null || value === undefined) return "text-emerald-100/50";
    if (Number(value) > 0) return "text-emerald-300";
    if (Number(value) < 0) return "text-red-300";
    return "text-yellow-300";
  }

  function fmt(value: any, digits = 4) {
    if (value === null || value === undefined || value === "") return "-";

    const num = Number(value);
    if (!Number.isFinite(num)) return "-";

    return num.toFixed(digits);
  }

  function money(value: any, digits = 2) {
    return `${fmt(value, digits)} USDT`;
  }

  function pct(value: any, digits = 2) {
    return `${fmt(value, digits)}%`;
  }

  return (
    <main className="min-h-screen p-6">
      <div className="mx-auto max-w-7xl space-y-6">
        <Nav />
        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-emerald-300">
              Finmt - Dashboard
            </h1>
            <p className="text-sm text-emerald-100/70">
              Пульт управления торговым роботом и Telegram-сигналами
            </p>
          </div>
          <button
            onClick={checkExpirations}
            className="rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            Проверить подписки
          </button>
        </header>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Card title="Статус робота" value={bot?.status || "-"} />
          <Card title="Режим" value={bot?.mode || "-"} />
          <Card title="Всего сигналов" value={analytics?.total_signals ?? 0} />
          <Card title="Активные" value={analytics?.active_signals ?? 0} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Card title="Закрыто" value={analytics?.closed_signals ?? 0} />
          <Card title="Expired" value={analytics?.expired_signals ?? 0} />
          <Card title="Rejected" value={analytics?.rejected_signals ?? 0} />
          <Card title="Winrate" value={pct(analytics?.winrate)} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Card title="Победы" value={analytics?.wins ?? 0} />
          <Card title="Убытки" value={analytics?.losses ?? 0} />
          <Card title="Итог %" value={pct(analytics?.total_result_pct, 4)} />
          <Card title="Net PnL" value={money(analytics?.total_net_pnl_usdt)} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Card title="Avg PnL" value={money(analytics?.avg_net_pnl_usdt)} />
          <Card title="Costs" value={money(analytics?.total_costs_usdt)} />
          <Card title="Used Margin" value={money(analytics?.exposure?.used_margin)} />
          <Card title="Free Margin" value={money(analytics?.exposure?.free_margin)} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Card title="Max Margin" value={money(analytics?.exposure?.max_allowed_margin)} />
          <Card title="Active Exposure" value={analytics?.exposure?.active_signals_count ?? 0} />
          <Card title="Bot Signals" value={botState?.signals_count ?? 0} />
          <Card title="Open Positions" value={botState?.open_positions ?? 0} />
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 text-xl font-semibold text-emerald-200">
            Управление
          </h2>

          <div className="flex flex-wrap gap-3">
            <button
              onClick={startBot}
              className="flex items-center gap-2 rounded-xl bg-emerald-600 px-4 py-2 font-semibold text-black hover:bg-emerald-500"
            >
              <Play size={16} />
              Start
            </button>

            <button
              onClick={stopBot}
              className="flex items-center gap-2 rounded-xl bg-red-500 px-4 py-2 font-semibold text-black hover:bg-red-400"
            >
              <Square size={16} />
              Stop
            </button>
            <button
              onClick={loadAll}
              className="flex items-center gap-2 rounded-xl bg-emerald-900 px-4 py-2 text-sm hover:bg-emerald-800"
            >
              <RefreshCw size={16} />
              {loading ? "Обновление..." : "Обновить"}
            </button>
          </div>

          <div className="mt-5 border-t border-emerald-900 pt-4">
            <div className="mb-3 text-sm font-semibold text-yellow-300">
              Dev Tools
            </div>
              <div className="flex flex-wrap gap-3">
                <button
                  onClick={forceSignal}
                  className="flex items-center gap-2 rounded-xl bg-yellow-400 px-4 py-2 font-semibold text-black hover:bg-yellow-300"
                >
                  <Send size={16} />
                  Force Paper Signal
                </button>

                <button
                  onClick={forceLiveNearSignal}
                  className="flex items-center gap-2 rounded-xl bg-cyan-400 px-4 py-2 font-semibold text-black hover:bg-cyan-300"
                >
                  <Send size={16} />
                  Force Live Near Signal
                </button>

                <button
                  onClick={runLifecycleOnce}
                  className="flex items-center gap-2 rounded-xl bg-purple-400 px-4 py-2 font-semibold text-black hover:bg-purple-300"
                >
                  <RefreshCw size={16} />
                  Run Lifecycle Now
                </button>
                <button
                  onClick={forceValidTradeSignal}
                  className="flex items-center gap-2 rounded-xl bg-emerald-400 px-4 py-2 font-semibold text-black hover:bg-emerald-300"
                >
                  <Send size={16} />
                  Force Valid Trade Signal
                </button>
                <button
                  onClick={forceScalpSignal}
                  className="flex items-center gap-2 rounded-xl bg-orange-400 px-4 py-2 font-semibold text-black hover:bg-orange-300"
                >
                  <Send size={16} />
                  Scalp Lifecycle Test
                </button>

                <button
                  onClick={sendAllReports}
                  className="flex items-center gap-2 rounded-xl bg-indigo-400 px-4 py-2 font-semibold text-black hover:bg-indigo-300"
                >
                  <Send size={16} />
                  Send All Reports
                </button>

                <button
                  onClick={sendVipReport}
                  className="flex items-center gap-2 rounded-xl bg-emerald-400 px-4 py-2 font-semibold text-black hover:bg-emerald-300"
                >
                  <Send size={16} />
                  VIP Report
                </button>

                <button
                  onClick={sendFreeReport}
                  className="flex items-center gap-2 rounded-xl bg-sky-400 px-4 py-2 font-semibold text-black hover:bg-sky-300"
                >
                  <Send size={16} />
                  FREE Report
                </button>
              </div>
          </div>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-xl font-semibold text-emerald-200">
              Последние сигналы
            </h2>
            <span className="text-sm text-emerald-100/50">
              автообновление каждые 5 секунд
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] border-collapse text-xs">
              <thead>
                <tr className="border-b border-emerald-900 text-left text-emerald-300">
                  <th className="p-2">ID</th>
                  <th className="p-2">Symbol</th>
                  <th className="p-2">Side</th>
                  <th className="p-2">Status</th>
                  <th className="p-2">Entry</th>
                  <th className="p-2">Grade</th>
                  <th className="p-2">PnL</th>
                  <th className="p-2">Actions</th>
                </tr>
              </thead>

              <tbody>
                {safeSignals.map((s) => {
                  const isFinished = ["closed", "expired", "stopped", "rejected"].includes(s.status);

                  return (
                    <tr key={s.id} className="border-b border-emerald-950/80">
                      <td className="p-2 text-emerald-100/80">{s.id}</td>

                      <td className="p-2 font-semibold text-emerald-100">
                        {s.symbol}
                      </td>

                      <td className="p-2">
                        <span className={s.side === "long" ? "text-emerald-300" : "text-red-300"}>
                          {s.side}
                        </span>
                      </td>

                      <td className="p-2">
                        <span className="rounded-lg bg-emerald-950 px-2 py-1 text-emerald-200">
                          {s.status}
                        </span>
                      </td>

                      <td className="p-2 text-emerald-100/70">
                        {fmt(s.entry_zone?.from, 4)} - {fmt(s.entry_zone?.to, 4)}
                      </td>

                      <td className="p-2">
                        <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${gradeClass(s.grade)}`}>
                          {s.grade || "-"}
                        </span>
                      </td>

                      <td className="p-2">
                        {s.status === "closed" ? (
                          <div className="space-y-1">
                            <div className={Number(s.result_pct) >= 0 ? "text-emerald-300 font-bold" : "text-red-300 font-bold"}>
                              {fmt(s.result_pct, 4)}%
                            </div>
                            <div className={resultClass(s.closed_net_pnl)}>
                              {fmt(s.closed_net_pnl, 4)} USDT
                            </div>
                          </div>
                        ) : (
                          <span className="text-emerald-100/40">-</span>
                        )}
                      </td>

                      <td className="p-2">
                        {!isFinished ? (
                          <div className="flex flex-wrap gap-1">
                            {s.status === "opened" && (
                              <>
                                <button
                                  onClick={() => testLifecyclePrice(s.id, s.tp?.tp1)}
                                  className="rounded-lg bg-blue-700 px-2 py-1 text-[10px] hover:bg-blue-600"
                                >
                                  TP1
                                </button>

                                <button
                                  onClick={() => testLifecyclePrice(s.id, s.stop_price)}
                                  className="rounded-lg bg-red-700 px-2 py-1 text-[10px] hover:bg-red-600"
                                >
                                  Stop
                                </button>
                              </>
                            )}

                            {(s.status === "tp1" || s.status === "breakeven") && (
                              <>
                                <button
                                  onClick={() => testLifecyclePrice(s.id, s.tp?.tp2)}
                                  className="rounded-lg bg-emerald-700 px-2 py-1 text-[10px] hover:bg-emerald-600"
                                >
                                  TP2
                                </button>

                                <button
                                  onClick={() => testLifecyclePrice(s.id, s.entry_zone?.from)}
                                  className="rounded-lg bg-yellow-700 px-2 py-1 text-[10px] hover:bg-yellow-600"
                                >
                                  BE
                                </button>
                              </>
                            )}

                            <button
                              onClick={() => closeSignal(s.id, 2.1)}
                              className="rounded-lg bg-emerald-700 px-2 py-1 text-[10px] hover:bg-emerald-600"
                            >
                              +2.1%
                            </button>

                            <button
                              onClick={() => closeSignal(s.id, -1.0)}
                              className="rounded-lg bg-red-700 px-2 py-1 text-[10px] hover:bg-red-600"
                            >
                              -1.0%
                            </button>
                          </div>
                        ) : (
                          <span className="text-emerald-100/50">{s.status}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}

                {safeSignals.length === 0 && (
                  <tr>
                    <td className="p-6 text-center text-emerald-100/50" colSpan={8}>
                      Сигналов пока нет
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </main>
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