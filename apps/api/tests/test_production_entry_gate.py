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
