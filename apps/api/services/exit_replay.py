"""Offline A/B exit-параметров по записанным траекториям сделок (#audit-traj).

Прогоняет варианты exit-конфига по lifecycle.traj из trade_outcomes.jsonl и
отвечает result-based: какой набор параметров дал бы лучший суммарный результат
на РЕАЛЬНЫХ траекториях.

Два профиля ведения — у них разные лестницы выхода и разные параметры:

  * `scalp`  — arm / giveback / time-stop (исторический путь);
  * `trend`  — (#backtest-trend-2026-07-27) полная лестница: безубыток-замок →
    полоса захвата (ярус 2) → ride-трейл, плюс сквозной порог
    `MIN_PROTECTIVE_EXIT_PCT`.

Почему добавлен trend. Раньше `build()` брал только `trade_mode in (scalp,
range)` — то есть молча пропускал ВЕСЬ трендовый контур. На боевой выборке
#264–282 это 16 сделок из 18. Инструмент, созданный искать течь в выходах, не
видел 89% выходов; при этом именно там обнаружился потолок прибыли: десять
победителей подряд закрылись в полосе +0.05…+0.10% при пиках до 1.54%.

Инварианты честности:
  - replay может закрыть сделку только РАНЬШЕ фактического закрытия; если
    правило не сработало — берём фактический final_result_pct (как и было);
  - выход книжится по ТЕКУЩЕЙ точке траектории, а не по защитному уровню:
    стоп не исполняется лучше рынка (тот же инвариант, что и #phantom-fill);
  - издержки у всех вариантов одинаковы (ровно один выход) → сравнение по
    gross-% корректно, комиссии сокращаются;
  - сделки без траектории (старые логи) пропускаются и честно считаются;
  - перебор по сотне вариантов на ~300 сделках легко рождает красивый мусор,
    поэтому лидер проверяется на двух половинах выборки — см. `_split_check`.

Только чтение датасета. На торговлю не влияет.
"""
from __future__ import annotations

import json
import math
from itertools import product
from pathlib import Path

from core.config import settings


def _sanitize_float(value, default=0.0) -> float:
    """Санитизация float-значений для JSON: nan/inf -> default."""
    if value is None:
        return default
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _honest_final_pct(row: dict, traj: list, final_pct: float) -> tuple[float, bool]:
    """(#replay-honesty-2026-07-25) Честный результат фактического закрытия.

    Записанный `result_pct` мог быть посчитан от ФАНТОМНОЙ цены выхода (баг
    #phantom-fill: защитные ветки книжили филл по `MIN_PROTECTIVE_EXIT_PCT`,
    а не по рынку). Признак — результат выше максимума, который сделка вообще
    видела: result_pct > mfe_pct. В этом случае берём последнюю точку
    траектории — реальную цену на момент закрытия.

    Возвращает (pct, is_phantom).
    """
    lc = row.get("lifecycle") or {}
    try:
        mfe_raw = lc.get("mfe_pct")
        if mfe_raw is None:
            return final_pct, False
        mfe = _sanitize_float(mfe_raw, 0.0)
    except (TypeError, ValueError):
        return final_pct, False
    if final_pct > mfe + 1e-9:
        try:
            last_val = _sanitize_float(traj[-1][1], mfe)
            return last_val, True
        except (TypeError, ValueError, IndexError):
            return mfe, True
    return final_pct, False


def _cost_pct(row: dict) -> float:
    """Издержки сделки в % от нотионала — цена приведения траектории к нетто.

    (#replay-units-2026-08-03) Точки `traj` — ВАЛОВОЕ движение цены, а
    `result_pct` — результат ПОСЛЕ издержек. Пока ранний выход возвращал точку
    траектории, а базой служила сумма `result_pct`, каждый такой выход получал
    даром разницу единиц — на боевой выборке это ровно 0.1385–0.1386% на сделку
    (сверено на #345 и #347). Подарок доставался тем вариантам, которые СИЛЬНЕЕ
    меняют поведение: текущий конфиг чаще доходит до `actual_close` и потому
    получал меньше всех. Инструмент против подгонки сам голосовал за правки.

    Считаем из факта: издержки / (qty × цена входа) × 100.
    """
    try:
        cost = _sanitize_float(row.get("closed_total_cost"), 0.0)
        qty = _sanitize_float(row.get("qty"), 0.0)
        entry = _sanitize_float((row.get("lifecycle") or {}).get("entry_price"), 0.0)
        notional = qty * entry
        if cost > 0 and notional > 0:
            return cost / notional * 100.0
    except (TypeError, ValueError):
        pass
    # Фолбэк — модельный круг по конфигу. Хуже факта, но лучше нуля: ноль
    # означал бы возврат к сравнению валового с чистым.
    taker = _sanitize_float(getattr(settings, "FUTURES_TAKER_FEE", 0.0005), 0.0005)
    slip = _sanitize_float(getattr(settings, "SLIPPAGE_BUFFER_PCT", 0.0002), 0.0002)
    return (taker + slip) * 2 * 100.0


