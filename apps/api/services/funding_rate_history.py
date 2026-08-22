"""История ставок фондирования и подтверждение стабильности
(#funding-confirm-2026-07-27).

Проблема одного снимка. Вход открывался по ОДНОМУ замеру ставки: увидели
0.08%/8ч — открылись. Но ставка фондирования mean-reverts, и это доказано нашей
же историей: TRX 16.5% годовых → −1.65%, XRP 14.2% → 3.65%. Единичный высокий
замер чаще означает всплеск, чем режим — а мы закладывались на 10 периодов
удержания вперёд, то есть на 80 часов жизни цифры, снятой один раз.

Отсюда и завышение дохода, которое мы снимали 27.07: начисление шло по ставке
входа. Причина глубже, чем формула начисления, — она в том, что ставка входа
вообще принималась за прогноз.

Что подтверждаем перед открытием:

  1. **Число наблюдений.** Меньше `FUNDING_ARB_MIN_OBSERVATIONS` — судить не о
     чем, даже если текущий замер отличный.
  2. **Стабильность знака.** Ставка, менявшая направление в окне, не даёт
     carry: половину периодов платим мы.
  3. **Ожидаемый carry по КОНСЕРВАТИВНОЙ ставке.** Не текущая и даже не
     средняя, а нижний перцентиль наблюдений: закладываемся на плохой сценарий
     в пределах наблюдавшегося, а не на удачный замер.
  4. **Стресс расширения базиса.** Базис может разойтись против нас за время
     удержания. Проверяем, переживёт ли позиция расширение на заданную
     величину — если нет, carry не покрывает риск конвергенции.

Журнал — компактный jsonl рядом с остальными. Только чтение/дозапись, на
торговлю влияет через гейт `confirm()`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.config import settings


def _path() -> Path:
    return Path(
        str(getattr(settings, "FUNDING_RATE_LOG_PATH", "")
            or "storage/ml/funding_rates.jsonl")
    )


def record(symbol: str, *, rate_pct: float, basis_pct: float,
           ts: float | None = None) -> None:
    """Одно наблюдение. Пишется на каждом скане — история копится сама."""
    row = {
        "ts": round(float(ts if ts is not None else time.time()), 1),
        "s": str(symbol),
        "r": round(float(rate_pct), 6),
        "b": round(float(basis_pct), 6),
    }
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — журнал не должен ронять скан
        pass


def _load(symbol: str, window_hours: float) -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    since = time.time() - float(window_hours) * 3600
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if row.get("s") == symbol and float(row.get("ts") or 0) >= since:
                    out.append(row)
    except Exception:  # noqa: BLE001
        return []
    return out


# Записи ближе этого интервала друг к другу — один и тот же скан, а не разные
# замеры. Наблюдения через час — РАЗНЫЕ данные (ставка меняется внутри периода),
# схлопывать их нельзя: потеряем реальную волатильность ставки.
_SCAN_DEDUPE_SEC = 60.0


def _dedupe_scan_writes(rows: list[dict]) -> list[dict]:
    """Схлопывает повторные записи ОДНОГО скана, не трогая разные замеры.

    `record()` вызывается несколько раз за проход: в журнале лежат строки с
    разницей 0.2–0.3 секунды и побайтово одинаковыми значениями. Информации в
    них нет, а счётчик наблюдений они множат вдвое-втрое.

    Последствие было тихим и опасным: `min_obs` удовлетворялся числом ЗАПИСЕЙ,
    а не числом замеров, то есть гейт подтверждения считал ставку «наблюдавшейся
    достаточно», посмотрев на неё вдвое меньше раз, чем думал. Хуже того,
    `conservative_rate_pct` (нижний квартиль) смещался к самому часто
    записанному значению вместо худшего — защита от всплеска слабела именно там,
    где она нужна.

    Схлопываем ТОЛЬКО соседние строки с идентичными r и b в пределах
    `_SCAN_DEDUPE_SEC`. Замер через час останется отдельным наблюдением.
    """
    window = float(getattr(settings, "FUNDING_SCAN_DEDUPE_SEC", _SCAN_DEDUPE_SEC) or 0.0)
    if window <= 0:
        return list(rows)

    out: list[dict] = []
    for row in rows:
        try:
            ts = float(row.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if out:
            prev = out[-1]
            same_value = (row.get("r") == prev.get("r") and row.get("b") == prev.get("b"))
            close_in_time = abs(ts - float(prev.get("ts") or 0.0)) <= window
            if same_value and close_in_time:
                out[-1] = row      # оставляем последнюю строку скана
                continue
        out.append(row)
    return out


def _percentile(values: list[float], q: float) -> float:
    """Нижний перцентиль без numpy — зависимость тут не нужна."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(int(round(q * (len(ordered) - 1))), len(ordered) - 1))
    return ordered[idx]


