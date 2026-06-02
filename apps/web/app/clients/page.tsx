"use client";

import { useEffect, useMemo, useState } from "react";
import { apiGet, apiPost } from "../../lib/api";
import AppShell from "../../components/AppShell";
import { RefreshCw, UserPlus, ShieldCheck, Ban, Clock, CheckCircle2 } from "lucide-react";

export default function ClientsPage() {
  const [subscribers, setSubscribers] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const [filters, setFilters] = useState({
    status: "all",
    plan: "all",
    trial: "all",
    search: "",
  });

  const [form, setForm] = useState({
    telegram_user_id: "",
    username: "",
    full_name: "",
    plan: "vip",
    days: 30,
    is_trial: false,
    notes: "",
  });

  async function loadSubscribers() {
    setLoading(true);
    try {
      const data = await apiGet("/subscribers");
      setSubscribers(Array.isArray(data) ? data : []);
    } finally {
      setLoading(false);
    }
  }

  async function createSubscriber() {
    if (!form.telegram_user_id.trim()) {
      alert("Telegram User ID обязателен");
      return;
    }

    await apiPost("/subscribers", {
      telegram_user_id: form.telegram_user_id.trim(),
      username: form.username.trim() || null,
      full_name: form.full_name.trim() || null,
      plan: form.plan.trim() || "vip",
      days: Number(form.days),
      is_trial: form.is_trial,
      notes: form.notes.trim() || null,
    });

    setForm({
      telegram_user_id: "",
      username: "",
      full_name: "",
      plan: "vip",
      days: 30,
      is_trial: false,
      notes: "",
    });

    await loadSubscribers();
  }

  async function extendSubscriber(id: number, days: number) {
    await apiPost(`/subscribers/${id}/extend`, { days });
    await loadSubscribers();
  }

  async function setStatus(id: number, status: string) {
    if (status === "blocked") {
      if (!window.confirm(`⚠️ Подписчик #${id} будет заблокирован.\n\nПродолжить?`)) return;
    }

    await apiPost(`/subscribers/${id}/status`, { status });
    await loadSubscribers();
  }

  useEffect(() => {
    loadSubscribers();
  }, []);

  const stats = useMemo(() => {
    const total = subscribers.length;
    const active = subscribers.filter((s) => s.status === "active").length;
    const expired = subscribers.filter((s) => s.status === "expired").length;
    const blocked = subscribers.filter((s) => s.status === "blocked").length;
    const trial = subscribers.filter((s) => s.is_trial).length;
    const vip = subscribers.filter((s) => s.plan === "vip").length;

    const expiringSoon = subscribers.filter((s) => {
      const days = Number(s.days_left ?? 0);
      return s.status === "active" && days >= 0 && days <= 3;
    }).length;

    return {
      total,
      active,
      expired,
      blocked,
      trial,
      vip,
      expiringSoon,
    };
  }, [subscribers]);

  const filteredSubscribers = useMemo(() => {
    const q = filters.search.trim().toLowerCase();

    return subscribers.filter((s) => {
      if (filters.status !== "all" && s.status !== filters.status) return false;
      if (filters.plan !== "all" && s.plan !== filters.plan) return false;

      if (filters.trial === "trial" && !s.is_trial) return false;
      if (filters.trial === "paid" && s.is_trial) return false;

      if (q) {
        const haystack = [
          s.telegram_user_id,
          s.username,
          s.full_name,
          s.plan,
          s.status,
          s.notes,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();

        if (!haystack.includes(q)) return false;
      }

      return true;
    });
  }, [subscribers, filters]);

  return (
    <AppShell>

        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-emerald-300">
              Clients / Subscribers
            </h1>
            <p className="text-sm text-emerald-100/70">
              Управление подписчиками VIP-канала, сроками и статусами доступа
            </p>
          </div>

          <button
            onClick={loadSubscribers}
            className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700"
          >
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
        </header>

        <section className="grid grid-cols-2 gap-4 md:grid-cols-4 xl:grid-cols-7">
          <StatCard title="Всего" value={stats.total} />
          <StatCard title="Active" value={stats.active} good />
          <StatCard title="Expired" value={stats.expired} />
          <StatCard title="Blocked" value={stats.blocked} danger={stats.blocked > 0} />
          <StatCard title="VIP" value={stats.vip} />
          <StatCard title="Trial" value={stats.trial} />
          <StatCard title="Soon" value={stats.expiringSoon} warn={stats.expiringSoon > 0} />
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center gap-2">
            <UserPlus size={18} className="text-emerald-300" />
            <h2 className="text-xl font-semibold text-emerald-200">
              Добавить / обновить подписчика
            </h2>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            <Input
              label="Telegram User ID"
              value={form.telegram_user_id}
              onChange={(v) => setForm({ ...form, telegram_user_id: v })}
            />

            <Input
              label="Username"
              value={form.username}
              onChange={(v) => setForm({ ...form, username: v })}
              placeholder="без @"
            />

            <Input
              label="Full name"
              value={form.full_name}
              onChange={(v) => setForm({ ...form, full_name: v })}
            />

            <Select
              label="Plan"
              value={form.plan}
              onChange={(v) => setForm({ ...form, plan: v })}
              options={[
                { value: "vip", label: "vip" },
                { value: "free", label: "free" },
              ]}
            />

            <Input
              label="Days"
              value={String(form.days)}
              onChange={(v) => setForm({ ...form, days: Number(v || 0) })}
              type="number"
            />

            <Input
              label="Notes"
              value={form.notes}
              onChange={(v) => setForm({ ...form, notes: v })}
            />
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 rounded-xl border border-emerald-900 bg-black/30 px-3 py-2 text-sm text-emerald-100/80">
              <input
                type="checkbox"
                checked={form.is_trial}
                onChange={(e) => setForm({ ...form, is_trial: e.target.checked })}
              />
              Trial
            </label>

            <button
              onClick={createSubscriber}
              className="flex items-center gap-2 rounded-xl bg-emerald-500 px-4 py-2 font-semibold text-black hover:bg-emerald-400"
            >
              <CheckCircle2 size={16} />
              Добавить / обновить
            </button>
          </div>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 text-xl font-semibold text-emerald-200">
            Фильтры
          </h2>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
            <Input
              label="Search"
              value={filters.search}
              onChange={(v) => setFilters({ ...filters, search: v })}
              placeholder="id, username, name, notes"
            />

            <Select
              label="Status"
              value={filters.status}
              onChange={(v) => setFilters({ ...filters, status: v })}
              options={[
                { value: "all", label: "all" },
                { value: "active", label: "active" },
                { value: "expired", label: "expired" },
                { value: "blocked", label: "blocked" },
              ]}
            />

            <Select
              label="Plan"
              value={filters.plan}
              onChange={(v) => setFilters({ ...filters, plan: v })}
              options={[
                { value: "all", label: "all" },
                { value: "vip", label: "vip" },
                { value: "free", label: "free" },
              ]}
            />

            <Select
              label="Type"
              value={filters.trial}
              onChange={(v) => setFilters({ ...filters, trial: v })}
              options={[
                { value: "all", label: "all" },
                { value: "trial", label: "trial" },
                { value: "paid", label: "paid" },
              ]}
            />
          </div>
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-xl font-semibold text-emerald-200">
              Подписчики
            </h2>
            <span className="text-sm text-emerald-100/50">
              показано: {filteredSubscribers.length} / {subscribers.length}
            </span>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {filteredSubscribers.map((s) => (
              <SubscriberCard
                key={s.id}
                subscriber={s}
                onExtend={extendSubscriber}
                onStatus={setStatus}
              />
            ))}

            {filteredSubscribers.length === 0 && (
              <div className="rounded-2xl border border-emerald-950 bg-black/20 p-8 text-center text-emerald-100/50 xl:col-span-2">
                Подписчиков пока нет
              </div>
            )}
          </div>
        </section>
    </AppShell>
  );
}

function SubscriberCard({
  subscriber,
  onExtend,
  onStatus,
}: {
  subscriber: any;
  onExtend: (id: number, days: number) => void;
  onStatus: (id: number, status: string) => void;
}) {
  const s = subscriber;
  const daysLeft = Number(s.days_left ?? 0);

  return (
    <article className="rounded-2xl border border-emerald-900 bg-black/30 p-4">
      <div className="mb-4 flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-bold text-emerald-300">
              {s.full_name || s.username || s.telegram_user_id}
            </h3>
            <StatusBadge status={s.status} />
            <PlanBadge plan={s.plan} />
            {s.is_trial && <SmallBadge text="trial" tone="warn" />}
          </div>

          <div className="mt-1 text-xs text-emerald-100/50">
            ID #{s.id} · Telegram {s.telegram_user_id}
          </div>
        </div>

        <div className="text-left md:text-right">
          <div className={daysLeft <= 3 && s.status === "active" ? "text-lg font-bold text-yellow-300" : "text-lg font-bold text-emerald-200"}>
            {daysLeft} дней
          </div>
          <div className="text-xs text-emerald-100/50">до окончания</div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MiniBox label="Username" value={s.username ? `@${s.username}` : "-"} />
        <MiniBox label="Plan" value={s.plan || "-"} />
        <MiniBox label="Trial" value={s.is_trial ? "yes" : "no"} />
        <MiniBox label="Status" value={s.status || "-"} />
        <MiniBox label="Starts" value={formatDate(s.starts_at)} />
        <MiniBox label="Expires" value={formatDate(s.expires_at)} />
        <MiniBox label="Created" value={formatDate(s.created_at)} />
        <MiniBox label="Notes" value={s.notes || "-"} wide />
      </div>

      <div className="mt-4 flex flex-wrap gap-2 border-t border-emerald-950 pt-4">
        <ActionButton onClick={() => onExtend(s.id, 7)} tone="blue">
          <Clock size={14} />
          +7d
        </ActionButton>

        <ActionButton onClick={() => onExtend(s.id, 30)} tone="green">
          <Clock size={14} />
          +30d
        </ActionButton>

        <ActionButton onClick={() => onStatus(s.id, "active")} tone="yellow">
          <ShieldCheck size={14} />
          Active
        </ActionButton>

        <ActionButton onClick={() => onStatus(s.id, "blocked")} tone="red">
          <Ban size={14} />
          Block
        </ActionButton>
      </div>
    </article>
  );
}

function StatCard({
  title,
  value,
  good,
  warn,
  danger,
}: {
  title: string;
  value: any;
  good?: boolean;
  warn?: boolean;
  danger?: boolean;
}) {
  const valueClass = danger
    ? "text-red-300"
    : warn
      ? "text-yellow-300"
      : good
        ? "text-emerald-300"
        : "text-emerald-200";

  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={`mt-2 text-2xl font-bold ${valueClass}`}>{value}</div>
    </div>
  );
}

