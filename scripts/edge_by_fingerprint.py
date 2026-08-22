"""Edge по поколениям конфига (#edge-by-fingerprint-2026-08-22).

Зачем
-----
Настройки правятся чаще, чем копится статистика. 13 закрытых на 22.08 выглядели
одной выборкой, а на деле были ДВУМЯ конфигурациями: #416–#417 с
`tz_adx_min=18`, #418–#428 с `tz_adx_min=15`. Сложить их вместе — значит
измерить среднее по разным системам и решить, что измерил одну.

`decision_config` пишет снимок настроек в каждый сигнал вместе с коротким
`fingerprint`. Скрипт группирует закрытые сделки по этому отпечатку и считает
edge ВНУТРИ поколения, а не поперёк.

Что показывает
--------------
* n, чистый и валовый P&L, победы/поражения, среднее на сделку;
* проверку знака по половинам выборки — та самая дисциплина walk-forward:
  разрез, меняющий знак между половинами, это шум, а не находка;
* ЧЕМ поколения отличаются друг от друга — построчный диф настроек.

Валовый = чистый + издержки. Издержки считаются РОВНО ОДИН раз с каждой
стороны: `closed_net_pnl` уже нетто, `closed_total_cost` — то, что из него
вычли. Смешение единиц однажды уже дало ложноположительный результат на целой
гипотезе, поэтому обе величины берутся из одной строки.

Запуск:
    python scripts/edge_by_fingerprint.py analytics_24h/signals_export_*.json
    python scripts/edge_by_fingerprint.py <export> --min-sample 30
    python scripts/edge_by_fingerprint.py <export> --by-engine
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", data) if isinstance(data, dict) else data
    return [x for x in items if isinstance(x, dict)]


def _plan(row: dict) -> dict:
    plan = row.get("plan") or row.get("plan_json") or {}
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError:
            return {}
    return plan if isinstance(plan, dict) else {}


def _config(row: dict) -> dict:
    cfg = _plan(row).get("config")
    return cfg if isinstance(cfg, dict) else {}


def _fingerprint(row: dict) -> str | None:
    fp = _config(row).get("fingerprint")
    return str(fp) if fp else None


def _engine(row: dict) -> str:
    """Движок по rationale/причине закрытия — теми же правилами, что в разборах."""
    text = f"{row.get('rationale') or ''} {row.get('entry_reason') or ''}".lower()
    reason = str(row.get("closed_reason") or "").lower()
    regime = str(_plan(row).get("regime") or row.get("regime") or "").lower()
    for key in ("scalp", "crt", "range", "reversal"):
        if key in regime or key in text or key in reason:
            return key
    if "trend" in regime or "breakout" in text or "breakdown" in text or "trend" in text:
        return "trend"
    return "other"


def _flatten(cfg: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in cfg.items():
        if key == "fingerprint":
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{name}."))
        else:
            out[name] = value
    return out


def _money(row: dict) -> tuple[float, float] | None:
    """(чистый, валовый). None — сделка без результата."""
    net = row.get("closed_net_pnl")
    if net is None:
        return None
    try:
        net = float(net)
        cost = float(row.get("closed_total_cost") or 0.0)
    except (TypeError, ValueError):
        return None
    return net, net + cost


def _stats(rows: list[dict]) -> dict:
    nets, grosses = [], []
    for row in rows:
        money = _money(row)
        if money is None:
            continue
        nets.append(money[0])
        grosses.append(money[1])

    n = len(nets)
    if n == 0:
        return {"n": 0}

    half = n // 2
    return {
        "n": n,
        "net": sum(nets),
        "gross": sum(grosses),
        "wins": sum(1 for x in nets if x > 0),
        "losses": sum(1 for x in nets if x < 0),
        "gross_per_trade": sum(grosses) / n,
        "first_half": sum(grosses[:half]) if half else 0.0,
        "second_half": sum(grosses[half:]),
        "sign_stable": (
            None if half == 0
            else (sum(grosses[:half]) > 0) == (sum(grosses[half:]) > 0)
        ),
    }


def _print_group(title: str, st: dict, min_sample: int) -> None:
    if st["n"] == 0:
        print(f"  {title:<26} нет закрытых сделок")
        return
    stable = st["sign_stable"]
    mark = "—" if stable is None else ("ДА" if stable else "НЕТ")
    print(
        f"  {title:<26} n={st['n']:<4} "
        f"чистый {st['net']:+8.2f}  валовый {st['gross']:+8.2f}  "
        f"{st['wins']}W/{st['losses']}L  "
        f"на сделку {st['gross_per_trade']:+6.3f}  "
        f"половины {st['first_half']:+7.2f}/{st['second_half']:+7.2f}  знак {mark}"
    )
    if st["n"] < min_sample:
        print(
            f"  {'':<26} !! выборки мало ({st['n']} < {min_sample}): "
            "числа показаны для наблюдения, вывод по ним не делается"
        )
    elif stable is False:
        print(
            f"  {'':<26} !! знак меняется между половинами — это шум, а не находка"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="JSON-выгрузка сигналов (scripts/export_signals.ps1)")
    ap.add_argument("--min-sample", type=int, default=30,
                    help="сколько сделок считать достаточным для вывода")
    ap.add_argument("--by-engine", action="store_true",
                    help="дополнительно разложить каждое поколение по движкам")
    args = ap.parse_args()

    rows = _load(Path(args.export))
    closed = [r for r in rows if str(r.get("status")) == "closed" and _money(r)]
    closed.sort(key=lambda r: (str(r.get("created_at") or ""), r.get("id") or 0))

    print(f"Закрытых сделок в выгрузке: {len(closed)}")

    no_fp = [r for r in closed if not _fingerprint(r)]
    if no_fp:
        print(f"  без отпечатка конфига: {len(no_fp)} — сделаны до #decision-config, "
              "в разрез по поколениям не попадут")

    groups: dict[str, list[dict]] = {}
    for row in closed:
        fp = _fingerprint(row)
        if fp:
            groups.setdefault(fp, []).append(row)

    if not groups:
        print("\nОтпечатков конфига нет — выгрузка старая. Разрез невозможен.")
        return 1

    # Поколения в хронологическом порядке первой сделки.
    order = sorted(groups, key=lambda fp: str(groups[fp][0].get("created_at") or ""))

    print(f"\nПоколений конфига: {len(order)}")
    print("=" * 120)
    for fp in order:
        rows_fp = groups[fp]
        first = str(rows_fp[0].get("created_at") or "")[:16]
        last = str(rows_fp[-1].get("created_at") or "")[:16]
        print(f"\n[{fp}]  {first} → {last}")
        _print_group("всего", _stats(rows_fp), args.min_sample)

        if args.by_engine:
            by_engine: dict[str, list[dict]] = {}
            for row in rows_fp:
                by_engine.setdefault(_engine(row), []).append(row)
            for engine in sorted(by_engine):
                _print_group(f"  движок {engine}", _stats(by_engine[engine]),
                             args.min_sample)

    # ── чем поколения отличаются ────────────────────────────────────────────
    # Это и есть ответ на «а что вообще менялось»: без дифа отпечаток —
    # просто хэш, по которому нельзя понять, что именно проверяем.
    print("\n" + "=" * 120)
    print("ОТЛИЧИЯ МЕЖДУ ПОКОЛЕНИЯМИ (только изменившиеся ключи)")
    flat = {fp: _flatten(_config(groups[fp][0])) for fp in order}
    keys = sorted({k for f in flat.values() for k in f})
    changed = [k for k in keys if len({repr(flat[fp].get(k)) for fp in order}) > 1]

    if not changed:
        print("  настройки идентичны — отпечатки различаются по полю вне config")
    else:
        width = max(len(k) for k in changed)
        header = " " * (width + 2) + "  ".join(fp[:8].ljust(10) for fp in order)
        print("  " + header)
        for key in changed:
            cells = "  ".join(str(flat[fp].get(key, "—"))[:10].ljust(10) for fp in order)
            print(f"  {key.ljust(width)}  {cells}")

    print("\nВаловый = чистый + издержки; издержки учтены ровно один раз.")
    print("Разрез, меняющий знак между половинами, — шум, а не находка.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