def stability(symbol: str, window_hours: float | None = None) -> dict[str, Any]:
    """Статистика ставки по окну наблюдений."""
    window_hours = float(
        window_hours if window_hours is not None
        else getattr(settings, "FUNDING_ARB_OBSERVATION_WINDOW_HOURS", 72.0)
    )
    raw = _load(symbol, window_hours)
    # Считаем ЗАМЕРЫ, а не записи в файл: повторы одного скана — это по-прежнему
    # один замер, и выдавать их за несколько значит обманывать собственный гейт.
    rows = _dedupe_scan_writes(raw)
    rates = [float(r["r"]) for r in rows]
    bases = [float(r.get("b") or 0.0) for r in rows]
    n = len(rates)
    if n == 0:
        return {"observations": 0, "raw_writes": len(raw), "window_hours": window_hours,
                "note": "наблюдений нет — история копится на каждом скане"}

    positive = sum(1 for v in rates if v > 0)
    mean = sum(rates) / n
    var = sum((v - mean) ** 2 for v in rates) / n
    q = float(getattr(settings, "FUNDING_ARB_CONSERVATIVE_QUANTILE", 0.25))

    return {
        "observations": n,
        # Сколько записей стояло за этими наблюдениями: разрыв показывает,
        # насколько журнал дублировал сам себя.
        "raw_writes": len(raw),
        # За какой СРОК набраны наблюдения. Число замеров и охват — разные вещи:
        # шесть замеров за час говорят о ставке куда меньше, чем шесть за двое
        # суток, а гейт `min_obs` считает только первое. Метрика выведена наружу,
        # чтобы решение о пороге принималось по данным (сейчас НЕ блокирует).
        "span_hours": round(
            (max(float(r.get("ts") or 0.0) for r in rows)
             - min(float(r.get("ts") or 0.0) for r in rows)) / 3600.0, 2
        ),
        "window_hours": window_hours,
        "mean_rate_pct": round(mean, 6),
        "min_rate_pct": round(min(rates), 6),
        "max_rate_pct": round(max(rates), 6),
        "std_rate_pct": round(var ** 0.5, 6),
        # Консервативная ставка: нижний квартиль наблюдавшихся. Закладываемся на
        # плохой сценарий В ПРЕДЕЛАХ наблюдавшегося, а не на удачный замер.
        "conservative_rate_pct": round(_percentile(rates, q), 6),
        # Доля периодов, где знак был в нашу пользу. Смена знака означает, что
        # часть периодов платим МЫ.
        "sign_consistency": round(positive / n, 4),
        "mean_basis_pct": round(sum(bases) / n, 6),
        "max_abs_basis_pct": round(max(abs(v) for v in bases), 6),
    }


def confirm(
    symbol: str,
    *,
    current_rate_pct: float,
    basis_pct: float,
    fee_round_trip_pct: float,
) -> dict[str, Any]:
    """Гейт открытия: подтверждена ли ставка настолько, чтобы на неё ставить.

    Возвращает `{"ok": bool, "reason": str|None, ...}`. Fail-closed по данным:
    нет истории — не открываем. Это осознанно: цена ошибки здесь не упущенная
    сделка, а позиция на 80 часов под ставку, которой уже нет.
    """
    if not bool(getattr(settings, "FUNDING_ARB_CONFIRM_ENABLED", True)):
        return {"ok": True, "reason": None, "skipped": "подтверждение выключено"}

    min_obs = int(getattr(settings, "FUNDING_ARB_MIN_OBSERVATIONS", 6))
    min_consistency = float(getattr(settings, "FUNDING_ARB_MIN_SIGN_CONSISTENCY", 0.85))
    # Имя отличается от FUNDING_ARB_MIN_HOLD_PERIODS намеренно — тот ключ про
    # ограничение выхода, а не про горизонт окупаемости. См. комментарий в config.
    min_hold = int(getattr(settings, "FUNDING_ARB_CONFIRM_HOLD_PERIODS", 10))
    stress_pct = float(getattr(settings, "FUNDING_ARB_BASIS_STRESS_PCT", 0.30))

    st = stability(symbol)
    n = int(st.get("observations") or 0)

    if n < min_obs:
        return {
            "ok": False,
            "reason": f"наблюдений {n} < {min_obs}: ставка не подтверждена",
            "stability": st,
        }

    consistency = float(st.get("sign_consistency") or 0.0)
    if consistency < min_consistency:
        return {
            "ok": False,
            "reason": (f"знак ставки менялся: положительна в {consistency:.0%} "
                       f"наблюдений < {min_consistency:.0%} — carry не гарантирован"),
            "stability": st,
        }

    # Ожидаемый carry за минимальное удержание, по консервативной ставке.
    conservative = float(st.get("conservative_rate_pct") or 0.0)
    gross = conservative * min_hold
    net = gross - fee_round_trip_pct
    if net <= 0:
        return {
            "ok": False,
            "reason": (f"за {min_hold} периодов по консервативной ставке "
                       f"{conservative:.4f}% доход {gross:.4f}% не покрывает "
                       f"издержки {fee_round_trip_pct:.4f}%"),
            "stability": st,
            "expected_net_carry_pct": round(net, 6),
        }

    # Стресс: базис расходится против нас на stress_pct за время удержания.
    # Позиция дельта-нейтральна, поэтому бьёт именно расхождение, а не движение
    # цены — и оно должно оставаться меньше накопленного carry.
    stressed = net - stress_pct
    if stressed <= 0:
        return {
            "ok": False,
            "reason": (f"стресс базиса {stress_pct}% съедает весь carry "
                       f"{net:.4f}% за {min_hold} периодов"),
            "stability": st,
            "expected_net_carry_pct": round(net, 6),
            "stressed_net_carry_pct": round(stressed, 6),
        }

    return {
        "ok": True,
        "reason": None,
        "stability": st,
        "conservative_rate_pct": conservative,
        "expected_net_carry_pct": round(net, 6),
        "stressed_net_carry_pct": round(stressed, 6),
        "min_hold_periods": min_hold,
    }
