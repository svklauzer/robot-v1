"use client";

import { useEffect, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { RefreshCw, BookOpen, Database } from "lucide-react";

export default function OrderbookPage() {
  const [ob, setOb] = useState<any>(null);
  const [ml, setMl] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const [obData, mlData] = await Promise.all([
        apiGet("/orderbook/state"),
        apiGet("/ml/outcomes/stats"),
      ]);
      setOb(obData);
      setMl(mlData);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!auto) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [auto]);

  const th = ob?.thresholds || {};
  const symbols = ob?.symbols || {};
  const freshAge = ob?.stats?.freshest_age_sec;
  const feedAlive = ob?.enabled && freshAge != null && freshAge < (th.data_max_age_sec ?? 15);
  const mlCount = ml?.count ?? 0;
  const mlTarget = ml?.target_for_training ?? 200;

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
            <BookOpen />
            Order Book &amp; ML
          </h1>
          <p className="mt-2 text-emerald-100/60">
            Живой стакан HTX (WS): спред, OBI, стенки, CVD — основа depth-гейта входов и CVD-выхода. Плюс рост ML-датасета.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-emerald-100/70">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            авто 5с
          </label>
          <button onClick={load} className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700">
            <RefreshCw size={16} />
            {loading ? "..." : "Обновить"}
          </button>
        </div>
      </header>

      <section className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card title="Depth feed" value={ob?.enabled ? (feedAlive ? "LIVE" : "STALE") : "OFF"} sub={freshAge != null ? `age ${Number(freshAge).toFixed(2)}s` : "no data"} tone={ob?.enabled ? (feedAlive ? "good" : "bad") : "warn"} />
        <Card title="Books" value={ob?.stats?.books ?? 0} sub={`gate ${ob?.gate_entries ? "on" : "off"} · exit ${ob?.accelerate_exits ? "on" : "off"}`} />
        <Card title="ML dataset" value={`${mlCount} / ${mlTarget}`} sub={`win ${ml?.winrate_pct ?? 0}%`} tone={mlCount >= mlTarget ? "good" : "warn"} icon={<Database size={16} />} />
        <Card title="ML depth-фичи" value={ml?.with_entry_depth ?? 0} sub={`regime ${ml?.with_regime ?? 0} · last ${ml?.last_reason || "-"}`} tone={(ml?.with_entry_depth ?? 0) > 0 ? "good" : "warn"} />
      </section>

      <section className="flex flex-wrap gap-2 text-xs text-emerald-100/70">
        <Chip>spread ≤ {th.max_spread_pct}%</Chip>
        <Chip>OBI ≥ {th.obi_confirm}</Chip>
        <Chip>wall ≥ {th.wall_confirm_share}</Chip>
        <Chip>CVD exit ≥ {th.cvd_exit_ratio}</Chip>
        <Chip>data max age {th.data_max_age_sec}s</Chip>
      </section>

      <section className="overflow-hidden rounded-2xl border border-emerald-900 bg-black/30">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-left text-sm">
            <thead className="border-b border-emerald-900 bg-emerald-950/40 text-emerald-200">
              <tr>
                <th className="px-4 py-3">Symbol</th>
                <th className="px-4 py-3">Spread %</th>
                <th className="px-4 py-3">OBI</th>
                <th className="px-4 py-3">Bid wall</th>
                <th className="px-4 py-3">Ask wall</th>
                <th className="px-4 py-3">CVD ratio</th>
                <th className="px-4 py-3">Trades</th>
                <th className="px-4 py-3">Age</th>
                <th className="px-4 py-3">Short gate</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(symbols).sort().map((sym) => {
                const d = symbols[sym];
                const wideSpread = d.spread_pct != null && d.spread_pct > (th.max_spread_pct ?? 0.08);
                // короткий вход: нужен ask-перекос (obi <= -confirm) или ask-стенка
                const askPressure = d.obi <= -(th.obi_confirm ?? 0.15) || d.ask_wall_share >= (th.wall_confirm_share ?? 0.3);
                const shortOk = !wideSpread && askPressure && !d.stale;
                return (
                  <tr key={sym} className="border-b border-emerald-950 last:border-b-0 hover:bg-emerald-950/20">
                    <td className="px-4 py-3 font-semibold text-emerald-200">{sym}</td>
                    <td className={"px-4 py-3 " + (wideSpread ? "text-red-300" : "text-emerald-100/80")}>{fmt(d.spread_pct, 4)}</td>
                    <td className={"px-4 py-3 " + (d.obi > 0 ? "text-emerald-300" : "text-red-300")}>{fmt(d.obi, 3)}</td>
                    <td className="px-4 py-3 text-emerald-100/70">{fmt(d.bid_wall_share, 2)}</td>
                    <td className="px-4 py-3 text-emerald-100/70">{fmt(d.ask_wall_share, 2)}</td>
                    <td className={"px-4 py-3 " + (Math.abs(d.cvd_ratio) >= (th.cvd_exit_ratio ?? 0.6) ? "text-yellow-300" : "text-emerald-100/70")}>{fmt(d.cvd_ratio, 2)}</td>
                    <td className={"px-4 py-3 " + ((d.cvd_trades ?? 0) < 15 ? "text-emerald-100/35" : "text-emerald-100/80")}>{d.cvd_trades ?? 0}</td>
                    <td className={"px-4 py-3 " + (d.stale ? "text-red-300" : "text-emerald-100/50")}>{d.age_sec != null ? `${d.age_sec}s` : "-"}</td>
                    <td className="px-4 py-3">
                      <span className={"rounded-full px-2 py-1 text-xs " + (shortOk ? "bg-emerald-900/60 text-emerald-200" : "bg-black/40 text-emerald-100/40")}>
                        {wideSpread ? "spread" : shortOk ? "pass" : "no ask"}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {Object.keys(symbols).length === 0 && (
          <div className="p-8 text-center text-emerald-100/50">
            {ob?.enabled ? "Стакан пуст — фид ещё поднимается или ENABLE_ORDERBOOK_ENGINE=false." : "Depth-движок выключен (ENABLE_ORDERBOOK_ENGINE)."}
          </div>
        )}
      </section>

      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5 text-sm text-emerald-100/70">
        ML-датасет: <span className="font-bold text-emerald-200">{mlCount}</span> закрытых из {mlTarget} для обучения · win {ml?.wins ?? 0} / loss {ml?.losses ?? 0} · с depth-фичами: <span className="font-bold text-emerald-200">{ml?.with_entry_depth ?? 0}</span> · последняя запись: {ml?.last_logged_at || "-"} ({ml?.last_symbol || "-"})
      </section>
    </AppShell>
  );
}

function Card({ title, value, sub, tone = "default", icon }: { title: string; value: any; sub?: string; tone?: "default" | "good" | "warn" | "bad"; icon?: any }) {
  const cls = tone === "bad" ? "text-red-300" : tone === "warn" ? "text-yellow-300" : tone === "good" ? "text-emerald-300" : "text-emerald-200";
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="flex items-center gap-2 text-sm text-emerald-100/60">{icon}{title}</div>
      <div className={`mt-2 text-2xl font-bold ${cls}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-emerald-100/50">{sub}</div>}
    </div>
  );
}

function Chip({ children }: { children: any }) {
  return <span className="rounded-full border border-emerald-800 bg-black/20 px-3 py-1">{children}</span>;
}

function fmt(v: any, d = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "-";
}
