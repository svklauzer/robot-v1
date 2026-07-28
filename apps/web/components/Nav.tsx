"use client";

/* (#ui-cleanup-2026-07-28) Навигация была плоским списком из 16 равнозначных
   пунктов в две строки. Плоский список не различает то, на что смотрят каждый
   день, и то, что открывают раз в месяц — и не показывает, что часть контуров
   вообще выключена (Grid, Venues).

   Три группы вместо одной кучи, отключённые контуры помечены и приглушены:
   страница остаётся доступной (историю смотреть надо), но не притворяется
   живой. */

import Link from "next/link";
import { usePathname } from "next/navigation";

type Item = { href: string; label: string; off?: string };

const GROUPS: { title: string; items: Item[] }[] = [
  {
    title: "Торговля",
    items: [
      { href: "/", label: "Обзор" },
      { href: "/signals", label: "Сигналы" },
      { href: "/positions", label: "Позиции" },
      { href: "/intelligence", label: "Решения" },
      { href: "/funding", label: "Funding Arb" },
      // Отключены по замеру 28.07: grid −5.74 на 185 циклах,
      // cross-arb 10 убытков из 10 закрытий.
      { href: "/grid", label: "Grid", off: "выключен" },
      { href: "/venues", label: "Venues", off: "cross-arb выкл" },
    ],
  },
  {
    title: "Анализ",
    items: [
      { href: "/analytics", label: "Аналитика" },
      { href: "/backtest", label: "Back test" },
      { href: "/orderbook", label: "Стакан" },
      { href: "/ml", label: "ML" },
      { href: "/reports", label: "Отчёты" },
    ],
  },
  {
    title: "Система",
    items: [
      { href: "/health", label: "Здоровье" },
      { href: "/clients", label: "Клиенты" },
      { href: "/payments", label: "Платежи" },
    ],
  },
];

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-4 z-20 rounded-3xl border border-emerald-800/70 bg-slate-950/80 p-3 shadow-2xl shadow-emerald-950/30 backdrop-blur">
      <div className="flex flex-col gap-2">
        {GROUPS.map((group) => (
          <div key={group.title} className="flex flex-wrap items-center gap-2">
            <span className="w-20 shrink-0 text-xs font-semibold uppercase tracking-wide text-emerald-100/35">
              {group.title}
            </span>
            {group.items.map((item) => {
              const active =
                item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);

              const base = "shrink-0 whitespace-nowrap rounded-2xl px-3.5 py-1.5 text-sm transition";
              const cls = active
                ? `${base} bg-emerald-400 font-bold text-slate-950 shadow-lg shadow-emerald-900/30`
                : item.off
                  ? `${base} font-medium text-emerald-100/30 hover:bg-emerald-900/40 hover:text-emerald-100/60`
                  : `${base} font-semibold text-emerald-100/75 hover:bg-emerald-900/60 hover:text-emerald-50`;

              return (
                <Link key={item.href} href={item.href} className={cls} title={item.off || undefined}>
                  {item.label}
                  {item.off && (
                    <span className="ml-1.5 rounded bg-slate-700/70 px-1.5 py-0.5 text-[10px] font-normal text-slate-300">
                      {item.off}
                    </span>
                  )}
                </Link>
              );
            })}
          </div>
        ))}
      </div>
    </nav>
  );
}