def _replay_one(traj: list, final_pct: float, *, arm: float, give: float,
                ts_min: float | None, hard_mult: float,
                cost_pct: float = 0.0) -> tuple[float, str]:
    """Возвращает (result_pct, exit_reason) для варианта конфига.

    ВАЖНО (#replay-honesty-2026-07-25): `final_pct` здесь — ЧЕСТНЫЙ результат
    удержания до конца траектории, а не записанный `result_pct` факта. Раньше
    fallback брал фактический результат, который сам был произведён ТЕКУЩИМ
    конфигом → каждый вариант, который не сработал раньше, наследовал исход
    текущего конфига, и текущий не мог проиграть. Отсюда «текущий конфиг №1
    шесть замеров подряд» — артефакт сравнения конфига с самим собой.

    (#replay-units-2026-08-03) Ранний выход книжится по траектории, то есть
    ВАЛОВО, и потому обязан заплатить круг издержек. `final_pct` уже чистый —
    из него не вычитаем, иначе издержки спишутся дважды.
    """
    mfe = 0.0
    ts_sec = (ts_min or 0.0) * 60.0
    hard_sec = ts_sec * max(hard_mult, 1.0)
    for point in traj:
        try:
            age = _sanitize_float(point[0], 0.0)
            pct = _sanitize_float(point[1], 0.0)
        except (TypeError, ValueError, IndexError):
            continue
        mfe = max(mfe, pct)
        # scalp breakeven lock: вооружились и отдали долю пика
        if mfe >= arm and mfe > 0 and (mfe - pct) >= mfe * give:
            return pct - cost_pct, "replay_breakeven_lock"
        # time stop (с grace до hard: не в значимом минусе → держим)
        if ts_min and age >= ts_sec and mfe < arm:
            net_safe = _sanitize_float(getattr(settings, "NET_SAFE_FLOOR_SWAP_PCT", 0.30), 0.30)
            if age >= hard_sec or pct <= -net_safe:
                return pct - cost_pct, "replay_time_stop"
    return final_pct, "actual_close"


def _replay_trend_one(
    traj: list,
    final_pct: float,
    *,
    be_arm: float,
    be_floor: float,
    band_arm: float,
    band_give: float,
    band_floor: float,
    ride_arm: float,
    ride_trail: float,
    min_protective: float,
    capture_start: float | None = None,
    capture_drawdown: float = 0.30,
    capture_share: float = 0.40,
    cost_pct: float = 0.0,
) -> tuple[float, str]:
    """(#backtest-trend-2026-07-27) Трендовая лестница выхода по траектории.

    Порядок ярусов повторяет `exit_policy`: чем выше добрался MFE, тем более
    щедрая ветка ведёт сделку.

      ярус 3 (ride)  mfe ≥ ride_arm   → трейл на доле пика
      ярус 2 (band)  mfe ≥ band_arm   → выход, отдав долю пика, но не ниже пола
      ярус 1 (BE)    mfe ≥ be_arm     → стоп в безубыток+floor

    Ключевая деталь — `min_protective`. Защитные ветки не срабатывают, если
    результат ниже этого порога, и сделка проваливается на безубыток. Именно
    так порог 1.80% превращал пик 1.38% в фиксацию +0.07%: до 1.80 сделка не
    дотягивала, ярусы 2–3 молчали, оставался замок. Поэтому параметр входит в
    перебор — его влияние надо видеть, а не предполагать.

    Выход книжится по ТЕКУЩЕЙ точке (`pct`), а не по защитному уровню: стоп не
    исполняется лучше рынка.

    (#replay-units-2026-08-03) И платит круг издержек: `traj` валовая, а
    `final_pct` уже чистый. Пороги (`min_protective`, `band_floor`) сравниваются
    с ВАЛОВЫМ `pct` — так же, как в бою: `exit_policy` смотрит на движение цены,
    а издержки вычитаются при закрытии.
    """
    mfe = 0.0
    for point in traj:
        try:
            pct = _sanitize_float(point[1], 0.0)
        except (TypeError, ValueError, IndexError):
            continue
        mfe = max(mfe, pct)

        if mfe >= ride_arm:
            protect = mfe * (1.0 - ride_trail)
            if pct <= protect and pct >= min_protective:
                return pct - cost_pct, "replay_trend_trail"
        elif capture_start is not None and mfe >= capture_start:
            # adaptive_mfe_capture: пик набран, откат превысил порог — забираем
            # долю пика. Ярус живёт МЕЖДУ ride и полосой захвата, поэтому его
            # параметры тоже должны перебираться, а не считаться данностью.
            if (mfe - pct) >= capture_drawdown and pct >= max(
                mfe * capture_share, min_protective
            ):
                return pct - cost_pct, "replay_mfe_capture"
        elif mfe >= band_arm:
            if (mfe - pct) >= mfe * band_give and pct >= max(band_floor, min_protective):
                return pct - cost_pct, "replay_capture_band"

        # Замок безубытка — последний рубеж; срабатывает, когда цена вернулась
        # к входу. Порогом min_protective НЕ гейтится: это стоп, а не фиксация.
        if mfe >= be_arm and pct <= be_floor:
            return pct - cost_pct, "replay_breakeven"

    return final_pct, "actual_close"


