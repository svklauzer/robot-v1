"""Зона входа с учётом стакана (#entry-zone-2026-07-27).

Что было. Зона входа строилась как `last × (1 ± 0.3%)` — от последней цены,
без единого взгляда на книгу. Depth-гейт существовал, но он отвечал только
«входить или нет»; КУДА ставить цену, он не влиял. В результате вход шёл
фактически по рынку в любых условиях книги, а издержки исполнения ложились
поверх и без того тонкого ожидания (+0.091% средняя победа против ~0.06%
round-trip комиссии на swap — исполнение здесь не мелочь, а половина результата).

Что делает этот модуль. Из снимка стакана считает три вещи:

  1. **Можно ли вообще брать по рынку.** Широкий спред, тонкая глубина в
     первых уровнях или встречный CVD — три разных способа заплатить за вход
     больше, чем он стоит.
  2. **Куда перенести цену**, если по рынку брать дорого: к микро-VWAP книги
     со стороны входа либо к ближайшему уровню-опоре (стенке). Лимит там
     ждёт рынок, а не гонится за ним.
  3. **Когда сигнал протух.** У перенесённого входа есть срок годности: цена,
     рассчитанная по книге минутной давности, к моменту исполнения уже не
     опирается ни на что.

Ключевое решение — «перенести», а не «отменить». Отмена по широкому спреду
выбрасывает сетап целиком; перенос сохраняет его и переносит риск в область,
которую мы контролируем: цену исполнения. Отменяем только когда перенос
уводит вход дальше допустимого — тогда сетап действительно потерял смысл.

Чистые функции: снимок стакана на вход, решение на выход. Без БД и сети.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import settings
from services.orderbook_analyzer import DepthSignal, OrderBookAnalyzer, _levels


@dataclass(frozen=True)
class EntryZoneDecision:
    mode: str                      # "market" | "limit_vwap" | "limit_wall" | "reject"
    allowed: bool
    entry_price: float | None      # куда ставить лимит (None = по рынку)
    entry_from: float | None
    entry_to: float | None
    drift_pct: float               # насколько ушли от last, в % (>0 = выгоднее нам)
    ttl_sec: float                 # срок годности решения
    reasons: list[str] = field(default_factory=list)
    depth: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allowed": self.allowed,
            "entry_price": self.entry_price,
            "entry_from": self.entry_from,
            "entry_to": self.entry_to,
            "drift_pct": round(self.drift_pct, 4),
            "ttl_sec": self.ttl_sec,
            "reasons": list(self.reasons),
            "depth": dict(self.depth),
        }


def micro_vwap(side: str, snapshot: dict | None, levels: int = 10) -> float | None:
    """VWAP первых уровней СО СТОРОНЫ, в которую мы входим.

    Для лонга это биды: мы хотим, чтобы нас налили те, кто уже стоит в
    покупке ниже рынка. Средневзвешенная по объёму цена этих уровней — та
    точка, где книга реально держит, а не где напечатана последняя сделка.
    """
    if not snapshot:
        return None
    book = _levels(snapshot.get("bids") if str(side).lower() == "long" else snapshot.get("asks"))
    book = book[:levels]
    vol = sum(x[1] for x in book)
    if not book or vol <= 0:
        return None
    return sum(p * a for p, a in book) / vol


def depth_thinness(side: str, snapshot: dict | None, levels: int = 5) -> float | None:
    """Доля объёма первых уровней от всей видимой глубины со своей стороны.

    Тонкая книга — это когда объём сосредоточен далеко: первые уровни пустые,
    и рыночный ордер проедет по ним мгновенно. Метрика от 0 (всё далеко) до 1.
    """
    if not snapshot:
        return None
    book = _levels(snapshot.get("bids") if str(side).lower() == "long" else snapshot.get("asks"))
    if not book:
        return None
    near = sum(a for _, a in book[:levels])
    total = sum(a for _, a in book)
    return (near / total) if total > 0 else None


def wall_price(side: str, snapshot: dict | None, levels: int = 10) -> float | None:
    """Цена крупнейшего уровня со своей стороны — опора, за которую ставим вход."""
    if not snapshot:
        return None
    book = _levels(snapshot.get("bids") if str(side).lower() == "long" else snapshot.get("asks"))
    book = book[:levels]
    if not book:
        return None
    return max(book, key=lambda x: x[1])[0]


def evaluate(
    *,
    side: str,
    last_price: float,
    snapshot: dict | None,
    regime: str | None = None,
    trade_mode: str | None = None,
) -> EntryZoneDecision:
    """Решение о цене входа по состоянию книги."""
    is_long = str(side).lower() == "long"
    last = float(last_price or 0.0)
    ttl = float(getattr(settings, "ENTRY_ZONE_TTL_SEC", 45.0))
    zone_pct = float(getattr(settings, "ENTRY_ZONE_WIDTH_PCT", 0.30)) / 100.0

    def _market(reasons: list[str], depth: dict) -> EntryZoneDecision:
        return EntryZoneDecision(
            mode="market", allowed=True, entry_price=None,
            entry_from=round(last * (1 - zone_pct), 8),
            entry_to=round(last * (1 + zone_pct), 8),
            drift_pct=0.0, ttl_sec=ttl, reasons=reasons, depth=depth,
        )

    if last <= 0:
        return _market(["нет цены — зона по умолчанию"], {})

    if not bool(getattr(settings, "ENTRY_ZONE_DEPTH_AWARE", True)):
        return _market(["depth-aware зона выключена"], {})

    sig: DepthSignal = OrderBookAnalyzer.analyze(snapshot)
    depth = {
        "fresh": sig.fresh, "spread_pct": sig.spread_pct, "obi": sig.obi,
        "cvd_ratio": sig.cvd_ratio, "cvd_trades": sig.cvd_trades,
    }

    # Фид молчит — переносить вход не на что. Это осознанный fail-open: движок
    # не должен вставать из-за фида. Но факт фиксируется, и по нему уже считается
    # отдельная статистика (/analytics/depth-coverage).
    if not sig.fresh:
        return _market(["стакан не свежий — вход по рынку без подтверждения"], depth)

    thin = depth_thinness(side, snapshot)
    depth["near_depth_share"] = round(thin, 4) if thin is not None else None

    max_spread = float(getattr(settings, "ENTRY_ZONE_MAX_MARKET_SPREAD_PCT", 0.05))
    min_near = float(getattr(settings, "ENTRY_ZONE_MIN_NEAR_DEPTH_SHARE", 0.25))
    adverse_cvd = float(getattr(settings, "ENTRY_ZONE_ADVERSE_CVD_RATIO", 0.25))
    min_trades = int(getattr(settings, "ENTRY_ZONE_CVD_MIN_TRADES", 20))

    problems: list[str] = []
    if sig.spread_pct is not None and sig.spread_pct > max_spread:
        problems.append(f"спред {sig.spread_pct:.3f}% > {max_spread}%")
    if thin is not None and thin < min_near:
        problems.append(f"тонкие первые уровни: {thin:.0%} объёма вблизи")
    # CVD учитываем только при достаточном числе сделок: на трёх сделках это шум.
    if sig.cvd_trades >= min_trades:
        against = (sig.cvd_ratio <= -adverse_cvd) if is_long else (sig.cvd_ratio >= adverse_cvd)
        if against:
            problems.append(f"поток против входа: CVD {sig.cvd_ratio:+.2f}")

    if not problems:
        return _market(["книга в порядке — вход по рынку допустим"], depth)

    # Переносим вход. Кандидаты: микро-VWAP своей стороны и цена крупнейшего
    # уровня. Берём БОЛЕЕ ВЫГОДНЫЙ для нас — вход должен ждать рынок, а не
    # догонять его.
    vwap = micro_vwap(side, snapshot)
    wall = wall_price(side, snapshot)
    candidates = [p for p in (vwap, wall) if p and p > 0]
    if not candidates:
        return _market(problems + ["перенести некуда — книга пуста"], depth)

    target = min(candidates) if is_long else max(candidates)
    mode = "limit_wall" if (wall and abs(target - wall) < 1e-12) else "limit_vwap"
    drift = ((last - target) / last * 100.0) if is_long else ((target - last) / last * 100.0)

    max_drift = float(getattr(settings, "ENTRY_ZONE_MAX_DRIFT_PCT", 0.60))
    if drift < 0:
        # Опора ХУЖЕ рынка — переносить бессмысленно, это не улучшение цены.
        return _market(problems + ["опора хуже рынка — остаёмся по рынку"], depth)
    if drift > max_drift:
        # Ближайшая опора слишком далеко: к моменту, когда цена туда дойдёт,
        # сетап будет уже другим. Вот это и есть настоящий повод отменить.
        return EntryZoneDecision(
            mode="reject", allowed=False, entry_price=None,
            entry_from=None, entry_to=None, drift_pct=drift, ttl_sec=ttl,
            reasons=problems + [
                f"ближайшая опора в {drift:.2f}% от рынка (потолок {max_drift}%) — "
                "цена дойдёт туда уже с другим сетапом"
            ],
            depth=depth,
        )

    half = float(getattr(settings, "ENTRY_ZONE_LIMIT_WIDTH_PCT", 0.10)) / 100.0
    return EntryZoneDecision(
        mode=mode, allowed=True, entry_price=round(target, 8),
        entry_from=round(target * (1 - half), 8),
        entry_to=round(target * (1 + half), 8),
        drift_pct=drift, ttl_sec=ttl,
        reasons=problems + [
            f"вход перенесён к {'стенке' if mode == 'limit_wall' else 'микро-VWAP'} "
            f"{target:.8f} ({drift:+.2f}% к рынку), годен {ttl:.0f} с"
        ],
        depth=depth,
    )


def is_stale(decision: EntryZoneDecision, age_sec: float) -> bool:
    """Протух ли перенесённый вход.

    Рыночный вход не протухает — он берёт то, что есть. Лимит, посчитанный по
    книге минутной давности, к моменту исполнения не опирается ни на что: та
    стенка, ради которой перенос и делался, уже могла уйти.
    """
    if decision.mode == "market":
        return False
    return float(age_sec) > float(decision.ttl_sec)
