"use client";

import { useEffect, useRef, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import { Grid3x3, RefreshCw, Power, PlayCircle, XCircle } from "lucide-react";

const REGIME_COLOR: Record<string, string> = {
  long: "bg-emerald-600 text-white",
  short: "bg-red-600 text-white",
  neutral: "bg-slate-600 text-white",
};

export default function GridPage() {
  const [state, setState] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [auto, setAuto] = useState(true);
  const loadingRef = useRef(false);

  async function load() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      setState(await apiGet("/grid/state"));
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  async function toggle(enabled: boolean) {
    const msg = enabled
      ? "Включить умную сетку? Она пойдёт ПАРАЛЛЕЛЬНО тренду на свой карман маржи. Открытые тренд-ордера не трогаются."
      : "Выключить сетку? Новые уровни не открываются; активные циклы можно закрыть вручную.";
    if (!window.confirm(msg)) return;
    setBusy(true);
    try {
      await apiPost(enabled ? "/grid/enable" : "/grid/disable");
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function runOnce() {
    setBusy(true);
    try {
      await apiPost("/grid/run-once");
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function closeCycle(symbol: string) {
    if (!window.confirm(`Закрыть цикл ${symbol} по рынку (вся корзина)?`)) return;
    setBusy(true);
    try {
      await apiPost(`/grid/close/${encodeURIComponent(symbol)}`);
      await load();
    } finally {
      setBusy(false);
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

  const enabled = !!state?.enabled;
  const cfg = state?.config || {};
  const cycles = state?.cycles || [];
  const history = state?.history || [];

  return (
    <AppShell>
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
            <Grid3x3 /> Smart Grid
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-emerald-100/60">
            Адаптивная сетка (ATR-шаг, EMA200/RSI-регайм) на swap. Параллельно тренду, свой карман маржи, тренд-ордера не трогает. Сейчас paper.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <label className="flex items-center gap-2 text-sm text-emerald-100/70">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> авто 5с
          </label>
          <button onClick={load} className="flex items-center gap-2 rounded-xl bg-emerald-900 px-4 py-2 text-sm font-semibold hover:bg-emerald-800">
            <RefreshCw size={16} />{loading ? "..." : "Обновить"}
          </button>
          <button onClick={runOnce} disabled={busy || !enabled} className="flex items-center gap-2 rounded-xl bg-cyan-800 px-4 py-2 text-sm font-semibold hover:bg-cyan-700 disabled:opacity-40">
            <PlayCircle size={16} /> Тик
          </button>
          <button
            onClick={() => toggle(!enabled)}
            disabled={busy}
            className={"flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-bold disabled:opacity-50 " + (enabled ? "bg-red-500 text-slate-950 hover:bg-red-400" : "bg-emerald-500 text-slate-950 hover:bg-emerald-400")}
          >
            <Power size={16} /> {enabled ? "Выключить сетку" : "Включить сетку"}
          </button>
        </div>
      </header>

      <section className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Card title="Статус" value={enabled ? "ВКЛ" : "ВЫКЛ"} tone={enabled ? "good" : "warn"} />
        <Card title="Активных циклов" value={state?.active_cycles ?? 0} />
        <Card title="Карман маржи" value={`${fmt(state?.margin_envelope_usdt)} USDT`} sub={`занято ${fmt(state?.grid_used_margin_usdt)}`} />
        <Card title="Свободно в кармане" value={`${fmt(state?.grid_free_margin_usdt)} USDT`} />
        <Card title="Реализ. PnL" value={`${fmt(state?.realized_pnl_usdt)} USDT`} tone={(state?.realized_pnl_usdt ?? 0) < 0 ? "bad" : "good"} />
        <Card title="Закрыто циклов" value={state?.closed_cycles ?? 0} />
      </section>

      {/* Конфиг */}
      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-emerald-100/50">Параметры (env)</h2>
        <div className="flex flex-wrap gap-2 text-xs text-emerald-100/70">
          <Chip>пары: {(cfg.symbols || []).join(", ") || "—"}</Chip>
          <Chip>ТФ {cfg.timeframe}</Chip>
          <Chip>линий {cfg.lines}</Chip>
          <Chip>база {cfg.base_order_usdt} USDT</Chip>
          <Chip>m_vol {cfg.vol_multiplier}</Chip>
          <Chip>m_step {cfg.step_multiplier}</Chip>
          <Chip>k_vol {cfg.vol_coeff_k}</Chip>
          <Chip>TP +{cfg.tp_pct}%</Chip>
          <Chip>SL {cfg.sl_atr_mult}·ATR</Chip>
          <Chip>max орд. {cfg.max_safety_orders}</Chip>
          <Chip>карман {cfg.max_used_margin_pct}%</Chip>
          <Chip>рынок {cfg.market}</Chip>
        </div>
      </section>

      {/* Активные циклы */}
      <section className="space-y-4">
        <h2 className="text-xl font-semibold text-emerald-200">Активные циклы</h2>
        {cycles.length === 0 && (
          <div className="rounded-2xl border border-emerald-950 bg-black/20 p-6 text-center text-emerald-100/50">
            {enabled ? "Циклов нет — сетка ждёт условий регайма / движения цены к уровням." : "Сетка выключена."}
          </div>
        )}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {cycles.map((c: any) => (
            <div key={c.symbol} className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <h3 className="text-lg font-bold text-emerald-200">{c.symbol}</h3>
                  <span className={`rounded-lg px-2 py-1 text-xs font-bold ${REGIME_COLOR[c.regime] || "bg-slate-600 text-white"}`}>{c.regime}</span>
                  {c.regime_now && c.regime_now !== c.regime && (
                    <span className={`rounded-lg px-2 py-1 text-xs font-bold ${REGIME_COLOR[c.regime_now] || "bg-slate-600 text-white"}`} title="живой регайм отличается от регайма цикла">→ {c.regime_now}</span>
                  )}
                  {c.frozen && (
                    <span className="rounded-lg bg-sky-900/70 px-2 py-1 text-xs font-bold text-sky-200" title="боковик: добор уровней заморожен, выходы работают">❄ заморожен</span>
                  )}
                  {(c.flip_streak ?? 0) > 0 && (
                    <span className="rounded-lg bg-amber-900/70 px-2 py-1 text-xs font-bold text-amber-200" title="тиков подряд против цикла; при достижении порога — разворот">↻ {c.flip_streak}</span>
                  )}
                </div>
                <button onClick={() => closeCycle(c.symbol)} className="flex items-center gap-1 rounded-lg bg-red-800/70 px-3 py-1 text-xs hover:bg-red-700">
                  <XCircle size={14} /> закрыть
                </button>
              </div>

              {/* Живой регайм — ВСЕГДА виден: спокойный long не путать с «нет данных» */}
              <LiveRegime c={c} confirm={cfg.flip_confirm_ticks ?? 3} band={cfg.regime_band_pct} />

              <div className="mt-3 grid grid-cols-2 gap-2 text-sm md:grid-cols-3">
                <Metric k="Якорь" v={fmt(c.anchor, 4)} />
                <Metric k="ATR" v={fmt(c.atr, 4)} />
                <Metric k="EMA200/RSI" v={c.ind ? `${fmt(c.ind.ema, 2)} / ${fmt(c.ind.rsi, 1)}` : "—"} />
                <Metric k="Уровни" v={`${c.levels_filled}/${c.levels_total}`} />
                <Metric k="Сред. цена" v={c.position ? fmt(c.position.avg_price, 4) : "—"} />
                <Metric k="Нетто кол-во" v={c.position ? fmt(c.position.net_qty, 4) : "—"} />
                <Metric k="Безубыток" v={fmt(c.breakeven, 4)} />
                <Metric k="TP" v={fmt(c.tp_price, 4)} tone="good" />
                <Metric k="SL" v={fmt(c.sl_price, 4)} tone="bad" />
                <Metric k="Цена" v={fmt(c.last_price, 4)} />
                <Metric k="Unreal PnL" v={`${fmt(c.unrealized_pnl)} USDT`} tone={(c.unrealized_pnl ?? 0) < 0 ? "bad" : "good"} />
              </div>

              {/* Лестница уровней */}
              <div className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[360px] text-left text-xs">
                  <thead className="text-emerald-100/40">
                    <tr><th className="py-1 pr-2">#</th><th className="pr-2">side</th><th className="pr-2">price</th><th className="pr-2">vol</th><th className="pr-2">dist%</th><th>fill</th></tr>
                  </thead>
                  <tbody>
                    {(c.levels || []).map((lv: any, i: number) => (
                      <tr key={i} className={"border-t border-emerald-950/60 " + (lv.filled ? "text-emerald-100" : "text-emerald-100/40")}>
                        <td className="py-0.5 pr-2">{lv.n}</td>
                        <td className={"pr-2 " + (lv.side === "buy" ? "text-emerald-300" : "text-red-300")}>{lv.side}</td>
                        <td className="pr-2 font-mono">{fmt(lv.price, 4)}</td>
                        <td className="pr-2 font-mono">{fmt(lv.volume, 4)}</td>
                        <td className="pr-2">{lv.distance_pct}%</td>
                        <td>{lv.filled ? "✓" : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* История */}
      {history.length > 0 && (
        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-3 text-xl font-semibold text-emerald-200">История циклов (последние)</h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-left text-sm">
              <thead className="text-emerald-100/50">
                <tr><th className="py-1 pr-3">Symbol</th><th className="pr-3">Regime</th><th className="pr-3">Причина</th><th className="pr-3">Цена закр.</th><th>Realized PnL</th></tr>
              </thead>
              <tbody>
                {[...history].reverse().map((h: any, i: number) => (
                  <tr key={i} className="border-t border-emerald-950">
                    <td className="py-1 pr-3 font-semibold text-emerald-200">{h.symbol}</td>
                    <td className="pr-3">{h.regime}</td>
                    <td className="pr-3 text-emerald-100/70">{h.close_reason}</td>
                    <td className="pr-3 font-mono">{fmt(h.close_price, 4)}</td>
                    <td className={(h.realized_pnl ?? 0) < 0 ? "text-red-300" : "text-emerald-300"}>{fmt(h.realized_pnl)} USDT</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </AppShell>
  );
}

function LiveRegime({ c, confirm, band }: { c: any; confirm: number; band?: number }) {
  const cycle = c.regime;
  const now = c.regime_now;
  let txt: string, cls: string;
  if (!now) {
    txt = "нет живых данных регайма"; cls = "text-slate-400";
  } else if (c.frozen || now === "neutral") {
    txt = `боковик ±${band ?? "?"}% у EMA — добор заморожен, выходы работают`; cls = "text-sky-300";
  } else if (now !== cycle) {
    txt = `разворот зреет: ${c.flip_streak ?? 0}/${confirm} тиков → ${now}`; cls = "text-amber-300";
  } else {
    txt = "рынок подтверждает направление сетки"; cls = "text-emerald-300";
  }
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-emerald-950 bg-black/20 px-3 py-1.5 text-xs">
      <span className="text-emerald-100/50">цикл встал:</span>
      <span className={`rounded px-1.5 py-0.5 font-bold ${REGIME_COLOR[cycle] || "bg-slate-600 text-white"}`}>{cycle}</span>
      <span className="text-emerald-100/50">рынок сейчас:</span>
      <span className={`rounded px-1.5 py-0.5 font-bold ${REGIME_COLOR[now] || "bg-slate-700 text-white"}`}>{now ?? "—"}</span>
      <span className={`ml-auto font-semibold ${cls}`}>{txt}</span>
    </div>
  );
}

function Card({ title, value, sub, tone = "default" }: { title: string; value: any; sub?: string; tone?: "default" | "good" | "warn" | "bad" }) {
  const cls = tone === "bad" ? "text-red-300" : tone === "warn" ? "text-yellow-300" : tone === "good" ? "text-emerald-300" : "text-emerald-200";
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${cls}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-emerald-100/50">{sub}</div>}
    </div>
  );
}

function Metric({ k, v, tone = "default" }: { k: string; v: any; tone?: "default" | "good" | "bad" }) {
  const cls = tone === "bad" ? "text-red-300" : tone === "good" ? "text-emerald-300" : "text-emerald-100";
  return (
    <div className="flex items-center justify-between gap-2 border-b border-emerald-950/60 py-1">
      <span className="text-emerald-100/50">{k}</span>
      <span className={"font-semibold " + cls}>{v}</span>
    </div>
  );
}

function Chip({ children }: { children: any }) {
  return <span className="rounded-full border border-emerald-800 bg-black/20 px-3 py-1">{children}</span>;
}

function fmt(v: any, d = 2) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "—";
}
