"""Опечатка в режиме не имеет права выглядеть как настройка (#mode-typo-2026-09-05).

05.09 при вооружении защёлки импульса в env ушло `eforce` вместо `enforce`.
Код сравнивает режим строкой, не совпало — и защёлка молча осталась в тени: ни
ошибки, ни строчки в логе, ни отличия от намеренного `shadow`.

Форма общая для четырёх гейтов (`!= "enforce"` означает тень), и у трёх из них
enforce РАБОТАЕТ прямо сейчас: там опечатка не «не включила бы», а тихо
выключила бы действующую защиту.
"""
from __future__ import annotations

import pytest

from core.config import _MODE_CHOICES, Settings


def _mode_blockers(**overrides) -> list[str]:
    return [b for b in Settings(**overrides).production_blockers() if "valid mode" in b]


def test_production_values_are_clean():
    """Иначе проверка ронялась бы на исправном конфиге и её бы отключили."""
    assert _mode_blockers() == []


@pytest.mark.parametrize("key", sorted(_MODE_CHOICES))
def test_every_mode_setting_rejects_a_typo(key):
    blockers = _mode_blockers(**{key: "enfroce"})

    assert len(blockers) == 1, f"{key}: опечатка прошла молча"
    assert key in blockers[0] and "enfroce" in blockers[0]


def test_valid_values_pass():
    for key, allowed in _MODE_CHOICES.items():
        for value in allowed:
            assert _mode_blockers(**{key: value}) == [], f"{key}={value} отвергнут зря"


def test_case_and_padding_are_tolerated():
    """Значения приходят из дашборда руками: лишний пробел и регистр — не
    опечатка, и ронять на них конфиг значило бы кричать волками."""
    assert _mode_blockers(ENTRY_IMPULSE_LATCH_MODE=" Enforce ") == []


def test_blocker_names_the_consequence_not_just_the_rule():
    """Через месяц «недопустимое значение» читается как придирка. Тихое
    разоружение гейта — как причина."""
    blocker = _mode_blockers(TZ_MODE="enforse")[0]

    assert "disarms" in blocker and "silently" in blocker


def test_every_string_compared_mode_is_covered():
    """Гейт, сравнивающий режим строкой и не попавший в список, остаётся с той
    же дырой. Список обязан расти вместе с ними."""
    from pathlib import Path

    api = Path(__file__).resolve().parents[1]
    sources = "\n".join(
        (api / name).read_text(encoding="utf-8")
        for name in ("services/tz_entry_shadow.py", "services/trend_trigger.py",
                     "services/tp_reachability.py", "services/entry_impulse_latch.py")
    )

    for key in ("TZ_MODE", "TREND_TRIGGER_MODE", "TP_REACH_MODE",
                "ENTRY_IMPULSE_LATCH_MODE"):
        assert key in sources, f"{key} больше не читается — обнови список"
        assert key in _MODE_CHOICES, f"{key} сравнивается строкой, но не проверяется"
