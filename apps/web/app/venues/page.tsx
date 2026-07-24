"use client";

// (#venues-page-2026-07-24) Страница Venues: funding-спреды HTX↔Kraken (P1) и
// paper cross-funding-arb (P2). До этого владелец мониторил их curl'ом.

import { useEffect, useRef, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { ArrowLeftRight, RefreshCw } from "lucide-react";

const WINDOWS = [
  { label: "24ч", days: 1 },
  { label: "3д", days: 3 },
  { label: "7д", days: 7 },
  { label: "14д", days: 14 },
];

export default function VenuesPage() {
  const [arb, setArb] = useState<any>(null);
  const [history, setHistory] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [days, setDays] = useState(3);
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);

  async function load(withHealth = false) {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      const [a, h] = await Promise.all([
        apiGet("/venues/cross-arb").catch(() => null),
        apiGet(`/venues/compare/history?days=${days}`).catch(() => null),
      ]);
      setArb(a);
      setHistory(h);
      if (withHealth) {
        // health дёргает обе биржи (медленно) — только по явному запросу/первому заходу
        setHealth(await apiGet("/venues/health").catch(() => null));
      }
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  useEffect(() => {
    load(true);
  }, []);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  const gates = arb?.gates || {};
  const open = arb?.open || [];
  const closed = arb?.closed_recent || [];
  const bySymbol = history?.by_symbol || [];

  return (
    <AppShell>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-bold text-emerald-200">
          <ArrowLeftRight /> Venues — HTX ↔ Kraken
        </h1>
        <button onClick={() => load(true)} className="flex items-center gap-2 rounded-xl border border-emerald-800 px-4 py-2 text-sm hover:bg-emerald-900/40">
          <RefreshCw size={16} className={loading ? "animate-spin" : ""} /> Обновить
        </button>
      </div>

      {/* Health обеих площадок */}
      <section className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2">
        {["htx", "kraken"].map((v) => {
          const s = health?.[v];
          return (
            <div key={v} className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
              <div className="text-sm uppercase text-emerald-100/60">{v.toUpperCase()}</div>
              <div className={"mt-1 text-xl font-bold " + (s?.ok ? "text-emerald-300" : "text-red-300")}>
                {s == null ? "…" : s.ok ? "OK" : "FAIL"}
                {s?.latency_ms != null && <span className="ml-2 text-sm font-normal text-emerald-100/60">{s.latency_ms} ms</span>}
              </div>
              {s?.btc_mark != null && <div className="text-xs text-emerald-100/50">BTC mark: {s.btc_mark}</div>}
              {s?.error && <div className="mt-1 text-xs text-red-300/80">{s.error}</div>}
            </div>
          );
        })}
      </section>

      {/* P2: paper cross-funding-arb */}
      <section className="mb-6 rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-emerald-200">Cross-Funding-Arb (paper)</h2>
          <div className="flex flex-wrap gap-2 text-xs text-emerald-100/70">
            <Chip>{arb?.enabled ? "ВКЛ" : "ВЫКЛ"}</Chip>
            <Chip>вход ≥{gates.min_ann_pct}% год.</Chip>
            <Chip>устойч. ≥{gates.min_stability_pct}%</Chip>
            <Chip>выход &lt;{gates.close_ann_pct}%</Chip>
            <Chip>нога {gates.notional_usdt} USDT</Chip>
            <Chip>макс {gates.max_positions} поз.</Chip>
          </div>
        </div>

        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat k="Реализовано всего" v={fmtUsd(arb?.realized_total_usdt)} tone={num(arb?.realized_total_usdt) < 0 ? "bad" : "good"} />
          <Stat k="Открыто ног" v={open.length} />
          <Stat k="Закрыто всего" v={arb?.closed_count ?? "—"} />
          <Stat
            k="Unrealized (сумма)"
            v={fmtUsd(open.reduce((s: number, p: any) => s + num(p.unrealized_net_usdt), 0))}
            tone={open.reduce((s: number, p: any) => s + num(p.unrealized_net_usdt), 0) < 0 ? "bad" : "good"}
          />
        </div>

        {open.length > 0 && (
          <div className="mb-4 space-y-2">
            {open.map((p: any) => (
              <div key={p.id} className="rounded-xl border border-emerald-900/70 bg-emerald-950/20 p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold text-emerald-200">{p.symbol}</span>
                  <span className="rounded bg-emerald-900/60 px-2 py-0.5 text-xs">{dirLabel(p.direction)}</span>
                  <span className="text-emerald-100/50 text-xs">с {fmtDt(p.opened_at)}</span>
                  {p.exit_streak > 0 && (
                    <span className="rounded bg-yellow-900/60 px-2 py-0.5 text-xs text-yellow-200">
                      выход {p.exit_streak}/3: {p.exit_streak_reason}
                    </span>
                  )}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-5">
                  <Metric k="Спред вход→сейчас" v={`${fmtPct(p.entry_spread_ann_pct)} → ${fmtPct(p.last_spread_ann_pct)}`} />
                  <Metric k="Carry" v={fmtUsd(p.funding_accrued_usdt)} cls={num(p.funding_accrued_usdt) < 0 ? "text-red-300" : "text-emerald-300"} />
                  <Metric k="Базис" v={fmtUsd(p.unrealized_basis_usdt)} cls={num(p.unrealized_basis_usdt) < 0 ? "text-red-300" : "text-emerald-300"} />
                  <Metric k="Комиссии" v={fmtUsd(-num(p.fees_round_trip_usdt))} cls="text-red-300" />
                  <Metric k="Unrealized net" v={fmtUsd(p.unrealized_net_usdt)} cls={num(p.unrealized_net_usdt) < 0 ? "text-red-300 font-bold" : "text-emerald-300 font-bold"} />
                </div>
              </div>
            ))}
          </div>
        )}

        {closed.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-xs">
              <thead className="text-emerald-100/50">
                <tr>
                  <th className="px-2 py-1">Символ</th>
                  <th className="px-2 py-1">Вход</th>
                  <th className="px-2 py-1">Спред входа</th>
                  <th className="px-2 py-1">Причина закрытия</th>
                  <th className="px-2 py-1">Carry</th>
                  <th className="px-2 py-1">Realized</th>
                </tr>
              </thead>
              <tbody>
                {[...closed].reverse().map((p: any) => (
                  <tr key={p.id} className="border-t border-emerald-950">
                    <td className="px-2 py-1 font-semibold text-emerald-200">{p.symbol}</td>
                    <td className="px-2 py-1 text-emerald-100/60">{fmtDt(p.opened_at)}</td>
                    <td className="px-2 py-1">{fmtPct(p.entry_spread_ann_pct)}</td>
                    <td className="px-2 py-1 text-emerald-100/70">{p.close_reason}</td>
                    <td className="px-2 py-1">{fmtUsd(p.funding_accrued_usdt)}</td>
                    <td className={"px-2 py-1 font-semibold " + (num(p.realized_usdt) < 0 ? "text-red-300" : "text-emerald-300")}>{fmtUsd(p.realized_usdt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* P1: история funding-спредов */}
      <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-emerald-200">Funding-спреды (спред&gt;0 → шорт HTX + лонг Kraken)</h2>
          <div className="flex gap-1">
            {WINDOWS.map((w) => (
              <button
                key={w.days}
                onClick={() => setDays(w.days)}
                className={
                  days === w.days
                    ? "rounded-lg bg-emerald-400 px-3 py-1 text-xs font-bold text-slate-950"
                    : "rounded-lg border border-emerald-800 px-3 py-1 text-xs text-emerald-100/70 hover:bg-emerald-900/40"
                }
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>
        <div className="mb-2 text-xs text-emerald-100/50">
          Снапшотов в окне: {history?.snapshots ?? "—"} (почасовые)
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="text-xs uppercase text-emerald-100/50">
              <tr>
                <th className="px-2 py-2">Символ</th>
                <th className="px-2 py-2">Avg, % год.</th>
                <th className="px-2 py-2">Min / Max</th>
                <th className="px-2 py-2">Устойчивость</th>
                <th className="px-2 py-2">Сейчас</th>
                <th className="px-2 py-2">Направление</th>
              </tr>
            </thead>
            <tbody>
              {bySymbol.map((r: any) => (
                <tr key={r.symbol} className="border-t border-emerald-950">
                  <td className="px-2 py-2 font-semibold text-emerald-200">{r.symbol}</td>
                  <td className={"px-2 py-2 font-semibold " + (num(r.avg_spread_ann_pct) >= 12 ? "text-emerald-300" : "text-emerald-100/70")}>
                    {fmtPct(r.avg_spread_ann_pct)}
                  </td>
                  <td className="px-2 py-2 text-emerald-100/60">
                    {fmtPct(r.min_spread_ann_pct)} / {fmtPct(r.max_spread_ann_pct)}
                  </td>
                  <td className={"px-2 py-2 " + (num(r.direction_stability_pct) >= 80 ? "text-emerald-300" : "text-yellow-300")}>
                    {fmtPct(r.direction_stability_pct)}
                  </td>
                  <td className={"px-2 py-2 " + (num(r.last_spread_ann_pct) < 0 ? "text-red-300" : "")}>{fmtPct(r.last_spread_ann_pct)}</td>
                  <td className="px-2 py-2 text-xs text-emerald-100/60">{dirLabel(r.dominant_direction)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </AppShell>
  );
}

function dirLabel(d: any) {
  if (d === "short_htx_long_kraken") return "шорт HTX · лонг Kraken";
  if (d === "short_kraken_long_htx") return "шорт Kraken · лонг HTX";
  return String(d ?? "—");
}

function num(v: any) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function fmtUsd(v: any) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n.toFixed(3)} USDT` : "—";
}

function fmtPct(v: any) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : "—";
}

function fmtDt(v: any) {
  if (!v) return "—";
  try {
    return new Date(v).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return String(v);
  }
}

function Chip({ children }: { children: any }) {
  return <span className="rounded-full border border-emerald-800 bg-black/20 px-3 py-1">{children}</span>;
}

function Stat({ k, v, tone }: { k: string; v: any; tone?: "good" | "bad" }) {
  const cls = tone === "bad" ? "text-red-300" : tone === "good" ? "text-emerald-300" : "text-emerald-200";
  return (
    <div className="rounded-xl border border-emerald-900/70 bg-black/20 p-3">
      <div className="text-xs text-emerald-100/50">{k}</div>
      <div className={"mt-1 text-lg font-bold " + cls}>{v}</div>
    </div>
  );
}

function Metric({ k, v, cls = "" }: { k: string; v: any; cls?: string }) {
  return (
    <div className="rounded-lg bg-black/20 px-2 py-1">
      <span className="block text-[10px] uppercase text-emerald-100/40">{k}</span>
      <span className={"text-xs " + cls}>{v}</span>
    </div>
  );
}
