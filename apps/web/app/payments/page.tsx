"use client";

import { useEffect, useMemo, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";
import { CreditCard, RefreshCw, CheckCircle2 } from "lucide-react";

export default function PaymentsPage() {
  const [payments, setPayments] = useState<any[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [plans, setPlans] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [form, setForm] = useState({ telegram_user_id: "", username: "", full_name: "", plan_code: "vip_30" });

  async function loadAll() {
    setLoading(true);
    try {
      const [paymentsData, plansData] = await Promise.all([
        apiGet("/payments?limit=100"),
        apiGet("/payments/plans"),
      ]);
      setPayments(paymentsData?.items || []);
      setSummary(paymentsData?.summary || null);
      setPlans(Array.isArray(plansData) ? plansData : []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  const activePlan = useMemo(() => plans.find((p) => p.code === form.plan_code), [plans, form.plan_code]);

  async function createCheckout() {
    if (!form.telegram_user_id.trim()) {
      alert("Введите Telegram ID");
      return;
    }

    const res = await apiPost("/payments/checkout", {
      telegram_user_id: form.telegram_user_id.trim(),
      username: form.username.trim() || undefined,
      full_name: form.full_name.trim() || undefined,
      plan_code: form.plan_code,
      provider: "manual",
      notes: "owner_ui_checkout",
    });

    if (res?.status !== "ok") {
      alert(`Ошибка создания checkout: ${res?.error || "unknown"}`);
    }

    setForm({ telegram_user_id: "", username: "", full_name: "", plan_code: "vip_30" });
    await loadAll();
  }

  async function confirmPayment(id: number) {
    if (!window.confirm(`Подтвердить оплату #${id} и активировать подписку?`)) return;
    const res = await apiPost(`/payments/${id}/manual-confirm`, { provider_event_id: `owner-confirm-${Date.now()}` });
    if (res?.status !== "ok") {
      alert(`Ошибка подтверждения: ${res?.error || "unknown"}`);
    }
    await loadAll();
  }

  return (
    <AppShell>
        <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="flex items-center gap-3 text-3xl font-bold text-emerald-300">
              <CreditCard />
              Payments
            </h1>
            <p className="mt-2 text-emerald-100/60">
              Manual checkout MVP: создание pending платежей и подтверждение активации VIP.
            </p>
          </div>

          <button onClick={loadAll} className="flex items-center gap-2 rounded-xl bg-emerald-800 px-4 py-2 font-semibold hover:bg-emerald-700">
            <RefreshCw size={16} />
            {loading ? "Обновление..." : "Обновить"}
          </button>
        </header>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Stat title="Total" value={summary?.total ?? 0} />
          <Stat title="Paid" value={summary?.paid ?? 0} good />
          <Stat title="Pending" value={summary?.pending ?? 0} warn />
          <Stat title="Cash" value={`${summary?.cash_collected ?? 0} ${summary?.currency ?? "USDT"}`} good />
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 text-xl font-semibold text-emerald-200">Создать checkout</h2>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
            <Input label="Telegram ID" value={form.telegram_user_id} onChange={(v) => setForm({ ...form, telegram_user_id: v })} />
            <Input label="Username" value={form.username} onChange={(v) => setForm({ ...form, username: v })} />
            <Input label="Full name" value={form.full_name} onChange={(v) => setForm({ ...form, full_name: v })} />
            <label className="text-sm">
              <span className="mb-1 block text-emerald-100/60">Plan</span>
              <select
                value={form.plan_code}
                onChange={(e) => setForm({ ...form, plan_code: e.target.value })}
                className="w-full rounded-xl border border-emerald-900 bg-black/40 px-3 py-2 text-emerald-100"
              >
                {plans.map((plan) => (
                  <option key={plan.code} value={plan.code}>{plan.title}</option>
                ))}
              </select>
            </label>
            <button onClick={createCheckout} className="mt-6 rounded-xl bg-emerald-700 px-4 py-2 font-semibold hover:bg-emerald-600">
              Создать
            </button>
          </div>
          {activePlan && (
            <p className="mt-3 text-sm text-emerald-100/50">
              {activePlan.amount_usdt} {activePlan.currency}, {activePlan.duration_days} дней
            </p>
          )}
        </section>

        <section className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
          <h2 className="mb-4 text-xl font-semibold text-emerald-200">Платежи</h2>
          <div className="space-y-3">
            {payments.map((payment) => (
              <article key={payment.id} className="rounded-xl border border-emerald-950 bg-black/20 p-4">
                <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                  <div>
                    <div className="font-semibold text-emerald-200">#{payment.id} · {payment.plan_code} · {payment.amount} {payment.currency}</div>
                    <div className="mt-1 text-sm text-emerald-100/50">
                      Telegram {payment.telegram_user_id} · @{payment.username || "-"} · {payment.status}
                    </div>
                  </div>
                  {payment.status === "pending" && (
                    <button onClick={() => confirmPayment(payment.id)} className="flex items-center gap-2 rounded-xl bg-emerald-700 px-4 py-2 font-semibold hover:bg-emerald-600">
                      <CheckCircle2 size={16} />
                      Confirm
                    </button>
                  )}
                </div>
              </article>
            ))}
            {payments.length === 0 && <div className="p-8 text-center text-emerald-100/50">Платежей пока нет.</div>}
          </div>
        </section>
    </AppShell>
  );
}

function Input({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="text-sm">
      <span className="mb-1 block text-emerald-100/60">{label}</span>
      <input value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-xl border border-emerald-900 bg-black/40 px-3 py-2 text-emerald-100" />
    </label>
  );
}

function Stat({ title, value, good, warn }: { title: string; value: any; good?: boolean; warn?: boolean }) {
  return (
    <div className="rounded-2xl border border-emerald-900 bg-black/30 p-5">
      <div className="text-sm text-emerald-100/60">{title}</div>
      <div className={good ? "mt-2 text-2xl font-bold text-emerald-300" : warn ? "mt-2 text-2xl font-bold text-yellow-300" : "mt-2 text-2xl font-bold text-emerald-200"}>{value}</div>
    </div>
  );
}
