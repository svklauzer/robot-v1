from services.production_entry_gate import ProductionEntryGate


def test_production_gate_payload_includes_thresholds_for_symbol_policy():
    decision = ProductionEntryGate().check(
        grade="A",
        setup_score=90.0,
        effective_confidence=90.0,
        net_rr_tp1=1.5,
        net_rr_tp2=2.0,
        priority_score=100.0,
    )

    assert decision.allowed is True
    assert decision.payload["thresholds"]["min_confidence"] > 0
    assert decision.payload["thresholds"]["min_rr_tp1"] > 0
    assert decision.payload["thresholds"]["min_rr_tp2"] > 0


def test_grade_b_is_learning_only_by_default():
    decision = ProductionEntryGate().check(
        grade="B",
        setup_score=95.0,
        effective_confidence=95.0,
        net_rr_tp1=2.0,
        net_rr_tp2=4.0,
        priority_score=100.0,
    )

    assert decision.allowed is False
    assert decision.reason == "grade_b_learning_only"
