"use client";

import { useEffect, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { RefreshCw, BookOpen, Database, BarChart3 } from "lucide-react";

export default function OrderbookPage() {
  const [ob, setOb] = useState<any>(null);
  const [ml, setMl] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);
  const [vpSym, setVpSym] = useState("BTC/USDT");
  const [vpTf, setVpTf] = useState("1h");
  const [vp, setVp] = useState<any>(null);
  const [vpLoading, setVpLoading] = useState(false);

  async function loadVP() {
    setVpLoading(true);
    try {
      setVp(await apiGet(`/orderbook/volume-profile?symbol=${encodeURIComponent(vpSym)}&timeframe=${vpTf}&limit=1000&bins=50`));
    } finally {
      setVpLoading(false);
    }
  }

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

  useEffect(() => {
    loadVP();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vpSym, vpTf]);

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

      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-lg font-bold text-emerald-300">
              <BarChart3 size={18} /> Volume Profile
            </h2>
            <p className="mt-1 text-xs text-emerald-100/50">
              Узлы объёма по цене из OHLCV. Для выбора уровней (TP/стоп у HVN, не в LVN), не для прогноза направления.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <select value={vpSym} onChange={(e) => setVpSym(e.target.value)} className="rounded-lg border border-emerald-800 bg-emerald-950 px-3 py-1.5 text-sm text-emerald-100">
              {(Object.keys(symbols).length ? Object.keys(symbols) : ["BTC/USDT"]).sort().map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <select value={vpTf} onChange={(e) => setVpTf(e.target.value)} className="rounded-lg border border-emerald-800 bg-emerald-950 px-3 py-1.5 text-sm text-emerald-100">
              {["15m", "30m", "1h", "4h"].map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <button onClick={loadVP} className="flex items-center gap-1 rounded-lg bg-emerald-800 px-3 py-1.5 text-sm hover:bg-emerald-700">
              <RefreshCw size={14} />{vpLoading ? "..." : "Обновить"}
            </button>
          </div>
        </div>

        {vp?.status === "ok" ? (
          <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_300px]">
            <VPHistogram vp={vp} />
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <Card title="Цена" value={fmt(vp.price, 4)} sub={vp.in_value_area ? "в value area" : "вне VA"} tone={vp.in_value_area ? "good" : "warn"} />
                <Card title="VPOC" value={fmt(vp.vpoc, 4)} sub="справедливая цена" tone="good" />
                <Card title="VAH" value={fmt(vp.vah, 4)} sub="верх value area" />
                <Card title="VAL" value={fmt(vp.val, 4)} sub="низ value area" />
              </div>
              <div className="rounded-xl border border-emerald-900 bg-black/30 p-3">
                <div className="mb-2 text-xs font-semibold text-emerald-300">Ближайшие узлы относительно цены</div>
                <Lvl k="HVN выше — зона реакции" v={vp.nearest_hvn_above} tone="warn" />
                <Lvl k="HVN ниже — зона реакции" v={vp.nearest_hvn_below} tone="warn" />
                <Lvl k="LVN выше — проходит быстро" v={vp.nearest_lvn_above} tone="dim" />
                <Lvl k="LVN ниже — проходит быстро" v={vp.nearest_lvn_below} tone="dim" />
              </div>
              <p className="text-[11px] leading-relaxed text-emerald-100/40">
                {vp.bars} баров · {vp.timeframe}. HVN = узлы реакции (тут логично ставить TP/стоп), LVN = «пустоты» (цена проскакивает), VPOC = цена с макс. объёмом.
              </p>
            </div>
          </div>
        ) : (
          <div className="mt-4 text-sm text-emerald-100/50">
            {vpLoading ? "Загрузка профиля…" : vp?.status ? `Нет профиля: ${vp.status}${vp.error ? ` (${vp.error})` : ""}` : "Выбери символ и таймфрейм."}
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

function Lvl({ k, v, tone = "warn" }: { k: string; v: any; tone?: "warn" | "dim" }) {
  const cls = tone === "dim" ? "text-emerald-100/60" : "text-yellow-200";
  return (
    <div className="flex items-center justify-between py-0.5 text-xs">
      <span className="text-emerald-100/50">{k}</span>
      <span className={"font-mono " + cls}>{v != null ? Number(v).toFixed(6) : "—"}</span>
    </div>
  );
}

function VPHistogram({ vp }: { vp: any }) {
  const bins: any[] = vp.profile || [];
  if (!bins.length) return null;
  const maxv = Math.max(...bins.map((b) => b.vol_pct), 0.001);
  const price = vp.price != null ? Number(vp.price) : null;
  // Бин, БЛИЖАЙШИЙ к текущей цене (argmin). Раньше брали допуск из ширины VA —
  // он уже ширины бина, поэтому маркер цены не попадал ни в один бин и не рисовался.
  let priceIdx = -1;
  if (price != null && Number.isFinite(price)) {
    let best = Infinity;
    bins.forEach((b, i) => {
      const d = Math.abs(Number(b.price) - price);
      if (d < best) { best = d; priceIdx = i; }
    });
  }
  const lo = Number(bins[0].price), hi = Number(bins[bins.length - 1].price);
  const priceBelow = price != null && price < lo;
  const priceAbove = price != null && price > hi;
  const pf = (v: number) => fmt(v, v >= 100 ? 2 : 5);
  // сверху вниз = от высокой цены к низкой (как в стакане)
  const rows = bins.map((b, i) => ({ b, i })).reverse();
  return (
    <div className="rounded-xl border border-emerald-900 bg-black/20 p-3">
      {priceAbove && price != null && (
        <div className="mb-1 text-center text-[10px] font-bold text-cyan-300">◄ цена {pf(price)} ВЫШE профиля</div>
      )}
      <div className="flex flex-col gap-[1px]">
        {rows.map(({ b, i }) => {
          const isVpoc = Math.abs(b.price - vp.vpoc) < 1e-9;
          const inVA = b.price >= vp.val && b.price <= vp.vah;
          const nearPrice = i === priceIdx && !priceBelow && !priceAbove;
          const w = Math.max(1.5, (b.vol_pct / maxv) * 100);
          return (
            <div key={i} className="flex items-center gap-2">
              <span className={"w-[78px] shrink-0 text-right font-mono text-[10px] " + (nearPrice ? "font-bold text-cyan-300" : "text-emerald-100/40")}>
                {fmt(b.price, b.price >= 100 ? 2 : 5)}{nearPrice ? " ◄" : ""}
              </span>
              <div className="h-[6px] flex-1">
                <div
                  className={"h-[6px] rounded-sm " + (isVpoc ? "bg-yellow-400" : inVA ? "bg-emerald-500" : "bg-emerald-800/70")}
                  style={{ width: `${w}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
      {priceBelow && price != null && (
        <div className="mt-1 text-center text-[10px] font-bold text-cyan-300">◄ цена {pf(price)} НИЖE профиля</div>
      )}
      <div className="mt-3 flex flex-wrap gap-3 text-[10px] text-emerald-100/45">
        <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm bg-yellow-400" /> VPOC</span>
        <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm bg-emerald-500" /> value area (~70%)</span>
        <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm bg-emerald-800/70" /> вне VA</span>
        <span className="flex items-center gap-1 text-cyan-300">◄ текущая цена</span>
      </div>
    </div>
  );
}

function fmt(v: any, d = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "-";
}
