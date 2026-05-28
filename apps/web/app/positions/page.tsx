"use client";

import { useEffect, useState } from "react";
import Nav from "../../components/Nav";
import { apiGet } from "../../lib/api";
import { RefreshCw, WalletCards } from "lucide-react";

export default function PositionsPage() {
  const [positions, setPositions] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

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

  return (
    <main className="min-h-screen bg-[#020617] text-emerald-50">
      <Nav />

      <div className="mx-auto max-w-7xl space-y-6 px-6 py-8">
        <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
              <WalletCards />
              Positions
            </h1>
            <p className="mt-2 text-emerald-100/60">
              Текущие и последние позиции для контроля live/paper lifecycle.
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

        <section className="overflow-hidden rounded-2xl border border-emerald-900 bg-black/30">
          <table className="w-full min-w-[760px] text-left text-sm">
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
              {positions.map((p) => (
                <tr key={p.id} className="border-b border-emerald-950 last:border-b-0">
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

          {positions.length === 0 && (
            <div className="p-8 text-center text-emerald-100/50">
              Позиций пока нет.
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
