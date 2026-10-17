# -*- coding: utf-8 -*-
"""Приводит старые тесты к текущему коду: test_ml_auto_demote.py и test_ml_rework.py.
Запуск из корня репозитория. Идемпотентен (повторный запуск ничего не меняет). Бэкапы *.bak-before-tests."""
import ast, shutil, sys

def edit(path, reps):
    raw = open(path, "rb").read(); crlf = b"\r\n" in raw
    s = raw.decode("utf-8").replace("\r\n", "\n"); changed = 0
    for old, new in reps:
        if new in s: continue
        if s.count(old) != 1:
            sys.exit(f"{path}: фрагмент найден {s.count(old)} раз(а), ожидался 1:\n{old[:120]}")
        s = s.replace(old, new); changed += 1
    ast.parse(s)
    if changed:
        shutil.copyfile(path, path + ".bak-before-tests")
        open(path, "wb").write((s.replace("\n", "\r\n") if crlf else s).encode("utf-8"))
    print(f"{path}: изменений {changed}")

edit("apps/api/tests/test_ml_auto_demote.py", [
('''def _controller(auc):
    c = MLController()
    c._val_auc = lambda: auc          # noqa: SLF001 — подменяем источник метрики
    return c
''',
'''def _controller(auc):
    """Контроллер с подменённым блоком metrics последнего обучения.

    (#authority-2026-10-10) Режим решает resolve_effective_mode() по ВСЕМУ блоку
    метрик, а не по одному val_auc: для сохранения полномочий нужны ещё
    events_per_feature >= 3, надёжность оценки и ДИ walk-forward выше 0.5.
    auc=None — метрики нет вовсе.
    """
    c = MLController()
    metrics = {} if auc is None else {
        "val_auc": auc,
        "events_per_feature": 12.0,
        "auc_is_reliable": True,
        "wf_auc_ci95": [0.55, 0.70],
    }
    c._metrics_block = lambda: metrics   # noqa: SLF001 — подменяем источник метрик
    c._val_auc = lambda: auc             # noqa: SLF001
    return c
'''),
('''def test_missing_metric_does_not_change_mode(monkeypatch):
    """Fail-open: проблема с чтением метрики не должна ломать торговлю."""
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    assert _controller(None)._mode() == "full_auto"
''',
'''def test_missing_metric_revokes_authority(monkeypatch):
    """Нет метрики — качество не доказано, полномочия отозваны до shadow.

    Fail-open означает «ML не мешает сделкам»: shadow ничего не блокирует. Право
    блокировать и менять размер без доказанного качества модель не получает.
    """
    monkeypatch.setattr(settings, "ML_MODE", "full_auto")
    assert _controller(None)._mode() == "shadow"
'''),
])

edit("apps/api/tests/test_ml_rework.py", [
('''    for name in ("stop_distance_pct", "notional_usdt", "rr_asymmetry", "hour_of_day"):''',
 '''    for name in ("stop_distance_pct", "rr_asymmetry", "hour_sin", "hour_cos"):'''),
('''        "regime": "crt", "opened_at": "2026-07-28T14:00:00+00:00",''',
 '''        "regime": "crt", "trade_mode": "crt", "opened_at": "2026-07-28T14:00:00+00:00",'''),
('''    assert vec[idx["hour_of_day"]] == 14.0
''',
'''    import math
    assert vec[idx["hour_sin"]] == pytest.approx(math.sin(2 * math.pi * 14 / 24))
    assert vec[idx["hour_cos"]] == pytest.approx(math.cos(2 * math.pi * 14 / 24))
'''),
('''from core.config import settings
from services.ml_features import (''',
'''import pytest

from core.config import settings
from services.ml_features import ('''),
('''    for token in ('"stop_price": plan.stop_price',
                  '"required_margin": plan.required_margin',
                  '"lifecycle": {"entry_price": plan.entry_price}'):
        assert token in call_src, f"{token!r} must be forwarded to ml_controller"
''',
'''    # Важны ключи, а не форма выражения справа (plan.x или локальная float(x)).
    # rationale/trade_mode — из них берутся is_scalp/is_crt (паритет train/serve).
    for token in ('"stop_price":', '"required_margin":', '"lifecycle": {"entry_price":',
                  '"rationale":', '"trade_mode":'):
        assert token in call_src, f"{token!r} must be forwarded to ml_controller"
'''),
])
