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
        # Grade B paper: setup_score >= 58, effective_confidence >= 60
        assert svc.should_publish_to_clients(
            grade="B",
            setup_score=60,
            effective_confidence=62,
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


def test_should_not_publish_grade_b_in_paper_when_confidence_too_low():
    svc = SignalQualityService()
    original_mode = settings.TRADING_MODE
    settings.TRADING_MODE = "paper_signal"
    try:
        # effective_confidence=56 is below the new threshold of 60
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
        ) is False
    finally:
        settings.TRADING_MODE = original_mode


def test_should_not_publish_grade_c_in_paper():
    svc = SignalQualityService()
    original_mode = settings.TRADING_MODE
    settings.TRADING_MODE = "paper_signal"
    try:
        assert svc.should_publish_to_clients(
            grade="C",
            setup_score=90,
            effective_confidence=90,
            setup_decision="approve",
            setup_quality={
                "weak_volume_count": 0,
                "volume_confirmation": 10,
                "trend_alignment": 60,
                "entry_timing": 20,
            },
        ) is False
    finally:
        settings.TRADING_MODE = original_mode
