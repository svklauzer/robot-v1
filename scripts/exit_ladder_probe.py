# scripts/exit_ladder_probe.py
import sys
sys.path.insert(0, "apps/api")
from services.exit_policy import ExitPolicyService

svc = ExitPolicyService(exchange="okx")
# Структура ЦЕЛА: цена выше KAMA, ADX не затухает от пика, OBV выше EMA.
TZ = {"kama": 100.0, "adx": 30.0, "adx_peak": 30.0,
      "obv": 1000.0, "obv_ema20": 900.0, "atr": 1.0}

def probe(mfe, cur, label):
    d = svc.before_tp1_decision(
        side="long", entry_price=100.0,
        current_price=100.0 * (1 + cur / 100),
        stop_price=98.0, tp1_price=101.2,
        mfe_pct=mfe, max_profit_price=100.0 * (1 + mfe / 100),
        symbol="BTC/USDT", market_type="swap",
        position_notional_usdt=200.0, signal_age_sec=3600.0,
        trade_mode="trend", flow_against=False,
        regime="trend_up_candidate", tz_context=TZ,
    )
    print(f"{label:34} exit={d.exit!s:5} reason={d.reason} note={(d.note or '')[:60]}")
    return d

ride = probe(1.20, 0.50, "ride: MFE 1.20 → 0.50 (отдал 58%)")
band = probe(0.55, 0.35, "band: MFE 0.55 → 0.35 (отдал 36%)")
hold = probe(0.55, 0.50, "hold: MFE 0.55 → 0.50 (отдал 9%)")

assert ride.exit and ride.reason == "trend_ride_trailing_stop", "ride НЕ сработал"
assert band.exit and band.reason == "trend_capture_band", "band НЕ сработал"
assert not hold.exit, "лестница режет здоровый откат"
print("\nOK: лестница вызывается внутри TZ-пути и не трогает здоровый откат")