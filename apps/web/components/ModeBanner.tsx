"use client";

import { useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type LiveState = {
  effective_mode?: string;      // off | dry_run | live
  configured_mode?: string;
  enable_live_orders?: boolean;
  robot_mode?: string;
  trading_mode?: string;
  execution_market?: string;
};

// Визуальная схема режима. LIVE — красный (реальные деньги), DRY-RUN — янтарь
// (живой путь логируется, но ордера не уходят), PAPER — спокойный изумруд.
const STYLES: Record<string, { label: string; sub: string; cls: string; dot: string }> = {
  live: {
    label: "LIVE",
    sub: "реальные ордера на HTX — настоящие деньги",
    cls: "border-red-500/70 bg-red-950/70 text-red-100",
    dot: "bg-red-400 animate-pulse",
  },
  dry_run: {
    label: "DRY-RUN",
    sub: "живой путь логируется, ордера НЕ отправляются",
    cls: "border-amber-500/60 bg-amber-950/60 text-amber-100",
    dot: "bg-amber-400",
  },
  off: {
    label: "PAPER",
    sub: "бумажная симуляция — биржа не задействована",
    cls: "border-emerald-700/60 bg-emerald-950/50 text-emerald-100",
    dot: "bg-emerald-400",
  },
};

export default function ModeBanner() {
  const [state, setState] = useState<LiveState | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await apiGet("/live/state");
        if (alive) { setState(data); setErr(false); }
      } catch {
        if (alive) setErr(true);
      }
    };
    load();
    const t = setInterval(load, 30000); // 30s — режим меняется редко
    return () => { alive = false; clearInterval(t); };
  }, []);

  if (err || !state) return null; // бэк без эндпоинта / недоступен → не мешаем

  const mode = String(state.effective_mode || "off").toLowerCase();
  const s = STYLES[mode] || STYLES.off;

  return (
    <div className={`sticky top-0 z-30 flex flex-wrap items-center justify-between gap-2 rounded-2xl border px-4 py-2 text-sm font-semibold shadow-lg ${s.cls}`}>
      <div className="flex items-center gap-2">
        <span className={`inline-block h-2.5 w-2.5 rounded-full ${s.dot}`} />
        <span className="text-base font-extrabold tracking-wide">{s.label}</span>
        <span className="opacity-80">{s.sub}</span>
      </div>
      <div className="flex items-center gap-3 text-xs opacity-75">
        <span>robot: {state.robot_mode ?? "—"}</span>
        <span>trading: {state.trading_mode ?? "—"}</span>
        <span>market: {state.execution_market ?? "—"}</span>
        {mode === "dry_run" && state.enable_live_orders === false && (
          <span className="opacity-70">(live заблокирован флагом)</span>
        )}
      </div>
    </div>
  );
}
