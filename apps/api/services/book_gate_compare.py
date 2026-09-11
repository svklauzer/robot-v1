"""Решения гейта стакана на двух книгах (#okx-gate-compare-2026-09-12).

Сравнение медиан 12.09 (рабочая HTX против тени OKX) показало: книги разные.
Спред у OKX втрое уже, thinness на обеих далеко под порогом, а стенки — главное
расхождение: бид/аск 0.49/0.43 на HTX против 0.30/0.22 на OKX при пороге
подтверждения 0.30, OBI −0.12 против −0.01.

Гейт стакана пропускает лонг, если OBI ≥ obi_confirm ИЛИ бид-стенка ≥
wall_confirm (при не слишком встречном OBI); шорт — зеркально. На HTX лонги
проходят через стенки, на OKX стенки ниже порога — гейт на OKX блокировал бы
чаще. Медианы этого не показывают: нужно, как часто гейт пропускает вход.

Здесь раз в `OB_COMPARE_SAMPLE_SEC` снимаются метрики обеих книг по каждому
символу и книжная часть решения гейта (спред, жёсткое вето OBI, подтверждение
OBI или стенкой; CVD исключён — тень OKX ленты сделок не берёт) — для обоих
профилей: position (тренд/CRT: OBI ≥ 0.05 или стенка ≥ 0.20, вето 0.80) и
scalp (0.15 / 0.30 / 0.45). Пороги 0.15/0.30 — только у скальпа; тренд, где
большинство сделок, мягче, и расхождение книг бьёт по нему слабее.

По выборке — доля пропусков по профилям, распределения и подобранные для OKX
пороги стенки и OBI, при которых доля пропуска каждого условия совпала бы с HTX
(квантильное совпадение). Решения по этим порогам не
принимаются — это основание для настроек OKX при переключении фида.
"""
from __future__ import annotations

import time
from collections import deque

from core.config import settings

# (метка книги, символ) → deque[(ts, obi, bid_wall, ask_wall, spread,
#                                 {профиль: (long_ok, short_ok)})]
SAMPLES: dict[tuple[str, str], deque] = {}
PROFILES: tuple[str, ...] = ("position", "scalp")
_REGIME_OF = {"position": "trend_up_candidate", "scalp": "scalp"}


def _maxlen() -> int:
    return max(int(getattr(settings, "OB_COMPARE_SAMPLES", 2880) or 2880), 10)


def gate_params(profile: str = "position") -> dict:
    """Пороги профиля, как в цикле робота; CVD выключен — у тени нет ленты."""
    from services.depth_profiles import depth_gate_params

    params = depth_gate_params(_REGIME_OF[profile])
    # Спред-кап цикл выбирает сам: позиционный — OB_POSITION_MAX_SPREAD_PCT.
    params["max_spread_pct"] = float(getattr(
        settings, "OB_POSITION_MAX_SPREAD_PCT" if profile == "position" else "OB_MAX_SPREAD_PCT",
        0.20 if profile == "position" else 0.08))
    params["cvd_block_ratio"] = 0.0
    params["cvd_thin_ratio"] = 0.0
    params.pop("profile", None)
    return params


def sample(label: str, store, now: float | None = None) -> int:
    """Снимок всех символов одной книги в выборку. Возвращает число символов."""
    from services.orderbook_analyzer import OrderBookAnalyzer

    now = time.time() if now is None else now
    levels = int(getattr(settings, "OB_DEPTH_LEVELS", 10))
    max_age = float(getattr(settings, "OB_DATA_MAX_AGE_SEC", 15.0))
    params = {prof: gate_params(prof) for prof in PROFILES}
    count = 0
    for sym in store.stats().get("symbols", []):
        snap = store.snapshot(sym)
        if not snap or float(snap.get("age_sec") or 1e9) > max_age:
            continue
        sig = OrderBookAnalyzer.analyze(snap, levels=levels)
        if not sig.fresh:
            continue
        verdicts = {
            prof: (OrderBookAnalyzer.entry_gate("long", sig, **params[prof])[0],
                   OrderBookAnalyzer.entry_gate("short", sig, **params[prof])[0])
            for prof in PROFILES
        }
        buf = SAMPLES.setdefault((label, sym), deque(maxlen=_maxlen()))
        buf.append((now, sig.obi, sig.bid_wall_share, sig.ask_wall_share,
                    sig.spread_pct, verdicts))
        count += 1
    return count


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


def _rows(label: str) -> list[tuple]:
    return [row for (lab, _), buf in SAMPLES.items() if lab == label for row in buf]


def _share(rows: list[tuple], profile: str, side: int) -> float | None:
    got = [r[5][profile][side] for r in rows if profile in r[5]]
    return round(sum(1 for v in got if v) / len(got), 4) if got else None


def _matched_threshold(ref: list[float], ref_threshold: float, target: list[float]) -> float | None:
    """Порог на target с той же долей «≥ порога», что у ref на ref_threshold."""
    if not ref or not target:
        return None
    pass_rate = sum(1 for v in ref if v >= ref_threshold) / len(ref)
    return _quantile(target, 1.0 - pass_rate)


def summary(primary: str = "primary", shadow: str = "shadow") -> dict:
    ref, tgt = _rows(primary), _rows(shadow)
    books = {}
    for label, rows in ((primary, ref), (shadow, tgt)):
        obi = [r[1] for r in rows]
        walls = [r[2] for r in rows] + [r[3] for r in rows]
        books[label] = {
            "samples": len(rows),
            "pass_rate": {prof: {"long": _share(rows, prof, 0), "short": _share(rows, prof, 1)}
                          for prof in PROFILES},
            "obi_p25_p50_p75": [_r(_quantile(obi, q)) for q in (0.25, 0.5, 0.75)],
            "wall_p25_p50_p75": [_r(_quantile(walls, q)) for q in (0.25, 0.5, 0.75)],
            "spread_p50": _r(_quantile([r[4] for r in rows if r[4] is not None], 0.5)),
        }

    ref_walls = [r[2] for r in ref] + [r[3] for r in ref]
    tgt_walls = [r[2] for r in tgt] + [r[3] for r in tgt]
    # OBI в сторону сделки: для лонга — obi, для шорта — −obi.
    ref_obi = [r[1] for r in ref] + [-r[1] for r in ref]
    tgt_obi = [r[1] for r in tgt] + [-r[1] for r in tgt]
    thresholds, matched = {}, {}
    for prof in PROFILES:
        params = gate_params(prof)
        thresholds[prof] = {k: params[k] for k in ("obi_confirm", "wall_confirm",
                                                   "obi_hard_veto", "max_spread_pct")}
        matched[prof] = {
            "wall_confirm": _r(_matched_threshold(ref_walls, params["wall_confirm"], tgt_walls)),
            "obi_confirm": _r(_matched_threshold(ref_obi, params["obi_confirm"], tgt_obi)),
        }
    return {
        "thresholds": thresholds,
        "books": books,
        "matched_for_shadow": matched,
        "note": ("pass_rate — доля выборок, где книжная часть гейта пропустила бы "
                 "вход (CVD исключён: у тени нет ленты сделок). matched_for_shadow — "
                 "пороги для тени, при которых доля «стенка ≥ порога» и «OBI в "
                 "сторону сделки ≥ порога» совпала бы с рабочей книгой."),
    }


def _r(value, nd: int = 4):
    return round(value, nd) if value is not None else None
