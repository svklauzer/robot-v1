"""WebSocket-фид книги и ленты сделок → общий in-memory store.

(#depth-venue-2026-09-07) Фид следует за биржей, на которой РЕАЛЬНО идут ордера.
До этой правки он был жёстко на HTX, а торговля с 02.09 ушла на OKX — и книга
чужой биржи при этом не наблюдалась, а работала: `OB_GATE_ENTRIES` блокировал по
ней входы, `entry_depth.*` уходил в план сделки и дальше в форензику стопов как
признак входа. То есть решение о входе принималось по одной площадке, сделка
жила на другой, а разбор считал это одним и тем же.

Биржу выбирает `feed_exchange()`: по умолчанию `ACTIVE_EXCHANGE`, с ручным
перекрытием `OB_EXCHANGE` — оно нужно, чтобы можно было держать фид на прежней
площадке, не трогая маршрутизацию ордеров.

Различия площадок, которые пришлось развести:
  HTX  gzip-кадры, `{"ping": ts}` от сервера → отвечаем `{"pong": ts}`;
       каналы market.<sym>.depth.step0 и market.<sym>.trade.detail.
  OKX  обычный текст, сервер НЕ пингует — клиент сам шлёт строку "ping" при
       тишине и получает "pong"; каналы books5 и trades, instId вида
       BTC-USDT / BTC-USDT-SWAP.

Уровни книги тоже разные: HTX step0 отдаёт полную глубину, OKX books5 — пять
уровней. `OB_DEPTH_LEVELS` (дефолт 10) на OKX упирается в пять, поэтому obi и
wall_share на двух площадках считаются по разной глубине и НЕ сравнимы между
собой напрямую. Ради этого в снимок и пишется биржа: в форензике признаки входа
из разных эпох иначе смешались бы молча.

Всё под флагом ENABLE_ORDERBOOK_ENGINE; если фид молчит, снимок отдаёт None →
анализатор уходит в pass-through, торговля как обычно.

Полная книга OKX и тень (#okx-depth-2026-09-12). OKX теперь идёт каналом
`books` (до 400 уровней, книга ведётся локально — см. services/okx_book.py) и
отдаётся потребителям обрезанной до глубины HTX `step0` (`OB_BOOK_LEVELS`).
Пока фид закреплён на HTX, книга OKX пишется в ТЕНЕВОЕ хранилище
(`OB_OKX_SHADOW_ENABLED`): решения по ней не принимаются, а `/orderbook/compare`
показывает метрики всех трёх потребителей — depth_thinness, wall_share, obi —
по обеим книгам рядом. Переключать фид — только когда они сойдутся: 07.09
переключение без такой проверки молча сломало пороги входа.
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

# (#okx-depth-2026-09-12) Книга OKX, пока рабочий фид на HTX. Только для
# сравнения — ни вход, ни выход её не читают.
ORDERBOOK_SHADOW_STORE = OrderBookStore(
    trades_window_sec=float(getattr(settings, "OB_CVD_WINDOW_SEC", 60)),
)


def book_levels() -> int:
    return max(int(getattr(settings, "OB_BOOK_LEVELS", 150) or 150), 10)


# Спот и перпетуал — РАЗНЫЕ книги с разной ликвидностью и разной лентой.
# Формат символа и эндпоинт у них тоже разные:
#   spot  wss://api-aws.huobi.pro/ws        market.btcusdt.depth.step0
#   swap  wss://api.hbdm.com/linear-swap-ws market.BTC-USDT.depth.step0
_WS_DEFAULTS = {
    "spot": "wss://api-aws.huobi.pro/ws",
    "swap": "wss://api.hbdm.com/linear-swap-ws",
}


def feed_exchange() -> str:
    """Чью книгу слушаем. По умолчанию — ту, где исполняются ордера.

    `OB_EXCHANGE` перекрывает ACTIVE_EXCHANGE намеренно: переезд фида меняет
    смысл измеряемых признаков (см. шапку), и возможность оставить его на месте
    при переключении торговли нужна для сравнимости разбора.
    """
    explicit = str(getattr(settings, "OB_EXCHANGE", "") or "").strip().lower()
    if explicit in ("htx", "okx"):
        return explicit
    try:
        from services.exchange_factory import resolve_exchange_name

        name = resolve_exchange_name()
    except Exception:  # noqa: BLE001 — фид не должен падать из-за резолвера
        return "htx"
    return name if name in ("htx", "okx") else "htx"


def ob_market_type() -> str:
    value = str(getattr(settings, "OB_MARKET_TYPE", "spot")).lower().strip()
    return value if value in _WS_DEFAULTS else "spot"


def ws_url() -> str:
    """Явный OB_WS_URL перекрывает всё; иначе — эндпоинт по типу рынка."""
    explicit = str(getattr(settings, "OB_WS_URL", "") or "").strip()
    return explicit or _WS_DEFAULTS[ob_market_type()]


def _ws_symbol(ccxt_symbol: str, market_type: str | None = None) -> str:
    base = str(ccxt_symbol).split(":", 1)[0]
    if (market_type or ob_market_type()) == "swap":
        return base.replace("/", "-").upper()
    return base.replace("/", "").lower()


async def run_htx_orderbook_feed(symbols, enabled_fn, store: OrderBookStore | None = None):
    """Запускается как фоновый таск. enabled_fn() -> bool управляет жизненным циклом."""
    try:
        import websockets  # локальный импорт: нет websockets → движок просто не стартует
    except Exception as exc:  # noqa: BLE001
        log_event(logger, 40, "ob_feed_no_websockets", error=str(exc))
        return

    store = store or ORDERBOOK_STORE
    market_type = ob_market_type()
    url = ws_url()
    sym_map = {_ws_symbol(s, market_type): s for s in symbols}
    backoff = 2.0
    # Watchdog: HTX шлёт ping/данные часто; тишина дольше этого = «мёртвый» сокет
    # (бывает без close-frame → ConnectionClosedError). Проактивно реконнектимся.
    read_timeout = float(getattr(settings, "OB_WS_READ_TIMEOUT_SEC", 30.0))
    # Ожидаемые разрывы соединения (реконнект) — это НЕ ошибка приложения.
    _Closed = getattr(websockets, "ConnectionClosed", ())

    while enabled_fn():
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2 ** 23) as ws:
                for wsym in sym_map:
                    await ws.send(json.dumps({"sub": f"market.{wsym}.depth.step0", "id": f"d_{wsym}"}))
                    await ws.send(json.dumps({"sub": f"market.{wsym}.trade.detail", "id": f"t_{wsym}"}))
                log_event(logger, 20, "ob_feed_connected", url=url,
                          market_type=market_type, symbols=len(sym_map),
                          example_symbol=next(iter(sym_map), None))
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


# ── OKX ─────────────────────────────────────────────────────────────────────

_OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


def okx_ws_url() -> str:
    explicit = str(getattr(settings, "OB_OKX_WS_URL", "") or "").strip()
    return explicit or _OKX_WS_URL


def _okx_inst_id(ccxt_symbol: str, market_type: str | None = None) -> str:
    """BTC/USDT → BTC-USDT (спот) или BTC-USDT-SWAP (перпетуал).

    Тип берётся из того же `OB_MARKET_TYPE`, что и у HTX: книга обязана быть той
    же природы, что и исполнение, иначе спред и стенки считаются не по тому
    инструменту.
    """
    base = str(ccxt_symbol).split(":", 1)[0]
    inst = base.replace("/", "-").upper()
    if (market_type or ob_market_type()) == "swap":
        return f"{inst}-SWAP"
    return inst


def handle_okx_message(msg: dict, inst_map: dict, books: dict, store: "OrderBookStore",
                       levels: int) -> list[str]:
    """Одно сообщение OKX → хранилище. Возвращает instId, чью книгу надо
    пересобрать (пропуск в последовательности или неверная контрольная сумма).

    `books` — полная книга, ведётся локально; `books5` и прочие — готовые
    снимки, как было.
    """
    from services.okx_book import OkxLocalBook

    arg = msg.get("arg") or {}
    rows = msg.get("data") or []
    inst = arg.get("instId")
    sym = inst_map.get(inst)
    if not sym or not rows:
        return []

    channel = str(arg.get("channel") or "")
    if channel == "books":
        book = books.setdefault(inst, OkxLocalBook())
        action = str(msg.get("action") or "update")
        for row in rows:
            if not book.apply(action, row):
                book.reset()
                return [inst]
        store.update_book(sym, *book.top(levels))
    elif channel.startswith("books"):
        book = rows[-1]
        store.update_book(sym, book.get("bids", []), book.get("asks", []))
    elif channel == "trades":
        store.add_trades(sym, [
            {"side": d.get("side"), "amount": d.get("sz")} for d in rows
        ])
    return []


async def run_okx_orderbook_feed(symbols, enabled_fn, store: OrderBookStore | None = None,
                                 *, shadow: bool = False):
    """Фид OKX. Форма та же, что у HTX-ветки, различия — в протоколе.

    OKX не пингует клиента: молчание не отличить от смерти сокета, поэтому при
    тишине шлём строку "ping" сами и ждём "pong". Реконнект — только если и
    после пинга тишина, иначе спокойный рынок выглядел бы обрывом.

    `shadow=True` — тень рядом с рабочим фидом HTX: только книга, без ленты
    сделок, в отдельное хранилище.
    """
    try:
        import websockets
    except Exception as exc:  # noqa: BLE001
        log_event(logger, 40, "ob_feed_no_websockets", error=str(exc))
        return

    store = store or ORDERBOOK_STORE
    market_type = ob_market_type()
    url = okx_ws_url()
    inst_map = {_okx_inst_id(s, market_type): s for s in symbols}
    book_channel = str(getattr(settings, "OB_OKX_BOOK_CHANNEL", "books") or "books")
    levels = book_levels()
    books: dict = {}
    backoff = 2.0
    read_timeout = float(getattr(settings, "OB_WS_READ_TIMEOUT_SEC", 30.0))
    _Closed = getattr(websockets, "ConnectionClosed", ())

    while enabled_fn():
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2 ** 23) as ws:
                books.clear()
                args = []
                for inst in inst_map:
                    args.append({"channel": book_channel, "instId": inst})
                    if not shadow:
                        args.append({"channel": "trades", "instId": inst})
                await ws.send(json.dumps({"op": "subscribe", "args": args}))
                log_event(logger, 20, "ob_feed_connected", url=url, exchange="okx",
                          market_type=market_type, symbols=len(inst_map),
                          channel=book_channel, levels=levels, shadow=shadow,
                          example_symbol=next(iter(inst_map), None))
                backoff = 2.0
                awaiting_pong = False

                while enabled_fn():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
                    except asyncio.TimeoutError:
                        if awaiting_pong:
                            log_event(logger, 30, "ob_feed_stale_reconnect",
                                      exchange="okx", timeout_sec=read_timeout)
                            break
                        await ws.send("ping")
                        awaiting_pong = True
                        continue

                    awaiting_pong = False
                    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
                    if text == "pong":
                        continue

                    try:
                        msg = json.loads(text)
                    except Exception:  # noqa: BLE001
                        continue

                    # Подписка не принята — молча это не оставляем: фид будет
                    # «жив» и пуст, а гейт уйдёт в fail-open без единого следа.
                    if msg.get("event") == "error":
                        log_event(logger, 40, "ob_feed_subscribe_error", exchange="okx",
                                  code=msg.get("code"), error=msg.get("msg"))
                        continue
                    if msg.get("event"):
                        continue

                    # Книга, которой нельзя верить, пересобирается с нового
                    # снимка: отписка и подписка заново на этот инструмент.
                    for inst in handle_okx_message(msg, inst_map, books, store, levels):
                        log_event(logger, 30, "ob_feed_book_resync", exchange="okx",
                                  inst=inst, shadow=shadow)
                        sub_arg = [{"channel": book_channel, "instId": inst}]
                        await ws.send(json.dumps({"op": "unsubscribe", "args": sub_arg}))
                        await ws.send(json.dumps({"op": "subscribe", "args": sub_arg}))

        except asyncio.CancelledError:
            raise
        except _Closed as exc:
            log_event(logger, 30, "ob_feed_reconnect", exchange="okx",
                      error_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(2.0)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, 40, "ob_feed_error", exchange="okx",
                      error_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)

    log_event(logger, 20, "ob_feed_stopped", exchange="okx")


async def run_orderbook_feed(symbols, enabled_fn, store: OrderBookStore | None = None):
    """Единая точка входа: слушаем ту биржу, на которой идут ордера.

    (#okx-depth-2026-09-12) Если рабочий фид на HTX, рядом идёт тень OKX —
    для сравнения книг перед переключением.
    """
    venue = feed_exchange()
    if venue == "okx":
        await run_okx_orderbook_feed(symbols, enabled_fn, store)
        return
    tasks = [run_htx_orderbook_feed(symbols, enabled_fn, store)]
    if bool(getattr(settings, "OB_OKX_SHADOW_ENABLED", True)):
        tasks.append(run_okx_orderbook_feed(symbols, enabled_fn, ORDERBOOK_SHADOW_STORE,
                                            shadow=True))
    await asyncio.gather(*tasks, return_exceptions=True)


def _book_metrics(snapshot: dict | None, levels: int) -> dict | None:
    """Метрики всех потребителей книги — тех, что сломались на books5 07.09."""
    if not snapshot:
        return None
    from services.entry_zone import depth_thinness
    from services.orderbook_analyzer import OrderBookAnalyzer

    bids, asks = snapshot.get("bids") or [], snapshot.get("asks") or []
    spread, _ = OrderBookAnalyzer.spread_pct(bids, asks)

    def _r(value, nd=4):
        return round(value, nd) if value is not None else None

    return {
        "levels_bid": len(bids),
        "levels_ask": len(asks),
        "spread_pct": _r(spread),
        "obi": _r(OrderBookAnalyzer.imbalance(bids, asks, levels)),
        "bid_wall": _r(OrderBookAnalyzer.wall_share(bids, levels)),
        "ask_wall": _r(OrderBookAnalyzer.wall_share(asks, levels)),
        "thinness_long": _r(depth_thinness("long", snapshot)),
        "thinness_short": _r(depth_thinness("short", snapshot)),
        "age_sec": _r(snapshot.get("age_sec"), 2),
    }


def compare_books(primary: OrderBookStore | None = None,
                  shadow: OrderBookStore | None = None, *, levels: int | None = None) -> dict:
    """Рабочая книга против теневой по каждому символу и медианы по всем."""
    primary = primary or ORDERBOOK_STORE
    shadow = shadow or ORDERBOOK_SHADOW_STORE
    levels = int(levels or getattr(settings, "OB_DEPTH_LEVELS", 10))
    names = sorted(set(primary.stats()["symbols"]) | set(shadow.stats()["symbols"]))
    rows = {
        sym: {"primary": _book_metrics(primary.snapshot(sym), levels),
              "shadow": _book_metrics(shadow.snapshot(sym), levels)}
        for sym in names
    }

    def _median(side: str, key: str):
        vals = sorted(r[side][key] for r in rows.values()
                      if r[side] and r[side].get(key) is not None)
        if not vals:
            return None
        mid = len(vals) // 2
        return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 4)

    keys = ("levels_bid", "spread_pct", "obi", "bid_wall", "ask_wall",
            "thinness_long", "thinness_short")
    return {
        "levels": levels,
        "symbols": rows,
        "median": {side: {k: _median(side, k) for k in keys} for side in ("primary", "shadow")},
    }
