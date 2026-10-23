"""Тесты наблюдателя trend_resume: только считает, не влияет на вход."""
from services.trend_resume import TrendResumeTracker


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def ctx(last, ema=100.0, atr=1.0):
    return {"1h": {"last_close": last, "ema20": ema, "atr14": atr}}


def test_short_pullback_then_resume_is_resumed():
    c = Clock(); tr = TrendResumeTracker(now=c)
    for price in (97.0, 99.0, 100.5, 99.5, 98.5):   # ниже EMA, коснулись, вернулись
        tr.observe("BTC/USDT", ctx(price)); c.t += 600
    s = tr.snapshot("BTC/USDT", "short")
    assert s["touched"] is True and s["resumed"] is True
    assert s["d_now"] > 0 and s["d_min_win"] < 0


def test_short_never_touched_is_not_resumed():
    c = Clock(); tr = TrendResumeTracker(now=c)
    for price in (97.0, 96.5, 96.0):
        tr.observe("ETH/USDT", ctx(price)); c.t += 600
    s = tr.snapshot("ETH/USDT", "short")
    assert s["touched"] is False and s["resumed"] is False
    assert s["minutes_since_touch"] is None


def test_still_on_wrong_side_is_not_resumed():
    c = Clock(); tr = TrendResumeTracker(now=c)
    for price in (97.0, 100.5, 101.0):               # коснулись и ещё не вернулись
        tr.observe("SOL/USDT", ctx(price)); c.t += 600
    assert tr.snapshot("SOL/USDT", "short")["resumed"] is False


def test_long_is_mirrored():
    c = Clock(); tr = TrendResumeTracker(now=c)
    for price in (103.0, 100.0, 101.5):
        tr.observe("LTC/USDT", ctx(price)); c.t += 600
    assert tr.snapshot("LTC/USDT", "long")["resumed"] is True


def test_old_touch_leaves_window():
    c = Clock(); tr = TrendResumeTracker(now=c)
    tr.observe("XRP/USDT", ctx(100.5)); c.t += 8 * 3600   # касание старше окна 6 ч
    tr.observe("XRP/USDT", ctx(97.0))
    s = tr.snapshot("XRP/USDT", "short")
    assert s["touched"] is False and s["resumed"] is False


def test_history_complete_only_after_window():
    c = Clock(); tr = TrendResumeTracker(now=c)
    tr.observe("DOGE/USDT", ctx(97.0)); c.t += 3600
    tr.observe("DOGE/USDT", ctx(97.0))
    assert tr.snapshot("DOGE/USDT", "short")["history_complete"] is False
    c.t += 6 * 3600
    tr.observe("DOGE/USDT", ctx(97.0))
    assert tr.snapshot("DOGE/USDT", "short")["history_complete"] is True


def test_fail_safe_on_garbage():
    tr = TrendResumeTracker()
    tr.observe("X", None)
    tr.observe("X", {"1h": {"last_close": "n/a"}})
    tr.observe("X", {"1h": {"last_close": 1, "ema20": 1, "atr14": 0}})
    assert tr.snapshot("X", "short")["resumed"] is None


def test_unknown_symbol_snapshot_is_safe():
    s = TrendResumeTracker().snapshot("NEW/USDT", "short")
    assert s["samples"] == 0 and s["history_complete"] is False