def _fidelity_verdict(*, current_pct: float, actual_pct: float, best_pct: float) -> dict:
    """Воспроизводит ли модель саму себя. (#replay-fidelity-2026-08-03)

    Реплей ТЕКУЩЕГО конфига на тех же сделках обязан дать примерно фактический
    результат — это один конфиг на одних данных. Первый боевой замер:

        факт            −8.5459
        текущий конфиг  −12.3596   → разрыв 3.81 п.п. при результате 8.5
        лидер           −8.5875    → дельта 0.04

    Ошибка модели в 91 раз больше её собственного вывода. Значит «вариант X
    лучше» сравнивало две МОДЕЛИ, а не две реальности, и менять по такому
    сравнению конфиг нельзя.

    Причина разрыва в лестнице `_replay_trend_one`: она знает безубыток, полосу
    и трейл, но не знает стоп, tp1-partial, tp2, adaptive-трейл и flow-выходы.
    Пока их нет, модель будет расходиться с движком систематически.

    Два условия доверия, оба обязательны:
      * разрыв мал сам по себе;
      * разрыв МЕНЬШЕ дельты лидера — иначе достаточно найти вариант
        поэкстремальнее, чтобы «доказать» любой вывод.
    """
    gap = _sanitize_float(current_pct, 0.0) - _sanitize_float(actual_pct, 0.0)
    scale = abs(_sanitize_float(actual_pct, 0.0)) or 1.0
    best_edge = abs(_sanitize_float(best_pct, 0.0) - _sanitize_float(actual_pct, 0.0))
    trustworthy = bool(abs(gap) <= max(0.5, 0.1 * scale) and best_edge > abs(gap))

    return {
        "current_replayed_pct": round(_sanitize_float(current_pct, 0.0), 4),
        "actual_pct": round(_sanitize_float(actual_pct, 0.0), 4),
        "gap_pct": round(gap, 4),
        "gap_share_of_result": round(abs(gap) / scale, 3),
        "best_edge_pct": round(best_edge, 4),
        "gap_over_edge": round(abs(gap) / best_edge, 1) if best_edge > 1e-9 else None,
        "trustworthy": trustworthy,
        "verdict": (
            "модель воспроизводит текущий конфиг — сравнению вариантов можно верить"
            if trustworthy else
            f"модель НЕ воспроизводит текущий конфиг: разрыв {abs(gap):.2f} п.п. "
            f"против дельты лидера {best_edge:.2f}. Сначала достроить лестницу "
            f"выходов (нет стопа, tp1/tp2, adaptive-трейла), потом сравнивать."
        ),
    }


