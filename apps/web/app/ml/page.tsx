"use client";

import { useEffect, useState } from "react";
import { RefreshCw, Brain, Play } from "lucide-react";
import AppShell from "../../components/AppShell";
import { apiGet, apiPost } from "../../lib/api";

const MODE_COLOR: Record<string, string> = {
  off: "bg-slate-700 text-white",
  shadow: "bg-cyan-700 text-white",
  advisory: "bg-yellow-500 text-black",
  full_auto: "bg-emerald-500 text-black",
};

const MODE_HINT: Record<string, string> = {
  off: "ML выключен — система работает по правилам (default). Запуск в live не затронут.",
  shadow: "ML считает ml_score и логирует, но НЕ влияет на сделки. Наблюдаем прогноз vs реальность.",
  advisory: "ML рекомендует (take/skip), решение остаётся за правилами/человеком.",
  full_auto: "ML гейтит и масштабирует сделки в пределах guardrails.",
};

export default function MLPage() {
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [training, setTraining] = useState(false);
  const [trainResult, setTrainResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      setStatus(await apiGet("/ml/status"));
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }

  async function train() {
    setTraining(true);
    setTrainResult(null);
    try {
      setTrainResult(await apiPost("/ml/train"));
      await load();
    } catch (e: any) {
      setTrainResult({ status: "error", error: String(e?.message || e) });
    } finally {
      setTraining(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const mode = String(status?.ml_mode || "off");
  const model = status?.model || {};
  const metrics = model?.metrics || {};
  const ready = !!model?.model_exists;

  return (
    <AppShell>
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 text-2xl font-bold text-emerald-50">
          <Brain className="h-6 w-6 text-emerald-400" /> ML-слой
        </h1>
        <button
          onClick={load}
          className="flex items-center gap-2 rounded-2xl bg-emerald-900/60 px-4 py-2 text-sm font-semibold text-emerald-100 hover:bg-emerald-800"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /> Обновить
        </button>
      </div>

      {err && (
        <div className="mt-4 rounded-2xl border border-red-800 bg-red-950/40 p-4 text-sm text-red-200">
          {err}
        </div>
      )}

      <div className="mt-6 grid gap-4 md:grid-cols-2">
        {/* Режим */}
        <div className="rounded-3xl border border-emerald-900/60 bg-slate-950/60 p-5">
          <div className="text-xs uppercase tracking-wide text-emerald-100/50">Режим (ML_MODE)</div>
          <div className="mt-2">
            <span className={`rounded-xl px-3 py-1 text-sm font-bold ${MODE_COLOR[mode] || "bg-slate-700 text-white"}`}>
              {mode}
            </span>
          </div>
          <p className="mt-3 text-sm text-emerald-100/70">{MODE_HINT[mode] || ""}</p>
          <p className="mt-3 text-xs text-emerald-100/40">
            Смена режима — через env <code>ML_MODE</code> (off / shadow / advisory / full_auto). Дефолт{" "}
            <b>off</b> не влияет на торговлю и live.
          </p>
        </div>

        {/* Модель */}
        <div className="rounded-3xl border border-emerald-900/60 bg-slate-950/60 p-5">
          <div className="flex items-center justify-between">
            <div className="text-xs uppercase tracking-wide text-emerald-100/50">Мета-лейблер</div>
            <span className={`rounded-lg px-2 py-1 text-xs font-semibold ${ready ? "bg-emerald-700 text-white" : "bg-orange-700 text-white"}`}>
              {ready ? "готова" : "не обучена"}
            </span>
          </div>
          <dl className="mt-3 space-y-1 text-sm">
            <Row k="Обучена" v={model?.trained_at ? String(model.trained_at).slice(0, 19).replace("T", " ") : "—"} />
            <Row k="Сделок в обучении" v={model?.samples ?? "—"} />
            <Row k="Нужно минимум" v={model?.min_train_samples ?? "—"} />
            <Row k="Winrate выборки" v={model?.win_rate != null ? `${model.win_rate}%` : "—"} />
            <Row k="Валидация AUC" v={metrics?.val_auc ?? "—"} highlight={metrics?.val_auc != null && metrics.val_auc > 0.6} />
            <Row k="Валидация Acc" v={metrics?.val_acc ?? "—"} />
            <Row k="Метка" v={model?.label_kind ?? "—"} />
          </dl>
          <button
            onClick={train}
            disabled={training}
            className="mt-4 flex items-center gap-2 rounded-2xl bg-emerald-500 px-4 py-2 text-sm font-bold text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
          >
            <Play className={`h-4 w-4 ${training ? "animate-pulse" : ""}`} /> {training ? "Обучаю…" : "Обучить сейчас"}
          </button>
        </div>
      </div>

      {/* Guardrails / параметры */}
      <div className="mt-4 rounded-3xl border border-emerald-900/60 bg-slate-950/60 p-5">
        <div className="text-xs uppercase tracking-wide text-emerald-100/50">Guardrails (full_auto)</div>
        <div className="mt-2 grid grid-cols-2 gap-2 text-sm md:grid-cols-3">
          <Row k="Мин. ml_score для входа" v={status?.min_score_to_trade ?? "—"} />
          <Row k="Множитель размера" v={status?.size_mult_range ? `${status.size_mult_range[0]}–${status.size_mult_range[1]}×` : "—"} />
        </div>
      </div>

      {trainResult && (
        <div className="mt-4 rounded-3xl border border-emerald-900/60 bg-slate-950/60 p-5">
          <div className="text-xs uppercase tracking-wide text-emerald-100/50">Результат обучения</div>
          <pre className="mt-2 overflow-x-auto rounded-xl bg-black/40 p-3 text-xs text-emerald-100/80">
            {JSON.stringify(trainResult, null, 2)}
          </pre>
        </div>
      )}

      <p className="mt-6 text-xs text-emerald-100/40">
        Путь включения: копится датасет → при ≥ минимума «Обучить» → <code>ML_MODE=shadow</code> (видно ml_score рядом с
        сигналами, без влияния) → обгоняет правила → <code>advisory</code> → <code>full_auto</code>. Авто-retrain — раз в сутки.
      </p>
    </AppShell>
  );
}

function Row({ k, v, highlight }: { k: string; v: any; highlight?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-emerald-100/50">{k}</span>
      <span className={highlight ? "font-bold text-emerald-300" : "text-emerald-50"}>{String(v)}</span>
    </div>
  );
}
