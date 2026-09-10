"""Чем стопнутая сделка отличалась на входе (#stop-forensics-2026-09-04).

Замер 04.09 по 91 закрытой сделке: 37 стопов дали −53.82 USDT, остальные 54 —
плюс 34.16. Без стопов система прибыльна, и сопровождение работает (tz_kama
режет по −0.38, breakeven_stop в плюс). Значит убивают входы, идущие против
сразу.

Отчёт ищет признак, разделяющий эти две группы. Ранговая мера (AUC) выбрана
потому, что на 37 наблюдениях среднее ломается одним выбросом.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = Path(__file__).resolve().parents[1]

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.bot import Bot
from models.signal import Signal
from models.user import User
from services.stop_loss_forensics import (
    _CATEGORICAL, _NUMERIC, _auc, _tp_reach_margin, build,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, Bot.__table__, Signal.__table__,
    ])
    session = sessionmaker(bind=engine)()
    user = User(email="o@e.com", password_hash="h")
    session.add(user)
    session.flush()
    bot = Bot(user_id=user.id, name="Main Robot", status="running",
              mode="paper", config_json={})
    session.add(bot)
    session.flush()
    session.bot_id = bot.id
    yield session
    session.close()


def _sig(db, *, reason: str, side: str = "long", net: float = -1.0,
         obi: float = 0.0, adx: float = 25.0, score: float = 80.0,
         structure: float = 12.0,
         regime: str = "trend_up_candidate", grade: str = "A"):
    signal = Signal(
        bot_id=db.bot_id, symbol="X/USDT", side=side, status="closed",
        entry_zone_json={"from": 100.0, "to": 100.0}, stop_price=99.0,
        tp_json={"tp1": 101.0, "tp2": 103.0},
        confidence=70.0, rationale="t", grade=grade, is_public=True,
        closed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        closed_net_pnl=net, net_pnl_stop=-1.0, closed_reason=reason,
        plan_json={
            "regime": regime,
            "trade_mode": "trend",
            "entry_depth": {"obi": obi, "cvd_ratio": 0.0, "spread_pct": 0.01,
                            "cvd_trades": 10},
            "tz_shadow": {"adx": adx, "di_spread": 10.0, "stoch_k": 40.0,
                          "would_pass": False},
            "setup_quality": {"final_score": score, "trend_alignment": 50.0,
                              "entry_timing": 20.0, "volume_confirmation": 15.0,
                              "structure_quality": structure, "penalty": 0.0},
            "ml": {"ml_score": 0.5},
            "tp_reach": {"tp1_dist_pct": 1.0, "tp2_dist_pct": 3.0},
            "sizing": {"conviction": 1.0},
            "entry_zone_plan": {"mode": "market", "drift_pct": 0.0,
                                "depth": {"near_depth_share": 0.05}},
        },
    )
    db.add(signal)
    db.flush()
    return signal


# ── ранговая мера ───────────────────────────────────────────────────────────

def test_auc_is_half_when_groups_are_identical():
    assert _auc([1, 2, 3], [1, 2, 3]) == 0.5


def test_auc_reaches_the_edges_on_perfect_separation():
    assert _auc([5, 6, 7], [1, 2, 3]) == 1.0
    assert _auc([1, 2, 3], [5, 6, 7]) == 0.0


def test_auc_is_not_moved_by_a_single_outlier():
    """Ради этого мера и ранговая: на 37 наблюдениях один выброс сдвинул бы
    среднее так, что признак показался бы разделяющим."""
    assert _auc([1, 2, 3], [4, 5, 10_000]) == _auc([1, 2, 3], [4, 5, 6])


# ── разделение групп ────────────────────────────────────────────────────────

def _rows(out, feature):
    return next(r for r in out["numeric"] if r["feature"] == feature)


def test_finds_a_feature_that_separates(db):
    """Стопнутые входили при низком ADX, выжившие при высоком."""
    for _ in range(8):
        _sig(db, reason="stop_loss", adx=15.0)
    for _ in range(8):
        _sig(db, reason="tp2_reached", net=2.0, adx=35.0)

    row = _rows(build(db, min_group=5), "tz_shadow.adx")

    assert row["auc"] == 0.0                      # у стопнутых всегда ниже
    assert row["higher_in"] == "survived"
    assert row["verdict"] == "separates_strongly"


def test_marks_a_useless_feature_as_indistinguishable(db):
    """Главная защита от самообмана: на малой выборке почти любой признак
    даёт какой-то перекос, и его нельзя принимать за находку."""
    for i in range(8):
        _sig(db, reason="stop_loss", adx=20.0 + i)
    for i in range(8):
        _sig(db, reason="tp2_reached", net=2.0, adx=20.0 + i)

    row = _rows(build(db, min_group=5), "tz_shadow.adx")

    assert row["auc"] == 0.5
    assert row["verdict"] == "indistinguishable"


def test_signed_features_are_oriented_by_side(db):
    """obi/cvd — направленные: «поток за сделку» у лонга и шорта имеет
    противоположный знак. Без нормировки лонги и шорты гасят друг друга, и
    разделение пропадает даже там, где оно есть.

    Здесь у ВСЕХ выживших поток по сделке (+0.6 лонгам, −0.6 шортам), у всех
    стопнутых против. В сыром виде медианы обеих групп были бы около нуля.
    """
    for i in range(8):
        side = "long" if i % 2 else "short"
        _sig(db, reason="stop_loss", side=side, obi=-0.6 if side == "long" else 0.6)
    for i in range(8):
        side = "long" if i % 2 else "short"
        _sig(db, reason="tp2_reached", net=2.0, side=side,
             obi=0.6 if side == "long" else -0.6)

    row = _rows(build(db, min_group=5), "entry_depth.obi")

    assert row["median_stopped"] == pytest.approx(-0.6)
    assert row["median_survived"] == pytest.approx(0.6)
    assert row["auc"] == 0.0


def test_strongest_separator_comes_first(db):
    for i in range(8):
        _sig(db, reason="stop_loss", adx=15.0, score=80.0 + i)
    for i in range(8):
        _sig(db, reason="tp2_reached", net=2.0, adx=35.0, score=80.0 + i)

    out = build(db, min_group=5)

    assert out["numeric"][0]["feature"] == "tz_shadow.adx"


# ── группы и категории ──────────────────────────────────────────────────────

def test_group_totals_match_the_split(db):
    for _ in range(3):
        _sig(db, reason="stop_loss", net=-2.0)
    for _ in range(2):
        _sig(db, reason="breakeven_stop", net=1.0)

    out = build(db, min_group=1)

    assert out["stopped"]["n"] == 3
    assert out["stopped"]["net_usdt"] == pytest.approx(-6.0)
    assert out["survived"]["n"] == 2
    assert out["survived"]["net_usdt"] == pytest.approx(2.0)


def test_categorical_shows_stop_rate_per_level(db):
    for _ in range(3):
        _sig(db, reason="stop_loss", grade="B")
    _sig(db, reason="tp2_reached", net=2.0, grade="B")
    _sig(db, reason="tp2_reached", net=2.0, grade="A")

    levels = next(c for c in build(db, min_group=1)["categorical"]
                  if c["feature"] == "signal.grade")["levels"]

    assert levels["B"]["n"] == 4
    assert levels["B"]["stop_rate"] == pytest.approx(0.75)
    assert levels["A"]["stop_rate"] == pytest.approx(0.0)


def test_thin_features_are_dropped_not_guessed(db):
    """Признак, у которого в группе меньше min_group значений, не выводится
    вовсе: доля по трём наблюдениям — не доля."""
    for _ in range(2):
        _sig(db, reason="stop_loss")
    for _ in range(2):
        _sig(db, reason="tp2_reached", net=2.0)

    assert build(db, min_group=5)["numeric"] == []


def test_regime_filter_narrows_the_sample(db):
    for _ in range(6):
        _sig(db, reason="stop_loss", regime="trend_up_candidate")
    for _ in range(6):
        _sig(db, reason="stop_loss", regime="trend_down_candidate")

    out = build(db, regime="trend_up_candidate", min_group=1)

    assert out["stopped"]["n"] == 6


def test_empty_history_does_not_explode(db):
    out = build(db)
    assert out["stopped"]["n"] == 0
    assert out["numeric"] == []


def test_signal_level_fields_are_reachable(db):
    """(#confidence-axis-2026-09-04) confidence живёт на самой записи, а не в
    plan_json. Пока разбор смотрел только в план, ось, на которой построен
    грейд, оставалась непроверенной — а именно она и оказалась разделяющей."""
    for _ in range(6):
        s = _sig(db, reason="stop_loss")
        s.confidence = 80.0
    for _ in range(6):
        s = _sig(db, reason="tp2_reached", net=2.0)
        s.confidence = 65.0
    db.flush()

    row = next(r for r in build(db, min_group=5)["numeric"]
               if r["feature"] == "signal.confidence")

    assert row["median_stopped"] == pytest.approx(80.0)
    assert row["median_survived"] == pytest.approx(65.0)
    assert row["auc"] == 1.0, "выше confidence — чаще стоп; это и надо увидеть"


def test_side_filter_splits_the_sample(db):
    """(#structure-mirror-2026-09-04) structure_score считается по шкале
    «хорошо для лонга» (близко к support = 70) и для шортов не зеркалится ни в
    confidence, ни в setup_quality. Значит у лонгов компонент работает верно
    (выше structure → чаще выживают), а у шортов ровно наоборот.

    На смешанной выборке эти две противоположные связи гасят друг друга ТОЧНО в
    0.5 — признак выглядит бесполезным именно там, где он вреден. Разрез по
    стороне разводит их обратно.
    """
    for _ in range(6):
        _sig(db, reason="stop_loss", side="short", structure=17.5)
        _sig(db, reason="tp2_reached", side="short", net=2.0, structure=10.0)
        _sig(db, reason="stop_loss", side="long", structure=10.0)
        _sig(db, reason="tp2_reached", side="long", net=2.0, structure=17.5)

    def structure_auc(**kw):
        out = build(db, min_group=5, **kw)
        return next(r for r in out["numeric"]
                    if r["feature"] == "setup_quality.structure_quality")["auc"]

    assert structure_auc(side="short") == 1.0   # выше structure → стоп
    assert structure_auc(side="long") == 0.0    # выше structure → выжила
    assert structure_auc() == 0.5               # смешанное: ровно ничего


def test_side_filter_is_reported_back(db):
    _sig(db, reason="stop_loss", side="short")
    assert build(db, side="short", min_group=1)["side"] == "short"
    assert build(db, side="long", min_group=1)["stopped"]["n"] == 0


def _trade_plan_keys() -> set[str]:
    """Ключи словаря plan_json, который боевой путь пишет в сделку."""
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if {"tz_shadow", "tp_reach", "entry_reason"} <= keys:
            return keys
    raise AssertionError("не найден словарь plan_json боевого пути")


def test_every_measured_feature_actually_reaches_the_plan():
    """(#setup-quality-plan-2026-09-04) Признак, которого нет в плане, не
    «показывает 0.5» — он молча выпадает из отчёта за нехваткой данных.

    Именно это и произошло: шесть признаков setup_quality.* перечислялись в
    списке замера, а в боевой plan_json ключ `setup_quality` не писался вовсе.
    Со стороны это выглядело как «признак не разделяет группы» — то есть
    отсутствие данных читалось как результат. Разница между «измерили и ничего
    не нашли» и «не измерили» — единственное, ради чего этот отчёт существует.
    """
    plan_keys = _trade_plan_keys()
    paths = [p for p, _ in _NUMERIC] + [p for p, _ in _CATEGORICAL]

    missing = sorted({
        path.split(".")[0] for path in paths
        if not path.startswith("signal.") and path.split(".")[0] not in plan_keys
    })

    assert missing == [], f"признаки замеряются, но в план не пишутся: {missing}"


def test_latched_entries_form_their_own_cohort(db):
    """(#latch-cohort-2026-09-05) Защёлка импульса впускает сделки с пометкой
    `adx_rising_latched`. Это единственный способ ответить, стали ли входы,
    пойманные в фазе импульса, лучше остальных: без разреза они растворятся в
    общей массе, и вооружение защёлки останется непроверяемым.
    """
    for _ in range(3):
        s = _sig(db, reason="stop_loss")
        s.plan_json = {**s.plan_json,
                       "tz_shadow": {"enforce_reason": "adx_rising_latched"}}
    for _ in range(2):
        s = _sig(db, reason="tp2_reached", net=2.0)
        s.plan_json = {**s.plan_json,
                       "tz_shadow": {"enforce_reason": "enabled_conditions_passed"}}
    db.flush()

    levels = next(c for c in build(db, min_group=1)["categorical"]
                  if c["feature"] == "tz_shadow.enforce_reason")["levels"]

    assert levels["adx_rising_latched"]["n"] == 3
    assert levels["adx_rising_latched"]["stop_rate"] == pytest.approx(1.0)
    assert levels["enabled_conditions_passed"]["stop_rate"] == pytest.approx(0.0)


# ── запас гейта достижимости ────────────────────────────────────────────────

def _reach(db, *, reason: str, net: float, hit: float, required: float,
           uncensored: float | None = None):
    """Сделка с записанным решением гейта достижимости — как его пишет
    боевой путь: `plan_json["tp_reach"] = TPReach.as_dict()`."""
    s = _sig(db, reason=reason, net=net)
    s.plan_json = {**s.plan_json, "tp_reach": {
        "tp1_dist_pct": 1.0, "tp2_dist_pct": 3.0,
        "tp2_hit_rate": hit, "required_hit_rate": required,
        "tp2_hit_rate_uncensored": uncensored,
        "would_block": not (hit >= required
                            or (uncensored is not None and uncensored >= required)),
    }}
    db.flush()
    return s


def test_margin_reproduces_the_gates_own_verdict():
    """Знак запаса обязан совпадать с `would_block`, иначе рядом в одном отчёте
    окажутся два несогласных ответа на один вопрос. Числа — с ленты 07.09.
    """
    # #472 AVAX: гейт пропустил (reward_reached_often_enough).
    allowed = {"tp2_hit_rate": 0.1562, "required_hit_rate": 0.0828}
    # #475 ETH: гейт остановил бы — не спасла и нецензурированная частота.
    blocked = {"tp2_hit_rate": 0.0952, "required_hit_rate": 0.3547,
               "tp2_hit_rate_uncensored": 0.1176}

    assert _tp_reach_margin(None, {"tp_reach": allowed}) > 0
    assert _tp_reach_margin(None, {"tp_reach": blocked}) < 0


def test_margin_counts_the_rescue_by_uncensored_rate():
    """Гейт считает вход допустимым, если порог берёт ЛИБО сырая частота, ЛИБО
    нецензурированная. Без этого сделки, впущенные именно спасением, выглядели
    бы в разборе остановленными — признак спорил бы с вердиктом рядом."""
    rescued = {"tp2_hit_rate": 0.10, "required_hit_rate": 0.20,
               "tp2_hit_rate_uncensored": 0.25}

    assert _tp_reach_margin(None, {"tp_reach": rescued}) == pytest.approx(0.05)


def test_margin_is_missing_not_zero_when_the_gate_left_no_numbers():
    """Ноль — «ровно на пороге», и это осмысленное значение. Сделка без замера
    обязана выпасть из выборки, а не встать в неё серединой."""
    assert _tp_reach_margin(None, {}) is None
    assert _tp_reach_margin(None, {"tp_reach": {"tp2_hit_rate": 0.2}}) is None
    assert _tp_reach_margin(None, {"tp_reach": {"required_hit_rate": 0.2}}) is None


def test_margin_separates_where_the_verdict_cannot(db):
    """Ради этого признак и заведён. У всех пяти сделок вердикт одинаков
    (`would_block=true`), и разрез по нему вырожден — ровно то, что показал
    замер 07.09: восемь осуждённых из восьми. Запас при этом различает."""
    for i in range(3):
        _reach(db, reason="stop_loss", net=-1.0,
               hit=0.02 + i * 0.01, required=0.30)
    for i in range(3):
        _reach(db, reason="tp2_reached", net=2.0,
               hit=0.20 + i * 0.01, required=0.30)

    out = build(db, min_group=3)
    verdicts = next(c for c in out["categorical"]
                    if c["feature"] == "tp_reach.would_block")["levels"]
    margin = _rows(out, "tp_reach.margin")

    assert list(verdicts) == ["True"], "ветви для сравнения не должно быть"
    assert margin["auc"] == 0.0, "у стопнутых запас ниже — признак это видит"


def test_margin_reads_fields_the_gate_actually_writes():
    """(#tp-reach-margin-2026-09-07) План — это `asdict(TPReach)`, поэтому
    переименование поля в гейте не сломает ничего громко: признак просто станет
    возвращать None, и ось молча исчезнет из отчёта. Тест читает имена прямо из
    кода признака и сверяет их с самим датаклассом.
    """
    import inspect
    import re
    from dataclasses import fields

    from services.tp_reachability import TPReach

    used = set(re.findall(r'reach\.get\("([^"]+)"\)',
                          inspect.getsource(_tp_reach_margin)))
    known = {f.name for f in fields(TPReach)}

    assert used, "признак перестал читать план — тест потерял смысл"
    assert used <= known, f"признак читает несуществующие поля гейта: {used - known}"


# ── исход tp1: отличает ли вход дошедших до TP1 (#tp1-forensics-2026-09-11) ──

def _tp1_sig(db, *, mfe, adx=25.0, symbol="X/USDT", tp1_hit_rate=None,
             reason="breakeven_stop"):
    """TP1 на 101 при входе 100 — дистанция 1%. Дошла, если MFE ≥ 1.0."""
    signal = _sig(db, reason=reason, adx=adx)
    signal.symbol = symbol
    plan = dict(signal.plan_json)
    plan["lifecycle"] = {"entry_price": 100.0, "mfe_pct": mfe}
    if tp1_hit_rate is not None:
        plan["tp_reach"] = dict(plan["tp_reach"], tp1_hit_rate=tp1_hit_rate)
    signal.plan_json = plan
    db.flush()
    return signal


def test_tp1_outcome_splits_by_reaching_tp1_not_by_close_reason(db):
    """Дошедшие входили при высоком ADX. Причина закрытия у всех одна и та же —
    деление идёт по траектории, а не по тому, чем сделка кончилась."""
    for _ in range(6):
        _tp1_sig(db, mfe=1.4, adx=35.0)
    for _ in range(6):
        _tp1_sig(db, mfe=0.4, adx=15.0)

    out = build(db, min_group=5, outcome="tp1")
    row = _rows(out, "tz_shadow.adx")

    assert out["reached"]["n"] == 6 and out["not_reached"]["n"] == 6
    assert out["base_rate"] == 0.5
    assert row["n_reached"] == 6
    assert row["auc"] == 1.0 and row["higher_in"] == "reached"
    assert row["ci_excludes_half"] is True


def test_the_measured_tp1_rate_is_judged_as_a_gate_would_use_it(db):
    """tp_reach.tp1_hit_rate — ровно то, по чему судил бы гейт на TP1. Если
    его AUC около 0.5, гейт на нём не отбирает, а только запрещает."""
    for _ in range(6):
        _tp1_sig(db, mfe=1.4, tp1_hit_rate=0.30)
    for _ in range(6):
        _tp1_sig(db, mfe=0.4, tp1_hit_rate=0.30)

    row = _rows(build(db, min_group=5, outcome="tp1"), "tp_reach.tp1_hit_rate")

    assert row["auc"] == 0.5
    assert row["ci_excludes_half"] is False


def test_trades_without_a_trajectory_are_skipped_not_guessed(db):
    _sig(db, reason="stop_loss")          # без lifecycle
    _tp1_sig(db, mfe=1.4)

    out = build(db, min_group=1, outcome="tp1")

    assert out["skipped_no_trajectory"] == 1
    assert out["reached"]["n"] == 1 and out["not_reached"]["n"] == 0


def test_levels_carry_the_reach_rate_with_an_interval(db):
    for _ in range(3):
        _tp1_sig(db, mfe=1.4, symbol="LTC/USDT")
    _tp1_sig(db, mfe=0.4, symbol="LTC/USDT")
    for _ in range(4):
        _tp1_sig(db, mfe=0.4, symbol="ADA/USDT")

    out = build(db, min_group=1, outcome="tp1")
    symbols = next(r for r in out["categorical"] if r["feature"] == "signal.symbol")

    ltc = symbols["levels"]["LTC/USDT"]
    assert ltc["reached"] == 3 and ltc["not_reached"] == 1
    assert ltc["reach_rate"] == 0.75
    lo, hi = ltc["reach_rate_ci"]
    assert lo < 0.75 < hi, "на четырёх сделках интервал обязан быть широким"


def test_the_stop_outcome_keeps_its_keys():
    """На ключах stop построены команды разбора владельца — их не трогаем."""
    from services.stop_loss_forensics import _OUTCOMES
    assert _OUTCOMES["stop"] == ("stopped", "survived", "stop_rate")


def test_an_unknown_outcome_is_refused(db):
    with pytest.raises(ValueError):
        build(db, outcome="tp2")


def test_auc_interval_is_narrow_on_many_trades_and_wide_on_few():
    from services.stop_loss_forensics import _auc_ci
    lo_few, hi_few = _auc_ci(0.6, 10, 10)
    lo_many, hi_many = _auc_ci(0.6, 100, 370)
    assert lo_few < 0.5 < hi_few
    assert 0.5 < lo_many < 0.6 < hi_many
