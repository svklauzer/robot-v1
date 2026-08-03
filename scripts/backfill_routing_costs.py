#!/usr/bin/env python3
"""Пересчёт исторических издержек по ФАКТИЧЕСКОМУ маршруту сделки.

(#cost-backfill-2026-07-30)

Что чинится
-----------
До правки `market_routing` (28.07) рынок сделки брался из одной глобальной
настройки, а комиссия — из спотового источника HTX. В результате 287 из 302
закрытых сделок записаны с round-trip издержками ~0.446% от нотионала, тогда
как маршрут этих же сделок — своп с taker 0.05% и полным round-trip 0.15%.

Цена ошибки в деньгах:

    валовый P&L (до издержек)        +37.96 USDT
    записанные издержки             −146.64 USDT
    записанный net                  −108.68 USDT
    net по фактическому маршруту     −30.76 USDT

То есть три четверти «убытка» системы — это комиссия, которой она не платила.
Пока история не пересчитана, от завышенной себестоимости питаются
`validation_gates`, `symbol_performance_guard`, expectancy-разрезы, метки
`is_win` в ML-датасете и решение о выходе в live. Ждать, пока 300 сделок
выкатятся из окна естественным образом, — это недели.

Как считается
-------------
Пропорциональное восстановление, а не пересбор с нуля. Для каждой сделки:

    booked_rt = closed_total_cost / entry_notional      (фактически записанная ставка)
    target_rt = 2·taker + slippage + funding·periods    (ставка её маршрута)
    new_cost  = closed_total_cost · target_rt / booked_rt
    new_net   = (closed_net_pnl + closed_total_cost) − new_cost

Почему пропорция, а не прямой пересчёт по CostEngine: у сделок с частичной
фиксацией TP1 три ноги исполнения, а не две, и точный состав ног в истории не
сохранён. Пропорция переносит структуру издержек как есть и меняет только
ставку. Погрешность даёт слипидж (0.02% из 0.44%, он от ставки не зависит) —
около 4% от величины поправки, в консервативную сторону: издержки остаются
слегка ЗАВЫШЕННЫМИ.

Трогаются только сделки, у которых:
  * нет `plan_json.routing` (то есть закрыты до правки маршрутизации), и
  * записанная ставка похожа на спотовую, и
  * маршрут по стороне сделки разрешается в своп.

Сделка с уже записанным routing не трогается никогда: её издержки посчитаны
правильно, и «поправить» их означало бы переписать честные данные.

Обратимость
-----------
Оригинальные значения складываются в `plan_json.cost_restatement` вместе с
меткой скрипта. Откат — `--revert`. Без `--apply` скрипт ничего не пишет.

Использование
-------------
    python scripts/backfill_routing_costs.py                     # dry-run по БД
    python scripts/backfill_routing_costs.py --from-export f.json # dry-run по выгрузке
    python scripts/backfill_routing_costs.py --apply             # запись в БД
    python scripts/backfill_routing_costs.py --revert --apply    # откат
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TAG = "cost-backfill-2026-07-30"

# Ставки берём из настроек, если доступны; иначе — дефолты config.py.
DEFAULTS = {
    "SPOT_TAKER_FEE": 0.002,
    "FUTURES_TAKER_FEE": 0.0005,
    "SLIPPAGE_BUFFER_PCT": 0.0002,
    "FUNDING_BUFFER_PCT": 0.0003,
}


def _settings():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))
        from core.config import settings  # type: ignore
        return settings
    except Exception:  # noqa: BLE001
        return None


def round_trip_rates(settings) -> dict[str, float]:
    def _get(name: str) -> float:
        if settings is not None:
            try:
                return float(getattr(settings, name))
            except Exception:  # noqa: BLE001
                pass
        return DEFAULTS[name]

    slip = _get("SLIPPAGE_BUFFER_PCT")
    return {
        "spot": 2 * _get("SPOT_TAKER_FEE") + slip,
        "swap": 2 * _get("FUTURES_TAKER_FEE") + slip + _get("FUNDING_BUFFER_PCT"),
    }


def route_market_type(side: str, settings, long_market: str = "auto") -> str:
    """Маршрут сделки по её стороне — та же логика, что в market_routing.resolve.

    Шорт всегда своп: продать на споте то, чего нет, нельзя — это свойство
    рынка, а не настройки. Лонг зависит от `ENABLE_FUTURES_EXECUTION`, и вот
    здесь ловушка: в `config.py` дефолт False, а в окружении Render — true.
    Запуск скрипта с локальными настройками отнёс бы все лонги к споту, счёл
    их издержки корректными и пересчитал только шорты (127 сделок вместо 174,
    поправка +52.96 вместо +76.98) — молча и без единой ошибки.

    Поэтому маршрут лонга задаётся явно и печатается в отчёте.
    """
    if str(side or "").lower() in ("short", "sell"):
        return "swap"
    value = str(long_market or "auto").lower()
    if value in ("spot", "swap"):
        return value
    prefer_futures = True
    if settings is not None:
        prefer_futures = bool(getattr(settings, "ENABLE_FUTURES_EXECUTION", False))
    return "swap" if prefer_futures else "spot"


def entry_notional(row: dict) -> float:
    plan = row.get("plan_json") or row.get("plan") or {}
    lifecycle = (plan or {}).get("lifecycle") or {}
    price = lifecycle.get("entry_price")
    qty = plan.get("qty") or row.get("qty")
    try:
        value = float(price) * float(qty)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def plan_of(row: dict) -> dict:
    return row.get("plan_json") or row.get("plan") or {}


def analyse(row: dict, rates: dict[str, float], settings, long_market: str = "auto") -> dict | None:
    """Что нужно изменить у одной сделки. None — трогать не нужно."""
    plan = plan_of(row)
    cost = row.get("closed_total_cost")
    net = row.get("closed_net_pnl")
    if cost is None or net is None:
        return None
    try:
        cost = float(cost)
        net = float(net)
    except (TypeError, ValueError):
        return None
    if cost <= 0:
        return None

    if isinstance(plan.get("routing"), dict) and plan["routing"].get("market_type"):
        return None  # маршрут записан — издержки уже честные

    notional = entry_notional(row)
    if notional <= 0:
        return None

    booked_rt = cost / notional
    target = route_market_type(row.get("side"), settings, long_market)
    target_rt = rates[target]

    # Записанная ставка должна быть похожа на спотовую, а не просто «больше
    # целевой»: иначе под пересчёт попадут сделки с честной, но крупной
    # структурой издержек (несколько ног фиксации).
    if booked_rt < rates["spot"] * 0.8:
        return None
    if booked_rt <= target_rt * 1.2:
        return None

    new_cost = cost * target_rt / booked_rt
    gross = net + cost
    new_net = gross - new_cost

    # result_pct и lifecycle.final_result_pct записаны УЖЕ НЕТТО: сверка по 302
    # сделкам даёт `closed_net_pnl == notional × final_result_pct/100` с
    # точностью до округления. Если пересчитать только closed_net_pnl, эти два
    # поля останутся от старой себестоимости — и разойдутся с деньгами. От них
    # питаются детектор фантомных филлов (сравнивает result_pct с mfe_pct),
    # exit-replay и ML-метки, поэтому чинить надо все три вместе.
    old_result_pct = row.get("result_pct")
    if old_result_pct is None:
        old_result_pct = (row.get("plan_json") or row.get("plan") or {}).get(
            "lifecycle", {}
        ).get("final_result_pct")
    new_result_pct = round(new_net / notional * 100, 6) if notional > 0 else None

    return {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "target_market_type": target,
        "booked_rt_pct": round(booked_rt * 100, 4),
        "target_rt_pct": round(target_rt * 100, 4),
        "notional": round(notional, 4),
        "gross": round(gross, 6),
        "old_cost": round(cost, 6),
        "new_cost": round(new_cost, 6),
        "old_net": round(net, 6),
        "new_net": round(new_net, 6),
        "old_result_pct": old_result_pct,
        "new_result_pct": new_result_pct,
    }


def report(changes: list[dict], scanned: int) -> None:
    if not changes:
        print(f"Пересчитывать нечего: просмотрено {scanned}, подходящих 0.")
        return
    old_net = sum(c["old_net"] for c in changes)
    new_net = sum(c["new_net"] for c in changes)
    old_cost = sum(c["old_cost"] for c in changes)
    new_cost = sum(c["new_cost"] for c in changes)
    print(f"Просмотрено закрытых сделок : {scanned}")
    print(f"Под пересчёт                : {len(changes)}")
    print(f"Издержки  {old_cost:10.2f} → {new_cost:8.2f} USDT   (освобождается {old_cost - new_cost:.2f})")
    print(f"Net PnL   {old_net:10.2f} → {new_net:8.2f} USDT   (поправка {new_net - old_net:+.2f})")
    wins_old = sum(1 for c in changes if c["old_net"] > 0)
    wins_new = sum(1 for c in changes if c["new_net"] > 0)
    print(f"Прибыльных {wins_old} → {wins_new} из {len(changes)} "
          f"({100 * wins_old / len(changes):.1f}% → {100 * wins_new / len(changes):.1f}%)")
    print("\nПервые 10:")
    for c in changes[:10]:
        print(f"  #{c['id']:<4} {c['symbol']:<10} {c['side']:<5} "
              f"rt {c['booked_rt_pct']:.3f}%→{c['target_rt_pct']:.3f}%  "
              f"net {c['old_net']:+.3f}→{c['new_net']:+.3f}")


def run_export(path: str, rates: dict, settings, long_market: str) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("items") if isinstance(payload, dict) else payload
    rows = [r for r in (rows or []) if r.get("status") == "closed"]
    changes = [c for c in (analyse(r, rates, settings, long_market) for r in rows) if c]
    report(changes, len(rows))
    return changes


def _db_hint(settings) -> str:
    """Куда скрипт пытался подключиться — без пароля."""
    try:
        import re

        url = str(getattr(settings, "database_url", "") or "")
        return re.sub(r"//[^@/]*@", "//***@", url) or "(DATABASE_URL не задан)"
    except Exception:  # noqa: BLE001
        return "(не удалось прочитать DATABASE_URL)"


def run_db(rates: dict, settings, *, apply: bool, revert: bool, long_market: str) -> list[dict]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))
    from core.db import SessionLocal  # type: ignore
    from models.signal import Signal  # type: ignore
    from sqlalchemy.orm.attributes import flag_modified  # type: ignore

    db = SessionLocal()
    try:
        # Подключение проверяем отдельно: на русской Windows libpq отдаёт текст
        # ошибки в cp1251, psycopg2 читает его как UTF-8 и падает на
        # UnicodeDecodeError — настоящая причина при этом теряется.
        try:
            from sqlalchemy import text

            db.execute(text("SELECT 1"))
        except UnicodeDecodeError:
            print(
                "Не удалось подключиться к БД, а текст ошибки libpq не читается "
                "(кириллица в cp1251 против UTF-8).\n"
                f"  DSN: {_db_hint(settings)}\n"
                "  Чтобы увидеть настоящую причину: $env:PGCLIENTENCODING = \"UTF8\"\n"
                "  Частая причина — внутренний хост Render в .env: он резолвится "
                "только внутри Render.\n"
                "  Варианты: внешний DATABASE_URL из дашборда, запуск из шелла "
                "сервиса на Render, либо работа по выгрузке через --from-export.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось подключиться к БД: {exc}\n  DSN: {_db_hint(settings)}",
                  file=sys.stderr)
            raise SystemExit(2)

        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed", Signal.closed_net_pnl.isnot(None))
            .order_by(Signal.id.asc())
            .all()
        )

        if revert:
            restored = 0
            for signal in signals:
                plan = signal.plan_json or {}
                saved = plan.get("cost_restatement")
                if not isinstance(saved, dict) or saved.get("tag") != TAG:
                    continue
                signal.closed_total_cost = saved["old_cost"]
                signal.closed_net_pnl = saved["old_net"]
                if saved.get("old_result_pct") is not None:
                    signal.result_pct = saved["old_result_pct"]
                    lifecycle = plan.get("lifecycle")
                    if isinstance(lifecycle, dict):
                        lifecycle["final_result_pct"] = saved["old_result_pct"]
                plan.pop("cost_restatement", None)
                signal.plan_json = plan
                flag_modified(signal, "plan_json")
                restored += 1
            print(f"Откат: восстановлено {restored} сделок.")
            if apply:
                db.commit()
                print("Записано в БД.")
            else:
                db.rollback()
                print("DRY-RUN: изменения не сохранены (--apply для записи).")
            return []

        changes: list[dict] = []
        for signal in signals:
            row = {
                "id": signal.id,
                "symbol": signal.symbol,
                "side": signal.side,
                "qty": signal.qty,
                "plan_json": signal.plan_json,
                "closed_total_cost": signal.closed_total_cost,
                "closed_net_pnl": signal.closed_net_pnl,
            }
            change = analyse(row, rates, settings, long_market)
            if not change:
                continue
            changes.append(change)
            if not apply:
                continue
            plan = signal.plan_json or {}
            plan["cost_restatement"] = {
                "tag": TAG,
                "old_cost": float(signal.closed_total_cost),
                "old_net": float(signal.closed_net_pnl),
                "old_result_pct": change["old_result_pct"],
                "booked_rt_pct": change["booked_rt_pct"],
                "target_rt_pct": change["target_rt_pct"],
                "target_market_type": change["target_market_type"],
                "note": "издержки восстановлены по фактическому маршруту сделки",
            }
            # result_pct тянется вместе с деньгами: детектор фантомных филлов
            # сравнивает его с mfe_pct, и рассинхрон дал бы ложные срабатывания.
            if change["new_result_pct"] is not None:
                signal.result_pct = change["new_result_pct"]
                lifecycle = plan.get("lifecycle")
                if isinstance(lifecycle, dict):
                    lifecycle["final_result_pct"] = change["new_result_pct"]
            signal.plan_json = plan
            flag_modified(signal, "plan_json")
            signal.closed_total_cost = change["new_cost"]
            signal.closed_net_pnl = change["new_net"]

        report(changes, len(signals))
        if apply:
            db.commit()
            print("\nЗаписано в БД. Откат: --revert --apply")
        else:
            db.rollback()
            print("\nDRY-RUN: изменения не сохранены (--apply для записи).")
        return changes
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="записать изменения (по умолчанию dry-run)")
    parser.add_argument("--revert", action="store_true", help="откатить предыдущий пересчёт")
    parser.add_argument("--from-export", dest="export", help="считать по JSON-выгрузке вместо БД")
    parser.add_argument("--long-market", choices=("auto", "spot", "swap"), default="auto",
                        help="рынок лонга: auto — из ENABLE_FUTURES_EXECUTION текущего окружения. "
                             "В Render эта переменная true, в дефолтах config.py false — "
                             "при запуске вне прода задавайте swap явно")
    args = parser.parse_args()

    settings = _settings()
    rates = round_trip_rates(settings)
    resolved_long = route_market_type("long", settings, args.long_market)
    print(f"Round-trip ставки: spot {rates['spot'] * 100:.3f}%  swap {rates['swap'] * 100:.3f}%")
    print(f"Маршрут ЛОНГА: {resolved_long}"
          + (f"  (--long-market {args.long_market})" if args.long_market != "auto"
             else "  (auto из ENABLE_FUTURES_EXECUTION; в проде эта переменная true)"))
    print("Маршрут ШОРТА: swap (на споте шорт невозможен)\n")

    if args.export:
        if args.apply or args.revert:
            print("--from-export работает только в режиме чтения.", file=sys.stderr)
            return 2
        run_export(args.export, rates, settings, args.long_market)
        return 0

    run_db(rates, settings, apply=args.apply, revert=args.revert, long_market=args.long_market)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
