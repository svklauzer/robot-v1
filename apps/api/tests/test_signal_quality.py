from services.signal_quality import SignalQualityService
from core.config import settings


def test_should_not_publish_when_setup_not_approved():
    svc = SignalQualityService()
    assert svc.should_publish_to_clients(
        grade="A",
        setup_score=80,
        effective_confidence=80,
        setup_decision="wait",
        setup_quality={},
    ) is False


def test_should_block_weak_volume_even_with_good_grade():
    svc = SignalQualityService()
    original_mode = settings.TRADING_MODE
    settings.TRADING_MODE = "paper_signal"
    try:
        assert svc.should_publish_to_clients(
            grade="A+",
            setup_score=90,
            effective_confidence=90,
            setup_decision="approve",
            setup_quality={
                "weak_volume_count": 5,
                "volume_confirmation": 2,
                "trend_alignment": 60,
                "entry_timing": 20,
            },
        ) is False
    finally:
        settings.TRADING_MODE = original_mode


def test_should_publish_grade_b_in_paper_when_thresholds_met():
    svc = SignalQualityService()
    original_mode = settings.TRADING_MODE
    settings.TRADING_MODE = "paper_signal"
    try:
        assert svc.should_publish_to_clients(
            grade="B",
            setup_score=58,
            effective_confidence=56,
            setup_decision="approve",
            setup_quality={
                "weak_volume_count": 1,
                "volume_confirmation": 8,
                "trend_alignment": 55,
                "entry_timing": 18,
            },
        ) is True
    finally:
        settings.TRADING_MODE = original_mode
