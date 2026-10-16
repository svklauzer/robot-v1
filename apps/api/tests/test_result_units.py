from types import SimpleNamespace as NS
from services.result_units import signal_notional, pct_of_notional


def _sig(**k):
    base = dict(qty=2.5, entry_zone={"from": 80.9, "to": 81.4},
                plan_json={"lifecycle": {"entry_price": 81.0}})
    base.update(k)
    return NS(**base)


def test_notional_and_pct():
    s = _sig()
    assert signal_notional(s) == 2.5 * 81.0
    # +4.69 при номинале 202.5 = 2.3%, а не 23% (от маржи)
    assert abs(pct_of_notional(4.69, s) - 2.3160) < 1e-3


def test_no_data_is_none():
    assert signal_notional(_sig(qty=None, plan_json={})) is None
    assert pct_of_notional(1.0, _sig(qty=0)) is None


def test_entry_zone_fallback():
    s = _sig(plan_json={})
    assert abs(signal_notional(s) - 2.5 * 81.15) < 1e-9
