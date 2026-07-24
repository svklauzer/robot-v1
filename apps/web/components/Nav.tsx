"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const items = [
  { href: "/", label: "Dashboard" },
  { href: "/clients", label: "Clients" },
  { href: "/payments", label: "Payments" },
  { href: "/funding", label: "Funding Arb" },
  { href: "/signals", label: "Signals" },
  { href: "/positions", label: "Positions" },
  { href: "/analytics", label: "Analytics" },
  { href: "/reports", label: "Reports" },
  { href: "/health", label: "Health" },
  { href: "/intelligence", label: "Intelligence" },
  { href: "/orderbook", label: "Order Book" },
  { href: "/ml", label: "ML" },
  { href: "/grid", label: "Grid" },
  { href: "/venues", label: "Venues" },
];

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav className="sticky top-4 z-20 rounded-3xl border border-emerald-800/70 bg-slate-950/80 p-2 shadow-2xl shadow-emerald-950/30 backdrop-blur">
      {/* (#venues-page-2026-07-24) 14 пунктов перестали влезать в одну строку:
          горизонтальный скролл со скрытым скроллбаром молча обрезал хвост
          («Venues» было не видно). Перенос строк вместо прокрутки. */}
      <div className="flex flex-wrap gap-2">
        {items.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);

          return (
            <Link
              key={item.href}
              href={item.href}
              className={
                active
                  ? "shrink-0 whitespace-nowrap rounded-2xl bg-emerald-400 px-4 py-2 text-sm font-bold text-slate-950 shadow-lg shadow-emerald-900/30"
                  : "shrink-0 whitespace-nowrap rounded-2xl px-4 py-2 text-sm font-semibold text-emerald-100/75 transition hover:bg-emerald-900/60 hover:text-emerald-50"
              }
            >
              {item.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
