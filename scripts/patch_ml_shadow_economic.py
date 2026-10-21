"""Экономический критерий в shadow-отчёте ML + честная плитка на странице /ml.

Что меняет
----------
ml_shadow_report.py
  * в ответ добавляются:
      - economic: сделки со score >= медианы ("верх") против остальных ("низ"),
        разница среднего net на сделку и 95% CI (блочный bootstrap, блок 20,
        seed 42, хронологический порядок). Порог не подбирается;
      - threshold_impact.taken_mean_net_usdt / skipped_mean_net_usdt /
        diff_mean_net_usdt / diff_ci95 — то же для порога ML_MIN_SCORE_TO_TRADE;
      - by_third: AUC против метки обучения по третям выборки (старые -> новые).
        Это не подбор: деление на три равные части без параметров. Показывает,
        не тянут ли вниз общий AUC устаревшие версии модели.
  * verdict edge_visible теперь требует нижнюю границу CI экономической разницы > 0
    вместо ml_gate_benefit_usdt > 0 (она равна -skipped_net и положительна у любой
    убыточной книги). Поле ml_gate_benefit_usdt сохранено для совместимости.
page.tsx
  * плитка "Выгода гейта" больше не зелёная по знаку суммы: показываем средний net
    взятых/отрезанных и разницу с CI; зелёное только если нижняя граница CI > 0;
  * блок "Верх против низа" и AUC по третям.

Запуск из корня репозитория:  python scripts/patch_ml_shadow_economic.py
Идемпотентен. Делает .bak-before-economic рядом с файлами. UTF-8, переводы строк сохраняются.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
PY = ROOT / "apps/api/services/ml_shadow_report.py"
TSX = ROOT / "apps/web/app/ml/page.tsx"


def load(p):
    raw = p.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def save(p, text, crlf):
    p.with_name(p.name + ".bak-before-economic").write_bytes(p.read_bytes())
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] якорь найден {n} раз(а), ожидался 1 - файл не менялся")
    return text.replace(old, new)


# ─────────────────────────── ml_shadow_report.py ───────────────────────────
t, crlf = load(PY)
if "_split_diff" in t:
    print("ml_shadow_report: уже применено")
else:
    t = once(t, "import json\nfrom typing import Any\n",
             "import json\nimport random\nimport statistics\nfrom typing import Any\n", "py-imports")

    helper = '''

def _split_diff(nets: list[float], flags: list[bool], block: int = 20,
                n_boot: int = 500, seed: int = 42) -> dict | None:
    """Разница среднего net: flags=True ("верх") минус остальные, с 95% CI.

    (#economic-criterion-2026-10-21) nets — в ХРОНОЛОГИЧЕСКОМ порядке; bootstrap
    блочный (блок `block` сделок подряд), потому что соседние сделки коррелируют.
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
        l = [nets[i] for i in idx if not flags[i]]
        if h and l:
            boots.append(sum(h) / len(h) - sum(l) / len(l))
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
'''
    t = once(t, "    return {}\n\n\ndef build(", "    return {}\n" + helper + "\n\ndef build(", "py-helper")

    block = '''
    # ── экономический критерий (#economic-criterion-2026-10-21) ────────────────
    # Сумма net отрезанных сделок (ml_gate_benefit_usdt) положительна у ЛЮБОЙ
    # убыточной книги: фильтр, убирающий сделки, "улучшает" итог без всякого края.
    # Честный вопрос другой: у отрезанных сделок средний net ХУЖЕ, чем у оставленных,
    # и разница не в пределах шума? Порог тут не подбирается: верх/низ делит медиана.
    chrono = sorted(rows, key=lambda r: r["id"])
    nets_chrono = [r["net"] for r in chrono]
    med = statistics.median(r["score"] for r in chrono)
    econ = _split_diff(nets_chrono, [r["score"] >= med for r in chrono])
    economic = {
        "split": "median_score", "median_score": round(med, 4), **(econ or {}),
    } if econ else {"split": "median_score", "median_score": round(med, 4), "insufficient": True}
    thr_diff = _split_diff(nets_chrono, [r["score"] >= thr for r in chrono])
    if thr_diff:
        threshold_impact.update({
            "taken_mean_net_usdt": thr_diff["top_mean_net"],
            "skipped_mean_net_usdt": thr_diff["bottom_mean_net"],
            "diff_mean_net_usdt": thr_diff["diff_mean_net"],
            "diff_ci95": thr_diff["ci95"],
        })
    econ_ok = bool(econ and econ["ci95"] and econ["ci95"][0] > 0)

    # AUC по третям выборки: деление без параметров, показывает дрейф версий модели.
    by_third = []
    third = max(len(chrono) // 3, 1)
    for k, (a, b) in enumerate(((0, third), (third, 2 * third), (2 * third, len(chrono)))):
        part = [r for r in chrono[a:b] if r["train_label"] is not None]
        by_third.append({
            "part": ("старые", "средние", "новые")[k],
            "n": len(part),
            "auc_vs_train_label": _auc(
                [r["score"] for r in part if r["train_label"] == 1],
                [r["score"] for r in part if r["train_label"] == 0],
            ),
        })

    base_winrate = round(len(wins) / n * 100, 1)'''
    t = once(t, "\n    base_winrate = round(len(wins) / n * 100, 1)", block, "py-block")

    t = once(t, 'and (taken_wr or 0) > base_winrate and threshold_impact["ml_gate_benefit_usdt"] > 0:',
             'and (taken_wr or 0) > base_winrate and econ_ok:', "py-verdict")

    t = once(t, '        "threshold_impact": threshold_impact,\n',
             '        "threshold_impact": threshold_impact,\n        "economic": economic,\n        "by_third": by_third,\n',
             "py-return")
    save(PY, t, crlf)
    print("ml_shadow_report: ok")

# ───────────────────────────────── page.tsx ─────────────────────────────────
t, crlf = load(TSX)
if "shadow.economic" in t:
    print("page.tsx: уже применено")
else:
    old_row = (
        '                <Row k="Выгода гейта" v={`${shadow.threshold_impact?.ml_gate_benefit_usdt} USDT`} '
        'highlight={(shadow.threshold_impact?.ml_gate_benefit_usdt ?? 0) > 0} />\n'
    )
    new_rows = (
        '                <Row k="Net на сделку: взято / отрезано" v={`${shadow.threshold_impact?.taken_mean_net_usdt ?? "—"} / ${shadow.threshold_impact?.skipped_mean_net_usdt ?? "—"}`} />\n'
        '                <Row k="Разница, USDT (95% CI)" v={shadow.threshold_impact?.diff_ci95 ? `${shadow.threshold_impact.diff_mean_net_usdt} [${shadow.threshold_impact.diff_ci95[0]}; ${shadow.threshold_impact.diff_ci95[1]}]` : "мало данных"} highlight={(shadow.threshold_impact?.diff_ci95?.[0] ?? -1) > 0} />\n'
        '                <Row k="Сумма net отрезанных" v={`${shadow.threshold_impact?.skipped_net_usdt} USDT`} />\n'
    )
    t = once(t, old_row, new_rows, "tsx-row")

    anchor = "            </div>\n          </div>\n        ) : (\n"
    new_block = (
        "            </div>\n"
        "\n"
        "            {/* (#economic-criterion-2026-10-21) Сумма net отрезанных положительна у любой убыточной\n"
        "                книги, поэтому здесь — сравнение верха и низа по score и интервал разницы. */}\n"
        '            <div className="rounded-xl border border-cyan-900/50 bg-black/30 p-3 text-xs">\n'
        '              <div className="mb-1 font-semibold text-cyan-200">Верхняя половина score против нижней (net на сделку)</div>\n'
        "              {shadow.economic?.insufficient ? (\n"
        '                <div className="text-cyan-100/60">Мало сделок для сравнения.</div>\n'
        "              ) : (\n"
        '                <div className="grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-3">\n'
        '                  <Row k={`Верх (n=${shadow.economic?.top_n})`} v={shadow.economic?.top_mean_net ?? "—"} />\n'
        '                  <Row k={`Низ (n=${shadow.economic?.bottom_n})`} v={shadow.economic?.bottom_mean_net ?? "—"} />\n'
        '                  <Row k="Разница (95% CI)" v={shadow.economic?.ci95 ? `${shadow.economic.diff_mean_net} [${shadow.economic.ci95[0]}; ${shadow.economic.ci95[1]}]` : "—"} highlight={(shadow.economic?.ci95?.[0] ?? -1) > 0} />\n'
        "                </div>\n"
        "              )}\n"
        '              <p className="mt-2 text-[11px] text-cyan-100/40">\n'
        "                Зелёное только если нижняя граница интервала выше нуля. Делит медиана score, порог не подбирается.\n"
        "              </p>\n"
        "              {Array.isArray(shadow.by_third) && shadow.by_third.length > 0 && (\n"
        '                <div className="mt-2 text-cyan-100/70">\n'
        "                  AUC по третям выборки (старые → новые):{\" \"}\n"
        "                  {shadow.by_third.map((p: any) => `${p.part}: ${p.auc_vs_train_label ?? \"—\"} (n=${p.n})`).join(\" · \")}\n"
        "                </div>\n"
        "              )}\n"
        "            </div>\n"
        "          </div>\n"
        "        ) : (\n"
    )
    t = once(t, anchor, new_block, "tsx-block")
    save(TSX, t, crlf)
    print("page.tsx: ok")
