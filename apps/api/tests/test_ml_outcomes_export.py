import json

from services.ml_outcomes_export import iter_slim_lines


def test_slim_drops_traj_and_skips_broken_lines(tmp_path):
    p = tmp_path / "o.jsonl"
    rows = [
        {"signal_id": 1, "closed_net_pnl": 1.5, "lifecycle": {"mfe_pct": 1.0, "traj": [1, 2, 3], "traj_step": 5}},
        {"signal_id": 2, "lifecycle": None},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + '\n{"signal_id": 3, "lifec', encoding="utf-8")
    out = [json.loads(b) for b in iter_slim_lines(p)]
    assert [r["signal_id"] for r in out] == [1, 2]            # недописанная строка пропущена
    assert out[0]["lifecycle"] == {"mfe_pct": 1.0}
    assert out[0]["closed_net_pnl"] == 1.5
