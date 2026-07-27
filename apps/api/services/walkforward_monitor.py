"""Регулярный walk-forward по режимам (#walk-forward-2026-07-27).

Зачем регулярно, а не по кнопке. Одиночный прогон отвечает «что лучше сейчас».
Ряд прогонов отвечает на более важное: **держится ли оптимум во времени**. Если
из недели в неделю подбор выбирает разные параметры, значит устойчивого оптимума
нет — есть шум, и любая правка конфига по такому результату будет подгонкой под
последний кусок истории.

Журнал компактный: по одной строке на прогон на режим. Из него видно дрейф —
момент, когда рынок реально сменил характер, отличается от обычного разброса
тем, что новый выбор ЗАКРЕПЛЯЕТСЯ на нескольких прогонах подряд.

Только чтение датасета. На торговлю не влияет.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.config import settings

REGIMES = ("trend", "range", "scalp")


def _path() -> Path:
    return Path(
        str(getattr(settings, "WALKFORWARD_LOG_PATH", "") or "storage/ml/walkforward.jsonl")
    )


def run_once(folds: int | None = None, limit: int = 2000) -> dict[str, Any]:
    """Один прогон по всем режимам. БЛОКИРУЮЩАЯ функция — звать через to_thread."""
    from services.exit_replay import walk_forward

    folds = int(folds or getattr(settings, "WALKFORWARD_FOLDS", 4))
    out: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "ts": time.time(), "regimes": {}}
    for regime in REGIMES:
        try:
            out["regimes"][regime] = walk_forward(regime=regime, folds=folds, limit=limit)
        except Exception as exc:  # noqa: BLE001 — один режим не должен ронять прогон
            out["regimes"][regime] = {"status": "error", "regime": regime, "error": str(exc)}
    return out


def log_snapshot(snapshot: dict[str, Any]) -> None:
    """Компактная строка: только то, по чему потом судим о дрейфе."""
    row: dict[str, Any] = {"ts": round(snapshot["ts"], 1), "at": snapshot["at"], "r": {}}
    for regime, res in (snapshot.get("regimes") or {}).items():
        if res.get("status") != "ok":
            row["r"][regime] = {"status": res.get("status"), "trades": res.get("trades", 0)}
            continue
        scored = [s for s in (res.get("steps") or []) if not s.get("skipped")]
        row["r"][regime] = {
            "status": "ok",
            "trades": res.get("trades"),
            "edge": res.get("oos_edge_pct"),
            "won": res.get("folds_won"),
            "scored": res.get("folds_scored"),
            "uniq": res.get("unique_param_picks"),
            # Параметры ПОСЛЕДНЕГО фолда — самый свежий выбор оптимизатора.
            # По их изменению от прогона к прогону и виден дрейф.
            "last_pick": scored[-1]["picked_params"] if scored else None,
        }

    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — журнал не должен ронять воркер
        pass


def history(limit: int = 60) -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"status": "no_data", "note": "прогонов ещё не было"}

    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001 — битая строка не валит историю
                    continue
    except Exception:  # noqa: BLE001
        return {"status": "no_data", "note": "журнал недоступен"}

    rows = rows[-int(limit):]
    if not rows:
        return {"status": "no_data", "note": "журнал пуст"}

    # Стабильность выбора по режимам: сколько РАЗНЫХ наборов параметров
    # оптимизатор выбрал за всю историю прогонов. Единица — оптимум стоит на
    # месте; число, близкое к числу прогонов, — устойчивого оптимума нет.
    stability: dict[str, Any] = {}
    for regime in REGIMES:
        picks = [
            json.dumps(r["r"][regime]["last_pick"], sort_keys=True, ensure_ascii=False)
            for r in rows
            if (r.get("r") or {}).get(regime, {}).get("last_pick")
        ]
        edges = [
            r["r"][regime]["edge"]
            for r in rows
            if (r.get("r") or {}).get(regime, {}).get("edge") is not None
        ]
        if not picks:
            stability[regime] = {"runs": 0, "verdict": "прогонов с результатом нет"}
            continue
        uniq = len(set(picks))
        share = uniq / len(picks)
        stability[regime] = {
            "runs": len(picks),
            "distinct_picks": uniq,
            "most_common_share_pct": round(
                max(picks.count(p) for p in set(picks)) / len(picks) * 100, 1
            ),
            "avg_oos_edge_pct": round(sum(edges) / len(edges), 4) if edges else None,
            "positive_edge_runs": sum(1 for e in edges if e > 0),
            "verdict": (
                "оптимум стоит на месте — выбор воспроизводится"
                if share <= 0.34
                else "оптимум плавает — устойчивого выбора нет, правки будут подгонкой"
                if share >= 0.75
                else "выбор частично воспроизводится — нужны ещё прогоны"
            ),
        }

    return {
        "status": "ok",
        "runs": len(rows),
        "first_at": rows[0].get("at"),
        "last_at": rows[-1].get("at"),
        "stability": stability,
        "rows": rows,
        "note": (
            "Ряд прогонов отвечает на вопрос, на который одиночный ответить не может: "
            "держится ли оптимум во времени. distinct_picks близкое к runs означает, "
            "что оптимизатор каждый раз выбирает новое — это шум, а не находка. "
            "Настоящая смена режима рынка отличается тем, что новый выбор "
            "ЗАКРЕПЛЯЕТСЯ на нескольких прогонах подряд."
        ),
    }