def _split_check(trades: list[dict], run, best_key: dict, keyfn) -> dict:
    """Лидер обязан выигрывать на обеих половинах выборки, а не только в сумме.

    Перебор сотни вариантов по нескольким сотням сделок почти гарантированно
    находит комбинацию, которая обслуживает пару удачных исходов. Дешёвая
    защита — разрезать выборку хронологически пополам и посмотреть, остаётся
    ли лидер лидером в каждой половине отдельно.
    """
    half = len(trades) // 2
    if half < 10:
        return {
            "checked": False,
            "reason": f"в половине меньше 10 сделок ({half}) — разбиение бессмысленно",
        }

    out = {}
    for name, subset in (("first_half", trades[:half]), ("second_half", trades[half:])):
        ranked = sorted(run(subset), key=lambda v: v["total_pct"], reverse=True)
        pos = next((i for i, v in enumerate(ranked) if keyfn(v) == keyfn(best_key)), None)
        out[name] = {
            "leader_rank": (pos + 1) if pos is not None else None,
            "leader_total_pct": next(
                (v["total_pct"] for v in ranked if keyfn(v) == keyfn(best_key)), None
            ),
            "half_best_total_pct": ranked[0]["total_pct"] if ranked else None,
            "trades": len(subset),
        }

    # Порог: лидер обязан попасть в верхнюю четверть вариантов на КАЖДОЙ
    # половине. Прежнее правило `max(ranks) <= max(3, len(ranks))` было
    # бессмысленным — на сетке из трёх вариантов оно засчитывало последнее
    # место как устойчивость. Тест это и поймал.
    n_variants = max(len(run(trades[:half]) or []), 1)
    top_quarter = max(1, -(-n_variants // 4))
    ranks = [v["leader_rank"] for v in out.values() if v["leader_rank"]]
    robust = bool(ranks) and len(ranks) == 2 and max(ranks) <= top_quarter
    out["top_quarter_rank"] = top_quarter
    out["variants_per_half"] = n_variants
    out["checked"] = True
    out["robust"] = robust
    out["verdict"] = (
        "лидер держится на обеих половинах — на подгонку не похоже"
        if robust
        else "лидер выигрывает только на всей выборке: вероятна подгонка, менять конфиг рано"
    )
    return out


def build(limit: int = 2000) -> dict:
    from services.ml_trade_logger import MLTradeLogger
    path = MLTradeLogger().path
    rows = _load_rows(Path(path))[-int(limit):]

    trades = []
    skipped_no_traj = 0
    phantom_count = 0
    for r in rows:
        lc = r.get("lifecycle") or {}
        traj = lc.get("traj")
        # replay применим к scalp/range-профилю ведения
        mode = str(r.get("trade_mode") or "").lower()
        if mode not in ("scalp", "range"):
            continue
        final_pct = r.get("result_pct")
        if final_pct is None:
            continue
        if not traj or len(traj) < 3:
            skipped_no_traj += 1
            continue
        honest_pct, is_phantom = _honest_final_pct(r, traj, _sanitize_float(final_pct, 0.0))
        if is_phantom:
            phantom_count += 1
        trades.append({"traj": traj, "final_pct": honest_pct,
                       "booked_pct": _sanitize_float(final_pct, 0.0), "phantom": is_phantom,
                       "cost_pct": _cost_pct(r),
                       "symbol": r.get("symbol"), "signal_id": r.get("signal_id")})

    if not trades:
        return {
            "status": "no_data",
            "scalp_closed_total": skipped_no_traj,
            "with_trajectory": 0,
            "message": ("Нет scalp/range-сделок с записанной траекторией. Траектории "
                        "пишутся с момента включения TRAJ_RECORD_ENABLED — подожди новых закрытий."),
        }

    arms = [0.3, 0.5, 0.7]
    gives = [0.4, 0.5, 0.6]
    time_stops = [45.0, 90.0, None]  # None = time-stop off
    hard_mult = _sanitize_float(getattr(settings, "SCALP_TIME_STOP_HARD_MULT", 2.0))

    actual_total = round(sum(t["final_pct"] for t in trades), 4)
    variants = []
    for arm, give, ts in product(arms, gives, time_stops):
        total = 0.0
        wins = 0
        early_exits = 0
        for t in trades:
            pct, reason = _replay_one(t["traj"], t["final_pct"],
                                      arm=arm, give=give, ts_min=ts, hard_mult=hard_mult,
                                      cost_pct=t.get("cost_pct", 0.0))
            total += pct
            wins += 1 if pct > 0 else 0
            early_exits += 1 if reason != "actual_close" else 0
        variants.append({
            "arm_pct": arm,
            "giveback_share": give,
            "time_stop_min": ts,
            "total_pct": round(total, 4),
            "delta_vs_actual_pct": round(total - actual_total, 4),
            "winrate_pct": round(wins / len(trades) * 100, 1),
            "early_exits": early_exits,
        })
    variants.sort(key=lambda v: v["total_pct"], reverse=True)

    def _run_scalp(subset):
        out = []
        for a, g, t in product(arms, gives, time_stops):
            tot = sum(
                _replay_one(x["traj"], x["final_pct"], arm=a, give=g, ts_min=t,
                            hard_mult=hard_mult, cost_pct=x.get("cost_pct", 0.0))[0]
                for x in subset
            )
            out.append({"arm_pct": a, "giveback_share": g, "time_stop_min": t,
                        "total_pct": round(tot, 4)})
        return out

    def _key_scalp(v):
        return (v["arm_pct"], v["giveback_share"], v["time_stop_min"])

    split = _split_check(trades, _run_scalp, variants[0], _key_scalp)

    current = {
        "arm_pct": _sanitize_float(getattr(settings, "SCALP_BREAKEVEN_ARM_PCT", 0.5)),
        "giveback_share": _sanitize_float(getattr(settings, "SCALP_BREAKEVEN_GIVEBACK_SHARE", 0.6)),
        "time_stop_min": (_sanitize_float(getattr(settings, "SCALP_TIME_STOP_MIN", 45.0))
                          if bool(getattr(settings, "SCALP_TIME_STOP_ENABLED", True)) else None),
    }

    return {
        "status": "ok",
        "profile": "scalp",
        "trades_replayed": len(trades),
        "skipped_no_trajectory": skipped_no_traj,
        "actual_total_pct": actual_total,
        "booked_total_pct": round(sum(t["booked_pct"] for t in trades), 4),
        "phantom_fill_trades": phantom_count,
        "current_config": current,
        "best": variants[0],
        "worst": variants[-1],
        "overfit_check": split,
        "variants": variants,
        "note": ("Сравнение по gross-% (издержки у вариантов одинаковы). Replay закрывает "
                 "только РАНЬШЕ факта; траектория даунсемплирована (шаг traj_step) → "
                 "результат консервативная оценка. Выборка <30 сделок = не доказательство. "
                 "(#replay-honesty-2026-07-25) actual_total_pct — ЧЕСТНЫЙ базис: фантомные "
                 "филлы (result_pct > mfe_pct) заменены последней точкой траектории, а "
                 "fallback варианта = удержание до конца траектории, а не результат "
                 "текущего конфига. Прежние замеры «текущий конфиг №1» сравнивали "
                 "конфиг сам с собой и недействительны."),
    }


# ── ТРЕНДОВЫЙ ПРОФИЛЬ ─────────────────────────────────────────────────────────

TREND_MODES = ("trend", "crt", "position", "")


def build_trend(limit: int = 2000) -> dict:
    """(#backtest-trend-2026-07-27) A/B трендовой лестницы выхода.

    Сюда попадает основная масса сделок, и здесь же сидит потолок прибыли,
    найденный 27.07: победители закрывались в полосе +0.05…+0.10% независимо
    от того, был пик 0.35% или 1.54%.
    """
    from services.ml_trade_logger import MLTradeLogger

    rows = _load_rows(Path(MLTradeLogger().path))[-int(limit):]

    trades: list[dict] = []
    skipped_no_traj = 0
    phantom_count = 0
    for r in rows:
        mode = str(r.get("trade_mode") or "").lower()
        if mode in ("scalp", "range") or mode not in TREND_MODES:
            continue
        final_pct = r.get("result_pct")
        if final_pct is None:
            continue
        lc = r.get("lifecycle") or {}
        traj = lc.get("traj")
        if not traj or len(traj) < 3:
            skipped_no_traj += 1
            continue
        honest_pct, is_phantom = _honest_final_pct(r, traj, _sanitize_float(final_pct, 0.0))
        phantom_count += int(is_phantom)
        trades.append({
            "traj": traj,
            "final_pct": honest_pct,
            "booked_pct": _sanitize_float(final_pct, 0.0),
            "phantom": is_phantom,
            "cost_pct": _cost_pct(r),
            "symbol": r.get("symbol"),
            "signal_id": r.get("signal_id"),
            "mfe_pct": lc.get("mfe_pct"),
        })

    if not trades:
        return {
            "status": "no_data",
            "profile": "trend",
            "skipped_no_trajectory": skipped_no_traj,
            "message": ("Нет трендовых сделок с записанной траекторией. "
                        "Траектории пишутся с момента включения TRAJ_RECORD_ENABLED."),
        }

    # Сетка держится компактной намеренно: чем больше комбинаций, тем выше шанс,
    # что лидер обслуживает пару удачных исходов. У каждой оси — своя гипотеза.
    be_arms = [0.35, 0.60, 1.00]        # позже взводим замок — дольше живёт сделка
    be_floors = [0.10, 0.25]
    band_arms = [0.30, 0.40, 0.55]
    band_gives = [0.25, 0.35]
    ride_trails = [0.35, 0.50]
    # 1.80 — прежнее боевое значение, 0.40 — правка 27.07. Ось нужна, чтобы
    # эффект правки был ИЗМЕРЕН, а не заявлен.
    min_protectives = [0.40, 1.80]
    # (#band-corridor-2026-08-03) ride_arm БЫЛ константой из конфига — и это
    # закрывало от замера ровно тот вопрос, который стоит: полоса захвата живёт
    # в коридоре [band_arm, ride_arm), и все 15 боевых закрытий по
    # `trend_capture_band` попали в него (MFE 0.41–0.76 при коридоре 0.40–0.80).
    # Ни одно не вышло за границу. Ширину коридора нельзя было ни подтвердить,
    # ни опровергнуть, пока его правая граница не перебиралась.
    ride_arms = [0.55, 0.80, 1.20]

    band_floor = _sanitize_float(getattr(settings, "TREND_CAPTURE_FLOOR_PCT", 0.30))

    def _run(subset: list[dict]) -> list[dict]:
        out = []
        for be_arm, be_floor, band_arm, band_give, ride_trail, min_prot, ride_arm in product(
            be_arms, be_floors, band_arms, band_gives, ride_trails, min_protectives, ride_arms
        ):
            total = 0.0
            wins = 0
            reasons: dict[str, int] = {}
            for t in subset:
                pct, reason = _replay_trend_one(
                    t["traj"], t["final_pct"],
                    be_arm=be_arm, be_floor=be_floor,
                    band_arm=band_arm, band_give=band_give, band_floor=band_floor,
                    ride_arm=ride_arm, ride_trail=ride_trail,
                    min_protective=min_prot,
                    cost_pct=t.get("cost_pct", 0.0),
                )
                total += pct
                wins += int(pct > 0)
                reasons[reason] = reasons.get(reason, 0) + 1
            out.append({
                "be_arm_pct": be_arm,
                "be_floor_pct": be_floor,
                "band_arm_pct": band_arm,
                "band_giveback_share": band_give,
                "ride_trail_share": ride_trail,
                "min_protective_pct": min_prot,
                "ride_arm_pct": ride_arm,
                # Ширина коридора, в котором полоса перехватывает управление у
                # трейла. Отрицательная = коридора нет, полоса выключена собой.
                "band_corridor_width": round(ride_arm - band_arm, 4),
                "total_pct": round(total, 4),
                "avg_pct": round(total / len(subset), 4),
                "winrate_pct": round(wins / len(subset) * 100, 1),
                "by_reason": reasons,
            })
        return out

    actual_total = round(sum(t["final_pct"] for t in trades), 4)
    variants = _run(trades)
    for v in variants:
        v["delta_vs_actual_pct"] = round(v["total_pct"] - actual_total, 4)
    variants.sort(key=lambda v: v["total_pct"], reverse=True)

    def _key(v):
        return (v["be_arm_pct"], v["be_floor_pct"], v["band_arm_pct"],
                v["band_giveback_share"], v["ride_trail_share"], v["min_protective_pct"],
                v["ride_arm_pct"])

    split = _split_check(trades, _run, variants[0], _key)

    current = {
        "be_arm_pct": _sanitize_float(getattr(settings, "BREAKEVEN_LOCK_ARM_PCT", 0.35)),
        "be_floor_pct": _sanitize_float(getattr(settings, "BREAKEVEN_LOCK_FLOOR_PCT", 0.10)),
        "band_arm_pct": _sanitize_float(getattr(settings, "TREND_CAPTURE_BAND_ARM_PCT", 0.40)),
        "band_giveback_share": _sanitize_float(getattr(settings, "TREND_CAPTURE_BAND_GIVEBACK_SHARE", 0.25)),
        "ride_trail_share": _sanitize_float(getattr(settings, "TREND_RIDE_TRAIL_DRAWDOWN_PCT", 0.50)),
        "min_protective_pct": _sanitize_float(getattr(settings, "MIN_PROTECTIVE_EXIT_PCT", 0.40)),
        "ride_arm_pct": _sanitize_float(getattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT", 0.8)),
    }
    current_row = next((v for v in variants if _key(v) == _key(current)), None)

    # (#replay-fidelity-2026-08-03) Прежде чем сравнивать варианты, модель
    # обязана воспроизвести САМА СЕБЯ: реплей текущего конфига на тех же
    # сделках должен дать примерно фактический результат. Первый замер:
    #   факт −8.5459, реплей текущего конфига −12.3596 → разрыв 3.81 п.п.
    # при общем результате 8.5. Дельта лидера при этом −0.04. То есть разница
    # между вариантами на два порядка меньше ошибки самой модели, и любой
    # вывод «вариант X лучше» был бы сравнением двух моделей, а не реальностей.
    #
    # Причина разрыва: лестница в _replay_trend_one знает только безубыток,
    # полосу и трейл. Реальных выходов больше — стоп, tp1-partial, tp2,
    # adaptive-трейл, flow-выходы. Пока их нет, модель систематически
    # расходится с движком.
    fidelity: dict = {"checked": bool(current_row)}
    if current_row:
        fidelity.update(_fidelity_verdict(
            current_pct=current_row["total_pct"],
            actual_pct=actual_total,
            best_pct=variants[0]["total_pct"],
        ))

    # Оси, которые НЕ повлияли ни на один вариант, — признак того, что эффект
    # запирается другим параметром. Без этого «лидер» можно принять за ответ
    # на вопрос, который на самом деле не задавался.
    inert_axes = []
    for axis in ("ride_arm_pct", "band_arm_pct", "band_giveback_share", "ride_trail_share"):
        totals = {v["total_pct"] for v in variants if v.get(axis) is not None}
        by_axis: dict = {}
        for v in variants:
            by_axis.setdefault(v[axis], set()).add(v["total_pct"])
        if len(totals) > 1 and all(len(s) == len(totals) for s in by_axis.values()):
            inert_axes.append(axis)

    return {
        "status": "ok",
        "profile": "trend",
        "fidelity": fidelity,
        "inert_axes": inert_axes,
        "trades_replayed": len(trades),
        "skipped_no_trajectory": skipped_no_traj,
        "actual_total_pct": actual_total,
        "actual_avg_pct": round(actual_total / len(trades), 4),
        "booked_total_pct": round(sum(t["booked_pct"] for t in trades), 4),
        "phantom_fill_trades": phantom_count,
        # (#band-corridor-2026-08-03) ride_arm БОЛЬШЕ НЕ фиксирован — он стал
        # осью перебора, потому что именно его расстояние до band_arm задаёт
        # коридор, в котором полоса перехватывает управление у трейла.
        "fixed": {"band_floor_pct": band_floor},
        "axes": {
            "ride_arm_pct": ride_arms,
            "band_arm_pct": band_arms,
            "band_giveback_share": band_gives,
            "ride_trail_share": ride_trails,
            "be_arm_pct": be_arms,
            "be_floor_pct": be_floors,
            "min_protective_pct": min_protectives,
        },
        "current_config": current,
        "current_rank": (variants.index(current_row) + 1) if current_row else None,
        "current_total_pct": current_row["total_pct"] if current_row else None,
        "variants_count": len(variants),
        "best": variants[0],
        "worst": variants[-1],
        "overfit_check": split,
        "variants": variants[:40],
        "note": (
            "Сравнение по ЧИСТЫМ %. Прежняя формулировка «издержки у всех вариантов "
            "одинаковы, комиссии сокращаются» была неверна и стоила направленного "
            "смещения: точки traj валовые, а базис actual_* — чистый, поэтому каждый "
            "ранний выход получал даром ~0.14% (#replay-units-2026-08-03). Подарок был "
            "пропорционален числу ранних выходов, то есть доставался вариантам, которые "
            "СИЛЬНЕЕ меняют поведение. Теперь ранний выход платит круг издержек по факту "
            "сделки. Выход книжится по ТЕКУЩЕЙ точке траектории — стоп не исполняется "
            "лучше рынка. Базис actual_* честный: фантомные филлы заменены последней "
            "точкой траектории. Ось min_protective_pct содержит прежнее боевое 1.80 и "
            "правку 0.40. Ось ride_arm_pct задаёт правую границу коридора, в котором "
            "полоса захвата перехватывает управление у трейла: смотрите "
            "band_corridor_width у лидера. Выборка меньше 30 сделок доказательством не "
            "является; смотрите overfit_check прежде чем что-то менять."
        ),
    }


# ── WALK-FORWARD ──────────────────────────────────────────────────────────────
#
# Зачем отдельно от build_trend. `build_trend` подбирает параметры на ВСЕЙ
# выборке и там же их оценивает — это in-sample, и лидер такой процедуры почти
# всегда красивее, чем он есть. `_split_check` ловит грубую подгонку, но не
# отвечает на главный вопрос: работал бы найденный конфиг на данных, которых
# он не видел.
#
# Walk-forward отвечает. Выборка режется хронологически на фолды; для каждого
# фолда k параметры подбираются на фолдах [0..k-1] и применяются к фолду k без
# права пересмотра. Сумма по фолдам — честная out-of-sample оценка.
#
# Практический смысл: если OOS-результат подбора не бьёт текущий конфиг на тех
# же данных, подбирать нечего — разница была шумом.

TREND_GRID_AXES = {
    "be_arm_pct": [0.35, 0.60, 1.00],
    "be_floor_pct": [0.10, 0.25],
    "band_arm_pct": [0.30, 0.40, 0.55],
    "band_giveback_share": [0.25, 0.35],
    "ride_trail_share": [0.35, 0.50],
    "min_protective_pct": [0.40, 1.80],
    "capture_start_pct": [0.90, None],
    "capture_drawdown_pct": [0.30],
    "capture_share": [0.40],
}

SCALP_GRID_AXES = {
    "arm_pct": [0.3, 0.5, 0.7],
    "giveback_share": [0.4, 0.5, 0.6],
    "time_stop_min": [45.0, 90.0, None],
}

# Режим -> какие trade_mode в него попадают. Разрезы нужны потому, что
# лестницы выхода у движков разные: оптимум скальпа ничего не говорит о тренде.
REGIME_MODES = {
    "trend": ("trend", "crt", "position", ""),
    "range": ("range",),
    "scalp": ("scalp",),
}


def _grid(axes: dict) -> list[dict]:
    keys = list(axes)
    return [dict(zip(keys, combo)) for combo in product(*(axes[k] for k in keys))]


def _load_trades(regime: str, limit: int) -> tuple[list[dict], int, int]:
    """Сделки нужного режима с траекторией, в хронологическом порядке."""
    from services.ml_trade_logger import MLTradeLogger

    allowed = REGIME_MODES.get(regime, REGIME_MODES["trend"])
    rows = _load_rows(Path(MLTradeLogger().path))[-int(limit):]

    trades: list[dict] = []
    skipped = 0
    phantom = 0
    for r in rows:
        mode = str(r.get("trade_mode") or "").lower()
        if mode not in allowed:
            continue
        final_pct = r.get("result_pct")
        if final_pct is None:
            continue
        lc = r.get("lifecycle") or {}
        traj = lc.get("traj")
        if not traj or len(traj) < 3:
            skipped += 1
            continue
        honest, is_phantom = _honest_final_pct(r, traj, _sanitize_float(final_pct, 0.0))
        phantom += int(is_phantom)
        trades.append({
            "traj": traj, "final_pct": honest, "booked_pct": _sanitize_float(final_pct, 0.0),
            "symbol": r.get("symbol"), "signal_id": r.get("signal_id"),
            "mfe_pct": lc.get("mfe_pct"), "phantom": is_phantom,
        })
    return trades, skipped, phantom


def _score(trades: list[dict], params: dict, regime: str) -> float:
    """Суммарный gross-% набора параметров на выборке."""
    total = 0.0
    if regime == "scalp":
        hard = _sanitize_float(getattr(settings, "SCALP_TIME_STOP_HARD_MULT", 2.0))
        for t in trades:
            total += _replay_one(
                t["traj"], t["final_pct"],
                arm=params["arm_pct"], give=params["giveback_share"],
                ts_min=params["time_stop_min"], hard_mult=hard,
            )[0]
        return total

    band_floor = _sanitize_float(getattr(settings, "TREND_CAPTURE_FLOOR_PCT", 0.30))
    # (#band-corridor-2026-08-03) ride_arm берём ИЗ ПАРАМЕТРОВ варианта, а не
    # из конфига. Иначе walk-forward оценивал бы лидера с чужим значением оси:
    # перебор выбрал бы одну правую границу коридора, а проверка вне выборки
    # мерила бы другую — и по новой оси проверка ничего бы не значила.
    ride_arm = float(params.get(
        "ride_arm_pct",
        getattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT", 0.8),
    ))
    for t in trades:
        total += _replay_trend_one(
            t["traj"], t["final_pct"],
            be_arm=params["be_arm_pct"], be_floor=params["be_floor_pct"],
            band_arm=params["band_arm_pct"], band_give=params["band_giveback_share"],
            band_floor=band_floor, ride_arm=ride_arm,
            ride_trail=params["ride_trail_share"],
            min_protective=params["min_protective_pct"],
            capture_start=params.get("capture_start_pct"),
            capture_drawdown=params.get("capture_drawdown_pct", 0.30),
            capture_share=params.get("capture_share", 0.40),
            cost_pct=t.get("cost_pct", 0.0),
        )[0]
    return total


def _current_params(regime: str) -> dict:
    if regime == "scalp":
        return {
            "arm_pct": _sanitize_float(getattr(settings, "SCALP_BREAKEVEN_ARM_PCT", 0.3)),
            "giveback_share": _sanitize_float(getattr(settings, "SCALP_BREAKEVEN_GIVEBACK_SHARE", 0.4)),
            "time_stop_min": (_sanitize_float(getattr(settings, "SCALP_TIME_STOP_MIN", 45.0))
                              if bool(getattr(settings, "SCALP_TIME_STOP_ENABLED", True)) else None),
        }
    return {
        "be_arm_pct": _sanitize_float(getattr(settings, "BREAKEVEN_LOCK_ARM_PCT", 0.35)),
        "be_floor_pct": _sanitize_float(getattr(settings, "BREAKEVEN_LOCK_FLOOR_PCT", 0.10)),
        "band_arm_pct": _sanitize_float(getattr(settings, "TREND_CAPTURE_BAND_ARM_PCT", 0.40)),
        "band_giveback_share": _sanitize_float(getattr(settings, "TREND_CAPTURE_BAND_GIVEBACK_SHARE", 0.25)),
        "ride_trail_share": _sanitize_float(getattr(settings, "TREND_RIDE_TRAIL_DRAWDOWN_PCT", 0.50)),
        "min_protective_pct": _sanitize_float(getattr(settings, "MIN_PROTECTIVE_EXIT_PCT", 0.40)),
        # Боевое значение правой границы коридора — чтобы «текущий конфиг» в
        # walk-forward считался тем же способом, что и варианты перебора.
        "ride_arm_pct": _sanitize_float(getattr(settings, "TREND_RIDE_MIN_MFE_TO_PROTECT_PCT", 0.8)),
        "capture_start_pct": (_sanitize_float(getattr(settings, "MFE_CAPTURE_START_PCT", 0.9))
                              if bool(getattr(settings, "MFE_CAPTURE_ENABLED", True)) else None),
        "capture_drawdown_pct": _sanitize_float(getattr(settings, "MFE_CAPTURE_DRAWDOWN_PCT", 0.30)),
        "capture_share": _sanitize_float(getattr(settings, "MFE_CAPTURE_PROTECT_SHARE", 0.40)),
    }


def walk_forward(regime: str = "trend", folds: int = 4, limit: int = 2000,
                 min_train: int = 20) -> dict:
    """Честная out-of-sample оценка подбора exit-параметров.

    Для каждого фолда k: параметры выбираются на [0..k-1], применяются к k.
    Ни один результат в `oos_total_pct` не получен на данных, которые видел
    оптимизатор.
    """
    regime = regime if regime in REGIME_MODES else "trend"
    trades, skipped, phantom = _load_trades(regime, limit)
    axes = SCALP_GRID_AXES if regime == "scalp" else TREND_GRID_AXES
    grid = _grid(axes)
    current = _current_params(regime)

    folds = max(2, min(int(folds), 8))
    if len(trades) < min_train + folds:
        return {
            "status": "insufficient_data",
            "regime": regime,
            "trades": len(trades),
            "skipped_no_trajectory": skipped,
            "required": min_train + folds,
            "message": (
                f"Для walk-forward нужно минимум {min_train + folds} сделок режима "
                f"«{regime}» с траекторией, есть {len(trades)}. Пока копится — "
                "смотрите in-sample через /ml/exit-replay, помня о его ограничении."
            ),
        }

    size = len(trades) // folds
    bounds = [(i * size, (i + 1) * size if i < folds - 1 else len(trades))
              for i in range(folds)]

    steps = []
    oos_total = 0.0
    oos_current = 0.0
    for k, (lo, hi) in enumerate(bounds):
        train = trades[:lo]
        test = trades[lo:hi]
        if len(train) < min_train or not test:
            steps.append({
                "fold": k + 1, "train_size": len(train), "test_size": len(test),
                "skipped": True,
                "reason": f"обучающая часть меньше {min_train} — фолд не оценивается",
            })
            continue

        best = max(grid, key=lambda p: _score(train, p, regime))
        picked = _score(test, best, regime)
        base = _score(test, current, regime)
        oos_total += picked
        oos_current += base
        steps.append({
            "fold": k + 1,
            "train_size": len(train),
            "test_size": len(test),
            "skipped": False,
            "picked_params": best,
            "picked_test_pct": round(picked, 4),
            "current_test_pct": round(base, 4),
            "edge_pct": round(picked - base, 4),
        })

    scored = [s for s in steps if not s.get("skipped")]
    wins = sum(1 for s in scored if s["edge_pct"] > 0)
    edge = round(oos_total - oos_current, 4)

    # Стабильность выбора: если каждый фолд просит СВОИ параметры, то оптимума
    # нет — есть шум, и внедрять «лучший» нельзя.
    picks = [tuple(sorted(s["picked_params"].items(), key=lambda kv: kv[0])) for s in scored]
    unique_picks = len(set(picks))

    if not scored:
        verdict = "фолдов с достаточной обучающей частью нет — данных мало"
    elif edge <= 0:
        verdict = ("подбор НЕ бьёт текущий конфиг вне выборки — разница на всей "
                   "истории была подгонкой, менять нечего")
    elif unique_picks == len(scored) and len(scored) > 2:
        verdict = ("каждый фолд просит свои параметры — устойчивого оптимума нет, "
                   "внедрять нельзя")
    elif wins < len(scored) - 1:
        verdict = (f"перевес только в {wins} фолдах из {len(scored)} — сигнал слабый, "
                   "стоит подождать данных")
    else:
        verdict = ("подбор устойчиво бьёт текущий конфиг вне выборки — "
                   "изменение обосновано")

    return {
        "status": "ok",
        "regime": regime,
        "folds": folds,
        "trades": len(trades),
        "skipped_no_trajectory": skipped,
        "phantom_fill_trades": phantom,
        "grid_size": len(grid),
        "current_config": current,
        "oos_total_pct": round(oos_total, 4),
        "oos_current_pct": round(oos_current, 4),
        "oos_edge_pct": edge,
        "folds_won": wins,
        "folds_scored": len(scored),
        "unique_param_picks": unique_picks,
        "steps": steps,
        "verdict": verdict,
        "note": (
            "Out-of-sample: в каждом фолде параметры выбраны ТОЛЬКО на предыдущих "
            "сделках и применены к последующим без права пересмотра. oos_edge_pct — "
            "то, что подбор дал бы сверх текущего конфига в реальном времени. "
            "Отрицательный или нулевой edge означает, что выигрыш на всей истории "
            "был артефактом подгонки. Разброс unique_param_picks по фолдам — второй "
            "признак: устойчивый оптимум выбирается одинаково."
        ),
    }
