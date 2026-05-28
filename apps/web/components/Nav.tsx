import Link from "next/link";

const items = [
  { href: "/", label: "Dashboard" },
  { href: "/clients", label: "Clients" },
  { href: "/signals", label: "Signals" },
  { href: "/reports", label: "Reports" },
  { href: "/health", label: "Health" },
  { href: "/intelligence", label: "Intelligence" },
  { href: "/analytics", label: "Analytics" },
  { href: "/positions", label: "Positions" }
];

export default function Nav() {
  return (
    <nav className="mb-6 flex flex-wrap gap-3 rounded-2xl border border-emerald-900 bg-black/30 p-3">
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          className="rounded-xl px-4 py-2 text-sm font-semibold text-emerald-100 hover:bg-emerald-900"
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}