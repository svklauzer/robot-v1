"""Короткая сводка состояния робота для периодической отправки в Telegram.

Читает БД напрямую + ORDERBOOK_STORE из памяти процесса. Запускается фоновым
циклом в API (background_digest_loop), поэтому видит и позиции, и живой стакан.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta

from sqlalchemy import desc

from models.bot import Bot
from models.position import Position
from models.signal import Signal
from models.intelligence_event import IntelligenceEvent


def build_digest_text(db, window_hours: int = 2) -> str:
    bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
    status = f"{bot.status}/{bot.mode}" if bot else "n/a"

    open_pos = db.query(Position).filter(Position.status == "open").all()

    closed = db.query(Signal).filter(Signal.status == "closed").all()
    n_closed = len(closed)
    wins = [s for s in closed if float(s.closed_net_pnl or 0) > 0]
    winrate = (len(wins) / n_closed * 100.0) if n_closed else 0.0
    net = sum(float(s.closed_net_pnl or 0) for s in closed)
    costs = sum(float(s.closed_total_cost or 0) for s in closed)

    recent = (
        db.query(Signal)
        .filter(Signal.status == "closed")
        .order_by(desc(Signal.id))
        .limit(5)
        .all()
    )

    # Депт-фид (из памяти процесса)
    try:
        from services.orderbook_feed import ORDERBOOK_STORE
        st = ORDERBOOK_STORE.stats()
        age = st.get("freshest_age_sec")
        depth = f"{st.get('books', 0)} books, age {age:.1f}s" if age is not None else "no data (feed off?)"
    except Exception:
        depth = "n/a"

    # Активность за окно
    block_str, opens_n = "n/a", 0
    try:
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        evs = db.query(IntelligenceEvent).filter(IntelligenceEvent.created_at >= since).all()
        blocks = Counter(e.decision for e in evs if e.status == "blocked")
        opens_n = sum(1 for e in evs if e.status == "opened")
        block_str = ", ".join(f"{k}:{v}" for k, v in blocks.most_common(4)) or "—"
    except Exception:
        pass

    lines = [
        f"DIGEST ({window_hours}h)",
        f"Bot: {status}",
        f"Closed: {n_closed} | WR {winrate:.1f}% | Net {net:+.2f} USDT (costs {costs:.2f})",
        f"Open positions: {len(open_pos)}",
    ]
    for p in open_pos[:6]:
        lines.append(f"  - {p.symbol} {p.side} uPnL {float(p.unrealized_pnl or 0):+.3f}")
    lines.append(f"Opens ({window_hours}h): {opens_n}")
    lines.append("Recent exits:")
    for s in recent:
        lines.append(f"  - {s.symbol} {s.closed_reason or '-'} {float(s.closed_net_pnl or 0):+.3f}")
    lines.append(f"Depth feed: {depth}")
    lines.append(f"Blocks ({window_hours}h): {block_str}")
    return "\n".join(lines)
