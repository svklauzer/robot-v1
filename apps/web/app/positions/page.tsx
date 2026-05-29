"use client";

import { useEffect, useMemo, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { RefreshCw, WalletCards } from "lucide-react";

const ACTIVE_STATUSES = new Set(["open", "active", "published"]);

export default function PositionsPage() {
  const [positions, setPositions] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("active");
  const [symbolFilter, setSymbolFilter] = useState("");

  async function loadPositions() {
    setLoading(true);
    try {
      const data = await apiGet("/positions");
      setPositions(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadPositions();
  }, []);

  const summary = useMemo(() => {
    const active = positions.filter((p) => ACTIVE_STATUSES.has(String(p.status || "").toLowerCase()));
    const closed = positions.filter((p) => String(p.status || "").toLowerCase() === "closed");
    const netPnl = positions.reduce((sum, p) => sum + numeric(p.unrealized_pnl), 0);

    return {
      total: positions.length,
      active: active.length,
      closed: closed.length,
      netPnl,
      lastSignalId: positions[0]?.signal_id || "-",
    };
  }, [positions]);

  const filteredPositions = useMemo(() => {
    const query = symbolFilter.trim().toLowerCase();

    return positions.filter((p) => {
      const status = String(p.status || "").toLowerCase();
      const symbol = String(p.symbol || "").toLowerCase();
      const matchesSymbol = !query || symbol.includes(query) || String(p.signal_id || "").includes(query);
      const matchesStatus =
        statusFilter === "all" ||
        (statusFilter === "active" && ACTIVE_STATUSES.has(status)) ||
        status === statusFilter;

      return matchesSymbol && matchesStatus;
    });
  }, [positions, statusFilter, symbolFilter]);

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
            <WalletCards />
            Positions
          </h1>
          <p className="mt-2 text-emerald-100/60">
            Активные позиции по умолчанию; закрытая история доступна через фильтр, чтобы не маскировать отсутствие новых open-сделок.
          </p>
        </div>

        <button
          onClick={loadPositions}
          className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
        >
          <RefreshCw size={16} />
          {loading ? "Обновление..." : "Обновить"}
        </button>
      </header>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-5">
        <SummaryCard title="Всего" value={summary.total} />
        <SummaryCard title="Active/Open" value={summary.active} tone={summary.active > 0 ? "good" : "warn"} />
        <SummaryCard title="Closed" value={summary.closed} />
        <SummaryCard title="Net PnL" value={`${summary.netPnl.toFixed(4)} USDT`} tone={summary.netPnl < 0 ? "bad" : "good"} />
        <SummaryCard title="Latest signal" value={summary.lastSignalId === "-" ? "-" : `#${summary.lastSignalId}`} />
      </section>

      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-[220px_1fr_auto] md:items-end">
          <label className="text-sm text-emerald-100/70">
            Status
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="mt-2 w-full rounded-xl border border-emerald-800 bg-black/40 px-3 py-2 text-emerald-100 outline-none focus:border-emerald-400"
            >
              <option value="active">active / open</option>
              <option value="all">all</option>
              <option value="open">open</option>
              <option value="closed">closed</option>
              <option value="expired">expired</option>
            </select>
          </label>

          <label className="text-sm text-emerald-100/70">
            Symbol / signal
            <input
              value={symbolFilter}
              onChange={(e) => setSymbolFilter(e.target.value)}
              placeholder="BTC/USDT или #40"
              className="mt-2 w-full rounded-xl border border-emerald-800 bg-black/40 px-3 py-2 text-emerald-100 outline-none placeholder:text-emerald-100/35 focus:border-emerald-400"
            />
          </label>

          <div className="rounded-xl border border-emerald-950 bg-black/20 px-4 py-2 text-sm text-emerald-100/60">
            показано: <span className="font-bold text-emerald-200">{filteredPositions.length}</span> / {positions.length}
          </div>
        </div>
      </section>

      {statusFilter === "active" && summary.active === 0 && (
        <section className="rounded-2xl border border-amber-800/70 bg-amber-950/20 p-5 text-amber-100">
          <h2 className="text-lg font-semibold">Активных/open позиций сейчас нет</h2>
          <p className="mt-2 text-sm text-amber-100/70">
            Это соответствует последнему отчету: робот запущен в paper, но за окно наблюдения новые сделки не дошли до open. Для аудита истории переключите фильтр на closed/all.
          </p>
        </section>
      )}

      <section className="overflow-hidden rounded-2xl border border-emerald-900 bg-black/30">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-left text-sm">
            <thead className="border-b border-emerald-900 bg-emerald-950/40 text-emerald-200">
              <tr>
                <th className="px-4 py-3">ID</th>
                <th className="px-4 py-3">Symbol</th>
                <th className="px-4 py-3">Side</th>
                <th className="px-4 py-3">Qty</th>
                <th className="px-4 py-3">Entry</th>
                <th className="px-4 py-3">Mark</th>
                <th className="px-4 py-3">PnL</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Signal</th>
              </tr>
            </thead>
            <tbody>
              {filteredPositions.map((p) => (
                <tr key={p.id} className="border-b border-emerald-950 last:border-b-0 hover:bg-emerald-950/20">
                  <td className="px-4 py-3 text-emerald-100/60">#{p.id}</td>
                  <td className="px-4 py-3 font-semibold text-emerald-200">{p.symbol}</td>
                  <td className={p.side === "short" ? "px-4 py-3 text-red-300" : "px-4 py-3 text-emerald-300"}>{p.side}</td>
                  <td className="px-4 py-3">{p.qty ?? "-"}</td>
                  <td className="px-4 py-3">{p.entry_price ?? "-"}</td>
                  <td className="px-4 py-3">{p.mark_price ?? "-"}</td>
                  <td className={(p.unrealized_pnl ?? 0) < 0 ? "px-4 py-3 text-red-300" : "px-4 py-3 text-emerald-300"}>{p.unrealized_pnl ?? 0}</td>
                  <td className="px-4 py-3">{p.status}</td>
                  <td className="px-4 py-3 text-emerald-100/60">{p.signal_id ? `#${p.signal_id}` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {filteredPositions.length === 0 && (
          <div className="p-8 text-center text-emerald-100/50">
            Позиций по выбранным фильтрам нет.
          </div>
        )}
      </section>
    </AppShell>
  );
}

function SummaryCard({ title, value, tone = "default" }: { title: string; value: any; tone?: "default" | "good" | "warn" | "bad" }) {
  const valueClass = tone === "bad" ? "text-red-300" : tone === "warn" ? "text-yellow-300" : "text-emerald-200";

  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${valueClass}`}>{value}</div>
    </div>
  );
}

function numeric(value: any) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}
