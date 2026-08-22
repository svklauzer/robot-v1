"use client";

// Страница конфигурации — ТОЛЬКО ЧТЕНИЕ (#config-visibility-2026-08-21).
//
// Настройка живёт в двух местах: дефолт в config.py и перекрытие в render.yaml
// → env. По коду не видно, какое из двух действует, и это стоило времени:
// правишь дефолт, деплоишь, а в проде значение из блупринта.
//
// Редактирования здесь нет намеренно. Числовые пороги торговли правятся
// коммитом, чтобы у каждой правки остались ревью, тесты и записанная причина.
// Форма в браузере превратила бы порог в то, что двигают под настроение рынка.

import { useEffect, useMemo, useRef, useState } from "react";
import AppShell from "../../components/AppShell";
import { apiGet } from "../../lib/api";
import { RefreshCw, Search, Lock, Server, FileCode2, AlertTriangle } from "lucide-react";

type Row = {
  name: string;
  value: any;
  default?: any;
  source: "env" | "default";
  secret: boolean;
  redundant?: boolean;
};

type Group = { name: string; items: Row[] };

export default function ConfigPage() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [onlyEnv, setOnlyEnv] = useState(false);
  const loadingRef = useRef(false);

  async function load() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      setData(await apiGet("/system/config-effective"));
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const groups: Group[] = useMemo(() => {
    if (!data?.groups) return [];
    const q = query.trim().toLowerCase();
    return data.groups
      .map((g: Group) => ({
        ...g,
        items: g.items.filter((r) => {
          if (onlyEnv && r.source !== "env") return false;
          if (!q) return true;
          return r.name.toLowerCase().includes(q);
        }),
      }))
      .filter((g: Group) => g.items.length > 0);
  }, [data, query, onlyEnv]);

  const shown = groups.reduce((n, g) => n + g.items.length, 0);

  return (
    <AppShell>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <FileCode2 className="w-6 h-6 text-emerald-400" />
            Конфигурация
          </h1>
          <p className="text-sm text-neutral-400 mt-1">
            Что действует сейчас и откуда взято. Только чтение — пороги правятся коммитом.
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-sm"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
          Обновить
        </button>
      </div>

      {data && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
            <Card label="Всего параметров" value={data.total} />
            <Card
              label="Из env (blueprint)"
              value={data.from_env}
              hint="перекрыто render.yaml — правка config.py не подействует"
              accent="text-sky-400"
            />
            <Card
              label="Из дефолта кода"
              value={data.from_default}
              hint="значение берётся из config.py"
              accent="text-neutral-200"
            />
          </div>

          {data.pinned_env_keys?.length > 0 && (
            <div className="mb-6 space-y-3">
              <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 p-4">
                <div className="text-sm font-medium text-neutral-200">
                  Совпадает с дефолтом кода: {data.pinned_env_keys.length}
                </div>
                <p className="text-xs text-neutral-400 mt-1">
                  Это <b>не</b> автоматически мусор. Запись в blueprint при совпадающем
                  значении — <b>закрепление</b>: поменяется дефолт в config.py, а прод
                  останется прежним. Удалять можно только то, что закреплять незачем.
                </p>
              </div>

              {data.protected_env_keys?.length > 0 && (
                <div className="rounded-xl border border-emerald-800/40 bg-emerald-950/20 p-4">
                  <div className="text-sm font-medium text-emerald-300">
                    Закреплено намеренно — не удалять ({data.protected_env_keys.length})
                  </div>
                  <p className="text-xs text-neutral-400 mt-1">
                    Выключатели реальных денег, аварийные стопы, лимиты убытка и гейты
                    течи капитала. Часть из них закреплена тестом блупринта — удаление
                    уронит сборку.
                  </p>
                  <div className="text-xs text-neutral-500 mt-2 font-mono break-all">
                    {data.protected_env_keys.join(", ")}
                  </div>
                </div>
              )}

              {data.removable_env_keys?.length > 0 && (
                <div className="rounded-xl border border-amber-700/40 bg-amber-950/20 p-4">
                  <div className="flex items-center gap-2 text-amber-300 text-sm font-medium">
                    <AlertTriangle className="w-4 h-4" />
                    Можно вычистить из render.yaml ({data.removable_env_keys.length})
                  </div>
                  <p className="text-xs text-neutral-400 mt-1">
                    Совпадает с дефолтом и закреплять незачем — ни выключатель, ни лимит.
                    Удаление ничего не меняет в поведении, только уменьшает шум.
                  </p>
                  <div className="text-xs text-neutral-500 mt-2 font-mono break-all">
                    {data.removable_env_keys.join(", ")}
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3 mb-4">
            <div className="relative flex-1 min-w-[220px]">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Поиск по имени, например TZ_ или GRID"
                className="w-full pl-9 pr-3 py-2 rounded-lg bg-neutral-900 border border-neutral-800 text-sm"
              />
            </div>
            <label className="flex items-center gap-2 text-sm text-neutral-300 cursor-pointer">
              <input
                type="checkbox"
                checked={onlyEnv}
                onChange={(e) => setOnlyEnv(e.target.checked)}
                className="accent-emerald-500"
              />
              только перекрытые env
            </label>
            <span className="text-xs text-neutral-500">показано: {shown}</span>
          </div>

          <div className="space-y-6">
            {groups.map((g) => (
              <section key={g.name} className="rounded-xl border border-neutral-800 overflow-hidden">
                <header className="px-4 py-2 bg-neutral-900/70 text-sm font-medium text-neutral-200">
                  {g.name}
                  <span className="text-neutral-500 font-normal"> · {g.items.length}</span>
                </header>
                <div className="divide-y divide-neutral-900">
                  {g.items.map((r) => (
                    <ConfigRow key={r.name} row={r} />
                  ))}
                </div>
              </section>
            ))}
          </div>
        </>
      )}
    </AppShell>
  );
}

function Card({
  label,
  value,
  hint,
  accent = "text-neutral-100",
}: {
  label: string;
  value: any;
  hint?: string;
  accent?: string;
}) {
  return (
    <div className="rounded-xl border border-neutral-800 p-4">
      <div className="text-xs text-neutral-500">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${accent}`}>{value}</div>
      {hint && <div className="text-xs text-neutral-500 mt-1">{hint}</div>}
    </div>
  );
}

function ConfigRow({ row }: { row: Row }) {
  const overridden =
    row.source === "env" && !row.secret && !row.redundant &&
    JSON.stringify(row.value) !== JSON.stringify(row.default);

  return (
    <div className="px-4 py-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm hover:bg-neutral-900/40">
      <span className="font-mono text-neutral-300 flex items-center gap-1.5">
        {row.secret && <Lock className="w-3 h-3 text-amber-400" />}
        {row.name}
      </span>

      <span className="ml-auto flex items-center gap-3">
        {/* Дефолт показываем только когда env его реально перебил — иначе шум */}
        {overridden && (
          <span className="text-xs text-neutral-600 line-through font-mono">
            {String(row.default)}
          </span>
        )}
        <span
          className={`font-mono ${
            row.secret ? "text-amber-400" : overridden ? "text-sky-300" : "text-neutral-100"
          }`}
        >
          {String(row.value)}
        </span>
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded flex items-center gap-1 ${
            row.source === "env"
              ? "bg-sky-950 text-sky-300 border border-sky-900"
              : "bg-neutral-900 text-neutral-500 border border-neutral-800"
          }`}
          title={
            row.source === "env"
              ? "значение из render.yaml → env; правка config.py не подействует"
              : "значение из дефолта config.py"
          }
        >
          {row.source === "env" ? <Server className="w-2.5 h-2.5" /> : <FileCode2 className="w-2.5 h-2.5" />}
          {row.source}
        </span>
      </span>
    </div>
  );
}
