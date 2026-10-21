"""Экономический критерий в MLOutcomeStatsService.shadow_report (то, что реально отдаёт /ml/shadow-report).

Прошлый патч (patch_ml_shadow_economic.py) правил services/ml_shadow_report.py — модуль, который
эндпоинт НЕ вызывает: роутер зовёт MLOutcomeStatsService().shadow_report() из ml_outcome_stats.py.
Здесь те же поля, но в нужном месте. page.tsx менять не нужно.

Добавляет в ответ: economic (верх/низ по медиане score), threshold_impact.taken_mean_net_usdt /
skipped_mean_net_usdt / diff_mean_net_usdt / diff_ci95, by_third (AUC по третям выборки).
Вердикт edge_visible теперь требует и AUC >= 0.60, и нижнюю границу CI разницы верх-низ > 0;
если AUC >= 0.60, а CI не выше нуля — weak_signal. ml_gate_benefit_usdt сохранён.

Запуск из корня репозитория: python scripts/patch_outcome_stats_economic.py
Идемпотентен, делает .bak-before-economic2, UTF-8, переводы строк сохраняются.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
PY = ROOT / "apps/api/services/ml_outcome_stats.py"

raw = PY.read_bytes().decode("utf-8")
crlf = "\r\n" in raw
t = raw.replace("\r\n", "\n")


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] якорь найден {n} раз(а), ожидался 1 - файл не менялся")
    return text.replace(old, new)


if "_split_diff" in t:
    print("ml_outcome_stats: уже применено")
    sys.exit(0)

t = once(t, "import json\nimport math\nimport threading\n",
         "import json\nimport math\nimport random\nimport statistics\nimport threading\n", "imports")

helper = '''def _split_diff(nets: list[float], flags: list[bool], block: int = 20,
                n_boot: int = 500, seed: int = 42) -> dict | None:
    """Разница среднего net: flags=True ("верх") минус остальные, с 95% CI.

    (#economic-criterion-2026-10-21) nets — в хронологическом порядке (порядок файла);
    bootstrap блочный (блок `block` сделок подряд): соседние сделки коррелируют.
    None — одна из сторон короче 5 сделок, судить не о чем.
    """
    top = [v for v, f in zip(nets, flags) if f]
    bot = [v for v, f in zip(nets, flags) if not f]
    if len(top) < 5 or len(bot) < 5:
        return None
    est = sum(top) / len(top) - sum(bot) / len(bot)
    m = len(nets)
    nb = max(1, -(-m // block))
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        idx: list[int] = []
        for _b in range(nb):
            s = rng.randrange(0, max(m - block, 0) + 1)
            idx.extend(range(s, min(s + block, m)))
        idx = idx[:m]
        h = [nets[i] for i in idx if flags[i]]
        lo = [nets[i] for i in idx if not flags[i]]
        if h and lo:
            boots.append(sum(h) / len(h) - sum(lo) / len(lo))
    ci = None
    if len(boots) >= 50:
        boots.sort()
        ci = [round(boots[int(0.025 * len(boots))], 4),
              round(boots[max(int(0.975 * len(boots)) - 1, 0)], 4)]
    return {
        "top_n": len(top), "bottom_n": len(bot),
        "top_mean_net": round(sum(top) / len(top), 4),
        "bottom_mean_net": round(sum(bot) / len(bot), 4),
        "diff_mean_net": round(est, 4),
        "ci95": ci,
    }


class MLOutcomeStatsService:
'''
t = once(t, "class MLOutcomeStatsService:\n", helper, "helper")

block = '''        # ── экономический критерий (#economic-criterion-2026-10-21) ────────────────
        # Сумма net отрезанных (ml_gate_benefit_usdt) положительна у ЛЮБОЙ убыточной книги:
        # фильтр, убирающий сделки, "улучшает" итог без всякого края. Честный вопрос:
        # у отрезанных средний net хуже, чем у оставленных, и разница не в пределах шума?
        # Порог не подбирается: верх/низ делит медиана score.
        med = statistics.median(y_pred)
        econ = _split_diff(nets, [s >= med for s in y_pred])
        economic = (
            {"split": "median_score", "median_score": round(med, 4), **econ}
            if econ else {"split": "median_score", "median_score": round(med, 4), "insufficient": True}
        )
        thr_diff = _split_diff(nets, [s >= threshold for s in y_pred])
        if thr_diff:
            threshold_impact.update({
                "taken_mean_net_usdt": thr_diff["top_mean_net"],
                "skipped_mean_net_usdt": thr_diff["bottom_mean_net"],
                "diff_mean_net_usdt": thr_diff["diff_mean_net"],
                "diff_ci95": thr_diff["ci95"],
            })
        econ_ok = bool(econ and econ["ci95"] and econ["ci95"][0] > 0)

        # AUC по третям выборки (деление без параметров): виден дрейф версий модели.
        by_third = []
        third = max(len(y_pred) // 3, 1)
        bounds = ((0, third), (third, 2 * third), (2 * third, len(y_pred)))
        for k, (a, b) in enumerate(bounds):
            idx = [i for i in range(a, b) if train_labels[i] is not None]
            yt = [train_labels[i] for i in idx]
            by_third.append({
                "part": ("старые", "средние", "новые")[k],
                "n": len(idx),
                "auc_vs_train_label": (
                    self._safe_auc(yt, [y_pred[i] for i in idx]) if len(set(yt)) >= 2 else None
                ),
            })

        # Вердикт — по AUC против цели обучения; если строк с меткой мало,'''
t = once(t, "        # Вердикт — по AUC против цели обучения; если строк с меткой мало,", block, "block")

t = once(t, '            if verdict_auc >= 0.60:\n                verdict = "edge_visible"',
         '            if verdict_auc >= 0.60 and econ_ok:\n                verdict = "edge_visible"', "verdict")
t = once(t, '            "threshold_impact": threshold_impact,\n            "verdict": verdict,',
         '            "threshold_impact": threshold_impact,\n            "economic": economic,\n            "by_third": by_third,\n            "verdict": verdict,',
         "return")

PY.with_name(PY.name + ".bak-before-economic2").write_bytes(PY.read_bytes())
PY.write_bytes((t.replace("\n", "\r\n") if crlf else t).encode("utf-8"))
print("ml_outcome_stats: ok")
