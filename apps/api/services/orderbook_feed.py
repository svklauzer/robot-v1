"""HTX WebSocket market-data feed → in-memory order book + trade flow store.

Подписываемся на market.<sym>.depth.step0 (полный снимок топ-уровней, push раз
в ~100мс-1с) и market.<sym>.trade.detail (лента сделок). HTX шлёт gzip-сжатые
сообщения и периодический {"ping": ts} — отвечаем {"pong": ts}. При обрыве —
реконнект с бэкоффом. Всё под флагом ENABLE_ORDERBOOK_ENGINE; если фид молчит,
снимок отдаёт None → анализатор уходит в pass-through, торговля как обычно.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import time

from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)


class OrderBookStore:
    """Потокобезопасно в рамках одного asyncio-loop (обычные dict-операции)."""

    def __init__(self, trades_window_sec: float = 60.0, max_trades: int = 800):
        self._books: dict[str, tuple] = {}
        self._book_ts: dict[str, float] = {}
        self._trades: dict[str, list] = {}
        self.trades_window_sec = float(trades_window_sec)
        self.max_trades = int(max_trades)

    def update_book(self, symbol: str, bids, asks) -> None:
        if not bids and not asks:
            return
        self._books[symbol] = (bids or [], asks or [])
        self._book_ts[symbol] = time.time()

    def add_trades(self, symbol: str, trades: list) -> None:
        if not trades:
            return
        now = time.time()
        buf = self._trades.setdefault(symbol, [])
        for t in trades:
            buf.append({"side": t.get("side"), "amount": t.get("amount"), "t": now})
        cutoff = now - self.trades_window_sec
        self._trades[symbol] = [x for x in buf if x["t"] >= cutoff][-self.max_trades:]

    def snapshot(self, symbol: str) -> dict | None:
        if symbol not in self._books:
            return None
        bids, asks = self._books[symbol]
        return {
            "bids": bids,
            "asks": asks,
            "trades": self._trades.get(symbol, []),
            "age_sec": time.time() - self._book_ts.get(symbol, 0.0),
        }

    def stats(self) -> dict:
        return {
            "symbols": sorted(self._books.keys()),
            "books": len(self._books),
            "freshest_age_sec": min(
                (time.time() - ts for ts in self._book_ts.values()), default=None
            ),
        }


# Глобальный синглтон — общий для WS-таска и стратегии/выхода.
ORDERBOOK_STORE = OrderBookStore(
    trades_window_sec=float(getattr(settings, "OB_CVD_WINDOW_SEC", 60)),
)


def _ws_symbol(ccxt_symbol: str) -> str:
    return ccxt_symbol.replace("/", "").replace(":USDT", "").lower()


async def run_htx_orderbook_feed(symbols, enabled_fn, store: OrderBookStore | None = None):
    """Запускается как фоновый таск. enabled_fn() -> bool управляет жизненным циклом."""
    try:
        import websockets  # локальный импорт: нет websockets → движок просто не стартует
    except Exception as exc:  # noqa: BLE001
        log_event(logger, 40, "ob_feed_no_websockets", error=str(exc))
        return

    store = store or ORDERBOOK_STORE
    ws_url = str(getattr(settings, "OB_WS_URL", "wss://api-aws.huobi.pro/ws"))
    sym_map = {_ws_symbol(s): s for s in symbols}
    backoff = 2.0
    # Watchdog: HTX шлёт ping/данные часто; тишина дольше этого = «мёртвый» сокет
    # (бывает без close-frame → ConnectionClosedError). Проактивно реконнектимся.
    read_timeout = float(getattr(settings, "OB_WS_READ_TIMEOUT_SEC", 30.0))
    # Ожидаемые разрывы соединения (реконнект) — это НЕ ошибка приложения.
    _Closed = getattr(websockets, "ConnectionClosed", ())

    while enabled_fn():
        try:
            async with websockets.connect(ws_url, ping_interval=None, max_size=2 ** 23) as ws:
                for wsym in sym_map:
                    await ws.send(json.dumps({"sub": f"market.{wsym}.depth.step0", "id": f"d_{wsym}"}))
                    await ws.send(json.dumps({"sub": f"market.{wsym}.trade.detail", "id": f"t_{wsym}"}))
                log_event(logger, 20, "ob_feed_connected", url=ws_url, symbols=len(sym_map))
                backoff = 2.0

                while enabled_fn():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
                    except asyncio.TimeoutError:
                        # тишина дольше read_timeout → сокет завис, реконнект
                        log_event(logger, 30, "ob_feed_stale_reconnect", timeout_sec=read_timeout)
                        break
                    try:
                        data = gzip.decompress(raw) if isinstance(raw, (bytes, bytearray)) else raw.encode()
                        msg = json.loads(data)
                    except Exception:  # noqa: BLE001
                        continue

                    if "ping" in msg:
                        await ws.send(json.dumps({"pong": msg["ping"]}))
                        continue

                    ch = msg.get("ch", "")
                    tick = msg.get("tick") or {}
                    if not ch or not tick:
                        continue
                    parts = ch.split(".")
                    if len(parts) < 3:
                        continue
                    sym = sym_map.get(parts[1])
                    if not sym:
                        continue

                    if ".depth." in ch:
                        store.update_book(sym, tick.get("bids", []), tick.get("asks", []))
                    elif ".trade.detail" in ch:
                        trades = [
                            {"side": d.get("direction"), "amount": d.get("amount")}
                            for d in tick.get("data", [])
                        ]
                        store.add_trades(sym, trades)

        except asyncio.CancelledError:
            raise
        except _Closed as exc:  # ОЖИДАЕМЫЙ транзиент: HTX закрыл сокет → быстрый реконнект
            log_event(logger, 30, "ob_feed_reconnect", error_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(2.0)
        except Exception as exc:  # noqa: BLE001 — НАСТОЯЩАЯ ошибка: backoff + ERROR
            log_event(logger, 40, "ob_feed_error", error_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)

    log_event(logger, 20, "ob_feed_stopped")