function MiniBox({
  label,
  value,
  wide,
}: {
  label: string;
  value: any;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "col-span-2 rounded-xl border border-emerald-950 bg-black/20 p-3 md:col-span-2" : "rounded-xl border border-emerald-950 bg-black/20 p-3"}>
      <div className="text-xs text-emerald-100/50">{label}</div>
      <div className="mt-1 break-words text-sm font-semibold text-emerald-100">
        {String(value)}
      </div>
    </div>
  );
}

function Input({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="space-y-1">
      <div className="text-sm text-emerald-100/70">{label}</div>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-xl border border-emerald-900 bg-black/40 px-3 py-2 text-emerald-100 outline-none focus:border-emerald-400"
      />
    </label>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="space-y-1">
      <div className="text-sm text-emerald-100/70">{label}</div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-xl border border-emerald-900 bg-black/40 px-3 py-2 text-emerald-100 outline-none focus:border-emerald-400"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "active"
      ? "bg-emerald-500 text-black"
      : status === "expired"
        ? "bg-yellow-700 text-black"
        : status === "blocked"
          ? "bg-red-700 text-white"
          : "bg-emerald-950 text-emerald-200";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {status || "-"}
    </span>
  );
}

function PlanBadge({ plan }: { plan: string }) {
  const cls =
    plan === "vip"
      ? "bg-emerald-700 text-white"
      : "bg-sky-700 text-white";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {plan || "-"}
    </span>
  );
}

function SmallBadge({ text, tone }: { text: string; tone?: "warn" | "good" }) {
  const cls = tone === "warn" ? "bg-yellow-600 text-black" : "bg-emerald-700 text-white";

  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${cls}`}>
      {text}
    </span>
  );
}

function ActionButton({
  children,
  onClick,
  tone,
}: {
  children: React.ReactNode;
  onClick: () => void;
  tone: "green" | "blue" | "yellow" | "red";
}) {
  const cls =
    tone === "green"
      ? "bg-emerald-700 hover:bg-emerald-600 text-white"
      : tone === "blue"
        ? "bg-blue-700 hover:bg-blue-600 text-white"
        : tone === "yellow"
          ? "bg-yellow-500 hover:bg-yellow-400 text-black"
          : "bg-red-700 hover:bg-red-600 text-white";

  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1 rounded-lg px-3 py-1 text-xs font-semibold ${cls}`}
    >
      {children}
    </button>
  );
}

function formatDate(value: string | null | undefined) {
  if (!value) return "-";

  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;

  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}