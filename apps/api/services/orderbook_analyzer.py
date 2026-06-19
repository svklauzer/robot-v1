"""OrderBookAnalyzer — чистые функции над снимком стакана и лентой сделок.

Мы НЕ HFT (нет колокации, FPGA, kernel bypass). Считаем устойчивые дисбалансы,
живущие секунды-минуты, и используем их как ПОДТВЕРЖДЕНИЕ/фильтр для наших
range-входов и ускоритель скальп-выхода — не как самостоятельный триггер.

Все функции принимают простые списки уровней [[price, amount], ...] (bids по
убыванию цены, asks по возрастанию) и список сделок [{"side","amount"}...], так
что модуль тестируется без ccxt/websocket.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class DepthSignal:
    fresh: bool                 # есть ли свежие данные стакана
    spread_pct: float | None    # (ask-bid)/mid * 100
    mid: float | None
    obi: float                  # order book imbalance, -1..1 (+ = давление бидов)
    bid_wall_share: float       # доля крупнейшего бид-уровня в топ-N
    ask_wall_share: float
    cvd: float                  # cumulative volume delta за окно (+ = агрессивные покупки)
    cvd_ratio: float            # cvd / суммарный объём окна, -1..1
    cvd_trades: int             # число сделок в окне (мало → CVD не сигнал)

    def as_dict(self) -> dict:
        return asdict(self)


def _levels(side) -> list[tuple[float, float]]:
    out = []
    for lvl in side or []:
        try:
            p = float(lvl[0]); a = float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p > 0 and a >= 0:
            out.append((p, a))
    return out


class OrderBookAnalyzer:
    @staticmethod
    def spread_pct(bids, asks):
        b = _levels(bids); a = _levels(asks)
        if not b or not a:
            return None, None
        best_bid = b[0][0]; best_ask = a[0][0]
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0 or best_ask < best_bid:
            return None, mid if mid > 0 else None
        return (best_ask - best_bid) / mid * 100.0, mid

    @staticmethod
    def imbalance(bids, asks, levels: int = 10) -> float:
        b = _levels(bids)[:levels]; a = _levels(asks)[:levels]
        bv = sum(x[1] for x in b); av = sum(x[1] for x in a)
        tot = bv + av
        return 0.0 if tot <= 0 else (bv - av) / tot

    @staticmethod
    def wall_share(side, levels: int = 10) -> float:
        s = _levels(side)[:levels]
        vol = [x[1] for x in s]
        tot = sum(vol)
        return 0.0 if tot <= 0 else max(vol) / tot

    @staticmethod
    def cvd(trades):
        delta = 0.0; total = 0.0; n = 0
        for t in trades or []:
            try:
                amt = float(t.get("amount", 0.0))
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            side = str(t.get("side", "")).lower()
            total += amt
            delta += amt if side == "buy" else -amt if side == "sell" else 0.0
            n += 1
        ratio = 0.0 if total <= 0 else delta / total
        return delta, ratio, n

    @classmethod
    def analyze(cls, snapshot: dict | None, levels: int = 10) -> DepthSignal:
        if not snapshot:
            return DepthSignal(False, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
        bids = snapshot.get("bids"); asks = snapshot.get("asks")
        trades = snapshot.get("trades", [])
        spread, mid = cls.spread_pct(bids, asks)
        obi = cls.imbalance(bids, asks, levels)
        bid_wall = cls.wall_share(bids, levels)
        ask_wall = cls.wall_share(asks, levels)
        cvd_v, cvd_r, cvd_n = cls.cvd(trades)
        fresh = spread is not None
        return DepthSignal(fresh, spread, mid, obi, bid_wall, ask_wall, cvd_v, cvd_r, cvd_n)

    # ── Подтверждение ВХОДА ───────────────────────────────────────────────────
    @classmethod
    def entry_gate(cls, side: str, sig: DepthSignal, *,
                   max_spread_pct: float, obi_confirm: float,
                   wall_confirm: float,
                   cvd_block_ratio: float = 0.0,
                   cvd_min_trades: int = 0) -> tuple[bool, str]:
        """Возвращает (allowed, reason). Если данных нет (not fresh) — пропускаем
        (allowed=True, reason="no_depth_data"): движок не должен блокировать
        торговлю при отсутствии WS-потока.

        CVD-фильтр: при cvd_block_ratio>0 и достаточной выборке (cvd_trades >=
        cvd_min_trades) НЕ входим против агрессивного исполненного потока —
        не шортим в доминирующие покупки, не лонгуем в доминирующие продажи.
        cvd_block_ratio=0 (дефолт) → CVD на входе выключен (обратная совместимость)."""
        if not sig.fresh:
            return True, "no_depth_data"
        if sig.spread_pct is not None and sig.spread_pct > max_spread_pct:
            return False, f"depth_spread_too_wide:{sig.spread_pct:.3f}>{max_spread_pct}"
        s = str(side).lower()

        # CVD-подтверждение входа: поток уже исполненных сделок не должен идти
        # против входа (только при надёжной выборке — иначе CVD это шум).
        if cvd_block_ratio and cvd_block_ratio > 0 and sig.cvd_trades >= int(cvd_min_trades):
            if s in ("long", "buy") and sig.cvd_ratio <= -abs(cvd_block_ratio):
                return False, f"depth_cvd_against_long:cvd_ratio={sig.cvd_ratio:.3f}<=-{abs(cvd_block_ratio)}"
            if s in ("short", "sell") and sig.cvd_ratio >= abs(cvd_block_ratio):
                return False, f"depth_cvd_against_short:cvd_ratio={sig.cvd_ratio:.3f}>={abs(cvd_block_ratio)}"

        if s in ("long", "buy"):
            if sig.obi < obi_confirm and sig.bid_wall_share < wall_confirm:
                return False, f"depth_no_bid_support:obi={sig.obi:.3f}"
            return True, f"depth_long_ok:obi={sig.obi:.3f}"
        if s in ("short", "sell"):
            if sig.obi > -obi_confirm and sig.ask_wall_share < wall_confirm:
                return False, f"depth_no_ask_pressure:obi={sig.obi:.3f}"
            return True, f"depth_short_ok:obi={sig.obi:.3f}"
        return True, "depth_side_unknown"

    # ── Ускоритель ВЫХОДА (поток против позиции) ──────────────────────────────
    @classmethod
    def flow_against(cls, side: str, sig: DepthSignal, *, cvd_exit_ratio: float,
                     min_trades: int = 15) -> bool:
        """True, если агрессивный поток сделок развернулся ПРОТИВ позиции.
        На тонкой выборке (< min_trades сделок) CVD — шум, сигнал не даём."""
        if not sig.fresh or sig.cvd_trades < int(min_trades):
            return False
        s = str(side).lower()
        if s in ("long", "buy"):
            return sig.cvd_ratio <= -abs(cvd_exit_ratio)
        if s in ("short", "sell"):
            return sig.cvd_ratio >= abs(cvd_exit_ratio)
        return False
