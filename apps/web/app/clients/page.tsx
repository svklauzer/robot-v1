"use client";

import { useEffect, useMemo, useState } from "react";
import { apiGet, apiPost } from "../../lib/api";
import { assertOk, reportActionError } from "../../lib/apiAction";
import AppShell from "../../components/AppShell";
import { RefreshCw, UserPlus, ShieldCheck, Ban, Clock, CheckCircle2 } from "lucide-react";

const PAGE_SIZE = 100;

export default function ClientsPage() {
  const [subscribers, setSubscribers] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  // (#subscribers-paging-2026-09-07) Сводка и общее число приходят с сервера.
  // Считать их на клиенте по загруженному куску значило бы показывать размер
  // страницы вместо размера базы — и заметить это было бы нечем.
  const [overview, setOverview] = useState<any>(null);
  const [matched, setMatched] = useState(0);
  const [offset, setOffset] = useState(0);

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

  async function loadSubscribers(nextOffset = offset) {
    setLoading(true);
    try {
      // Фильтры отбирают по ВСЕЙ базе, а не внутри загруженной страницы:
      // клиентский фильтр после пагинации молча сузился бы до неё.
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(nextOffset),
        status: filters.status,
        plan: filters.plan,
        trial: filters.trial,
      });
      if (filters.search.trim()) params.set("q", filters.search.trim());

      const data = await apiGet(`/subscribers?${params.toString()}`);
      setSubscribers(Array.isArray(data?.items) ? data.items : []);
      setOverview(data?.overview ?? null);
      setMatched(Number(data?.total ?? 0));
      setOffset(nextOffset);
    } finally {
      setLoading(false);
    }
  }

  async function createSubscriber() {
    if (!form.telegram_user_id.trim()) {
      alert("Telegram User ID обязателен");
      return;
    }

    // (#silent-actions-2026-09-07) Здесь стоял голый await: упавший запрос
    // не менял экран, и это неотличимо от «создал, но список не обновился».
    // Форма при этом очищалась в любом случае — введённые данные пропадали
    // вместе с ошибкой, о которой никто не узнал.
    let created: any = null;
    try {
      created = assertOk(await apiPost("/subscribers", {
        telegram_user_id: form.telegram_user_id.trim(),
        username: form.username.trim() || null,
        full_name: form.full_name.trim() || null,
        plan: form.plan.trim() || "vip",
        days: Number(form.days),
        is_trial: form.is_trial,
        notes: form.notes.trim() || null,
      }));
    } catch (e) {
      reportActionError(e);
      return;
    }

    // (#clients-audit-2026-09-12) Бэкенд не сокращает уже оплаченный срок —
    // владелец должен это увидеть, а не удивиться, что «30 дней» не сработали.
    if (created?.kept_longer_expiry) {
      alert(`Срок не сокращён: у подписчика уже оплачено до ${created.expires_at}`);
    }

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
    // Продление платного доступа: молчаливый отказ означает, что владелец
    // считает подписку продлённой, а клиент её теряет.
    try {
      assertOk(await apiPost(`/subscribers/${id}/extend`, { days }));
    } catch (e) {
      reportActionError(e);
      return;
    }
    await loadSubscribers();
  }

  async function setStatus(id: number, status: string) {
    if (status === "blocked") {
      if (!window.confirm(`⚠️ Подписчик #${id} будет заблокирован.\n\nПродолжить?`)) return;
    }

    try {
      assertOk(await apiPost(`/subscribers/${id}/status`, { status }));
    } catch (e) {
      reportActionError(e);
      return;
    }
    await loadSubscribers();
  }

  // Смена фильтра — это новый запрос и возврат к первой странице: остаться на
  // пятой странице прежней выборки значило бы показать пустоту как «ничего не
  // найдено».
  useEffect(() => {
    loadSubscribers(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  // Сводка — из ответа сервера, по всей базе. Раньше складывалась здесь из
  // полного списка; с постраничной выдачей тот же код показывал бы «Всего 100».
  const stats = {
    total: overview?.total ?? 0,
    active: overview?.active ?? 0,
    expired: overview?.expired ?? 0,
    blocked: overview?.blocked ?? 0,
    trial: overview?.trial ?? 0,
    vip: overview?.vip ?? 0,
    expiringSoon: overview?.expiring_soon ?? 0,
  };

  // Фильтрация переехала в запрос — здесь остаётся то, что пришло.
  const filteredSubscribers = subscribers;

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
            onClick={() => loadSubscribers()}
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
              показано: {filteredSubscribers.length} из {matched}
              {matched > PAGE_SIZE && (
                <span className="ml-3 inline-flex items-center gap-2">
                  <button
                    onClick={() => loadSubscribers(Math.max(0, offset - PAGE_SIZE))}
                    disabled={offset === 0 || loading}
                    className="rounded-lg border border-emerald-800 px-2 py-0.5 hover:bg-emerald-900/40 disabled:opacity-40"
                  >
                    ←
                  </button>
                  <span>
                    страница {Math.floor(offset / PAGE_SIZE) + 1} из {Math.ceil(matched / PAGE_SIZE)}
                  </span>
                  <button
                    onClick={() => loadSubscribers(offset + PAGE_SIZE)}
                    disabled={offset + PAGE_SIZE >= matched || loading}
                    className="rounded-lg border border-emerald-800 px-2 py-0.5 hover:bg-emerald-900/40 disabled:opacity-40"
                  >
                    →
                  </button>
                </span>
              )}
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
  // `expires_at` на бэкенде NOT NULL, то есть срок есть всегда. Разбор null —
  // страховка на случай смены схемы, а не описание существующего состояния.
  const daysLeft = s.days_left == null ? null : Number(s.days_left);

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
          <div className={daysLeft != null && daysLeft <= 3 && s.status === "active" ? "text-lg font-bold text-yellow-300" : "text-lg font-bold text-emerald-200"}>
            {daysLeft == null ? "—" : `${daysLeft} дней`}
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