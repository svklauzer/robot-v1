from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MLScore:
    probability: float
    confidence: float
    multiplier: float
    grade_multiplier: float = 1.0    # data-driven adjustment from historical outcomes
    grade_stats_used: bool = False   # True when real outcome stats were applied


class MLScorer:
    """
    Multi-factor ML-style scorer v2.

    Now supports data-driven grade confidence multiplier: when `grade_stats`
    dict is provided (from MLOutcomeStatsService.grade_stats()), the final
    confidence is adjusted based on each grade's historical win rate vs the
    neutral baseline of 50%.

    Multiplier logic (applied after all heuristic factors):
      winrate >= 60%  → +8%   (historically strong grade)
      winrate >= 50%  → +4%
      winrate >= 40%  → neutral (no change)
      winrate >= 30%  → -6%   (historically weak)
      winrate <  30%  → -12%  (historically very weak)

    The adjustment is capped so final probability stays in [BASE_FLOOR, 0.95].
    """
    """
    Multi-factor ML-style scorer v2.

    Replaces the trivial 3-heuristic v1 (score=0.5 + 3 bumps).

    Factors weighted by empirical importance from trade_outcomes analysis:
    1. EMA alignment (trend structure)        — up to +0.30
    2. RSI momentum zone                      — up to +0.12 / -0.05
    3. MACD histogram direction & magnitude   — up to +0.13 / -0.05
    4. Volume confirmation                    — up to +0.15 / -0.05
    5. Grade penalty/boost (passed as kwarg)  — up to ±0.10

    Base floor = 0.35 so even poor setups produce a finite score that
    downstream gates can compare against their thresholds.

    Score range → [0.35, 0.95]
    Confidence  = score * 100  → [35, 95]
    Multiplier  = 1.0 / 1.25 / 1.50 depending on tier
    """

    # Weights
    W_EMA_PRICE_ABOVE_20 = 0.12
    W_EMA_PRICE_ABOVE_50 = 0.08
    W_EMA_20_ABOVE_50    = 0.10
    W_RSI_BULLISH_ZONE   = 0.12   # 45–65 for long, 35–55 for short
    W_RSI_OVERBOUGHT_PEN = 0.05   # penalty when RSI > 68 (long) or < 32 (short)
    W_MACD_CONFIRM       = 0.13   # histogram direction + growing
    W_MACD_WEAK          = 0.05   # histogram correct direction but shrinking
    W_MACD_WRONG_PEN     = 0.05   # histogram against trade direction
    W_VOLUME_STRONG      = 0.15   # volume ≥ 1.3× MA
    W_VOLUME_OK          = 0.08   # volume 1.1–1.3× MA
    W_VOLUME_WEAK_PEN    = 0.05   # volume < 0.8× MA

    BASE_FLOOR = 0.35

    def score(
        self,
        features: dict,
        regime: str,
        grade: str | None = None,
        grade_stats: dict[str, dict] | None = None,
    ) -> MLScore:
        """
        Score a trade setup.

        Args:
            features:     Technical indicator dict (ema20, ema50, rsi, macd_hist, etc.)
            regime:       "trend_up" | "trend_down" | other
            grade:        Signal grade string ("A+", "A", "B", "C")
            grade_stats:  Optional per-grade stats from MLOutcomeStatsService.grade_stats().
                          When provided, applies a data-driven confidence multiplier.
        """
        last_close  = float(features.get("last_close", 0) or 0)
        ema20       = float(features.get("ema20", 0) or 0)
        ema50       = float(features.get("ema50", 0) or 0)
        volume      = float(features.get("volume", 0) or 0)
        volume_ma   = float(features.get("volume_ma", 1) or 1)
        rsi         = float(features.get("rsi", 50.0) or 50.0)
        macd_hist   = float(features.get("macd_hist", 0) or 0)
        macd_hist_p = float(features.get("macd_hist_prev", 0) or 0)

        score = 0.0

        # ── 1. EMA alignment ────────────────────────────────────────────────────
        if regime == "trend_up":
            if last_close > ema20:
                score += self.W_EMA_PRICE_ABOVE_20
            if last_close > ema50:
                score += self.W_EMA_PRICE_ABOVE_50
            if ema20 > ema50:
                score += self.W_EMA_20_ABOVE_50

        elif regime == "trend_down":
            if last_close < ema20:
                score += self.W_EMA_PRICE_ABOVE_20
            if last_close < ema50:
                score += self.W_EMA_PRICE_ABOVE_50
            if ema20 < ema50:
                score += self.W_EMA_20_ABOVE_50

        # ── 2. RSI momentum zone ────────────────────────────────────────────────
        if regime == "trend_up":
            if 45.0 <= rsi <= 65.0:
                score += self.W_RSI_BULLISH_ZONE
            elif rsi > 68.0:
                score -= self.W_RSI_OVERBOUGHT_PEN   # overbought — risky long
        elif regime == "trend_down":
            if 35.0 <= rsi <= 55.0:
                score += self.W_RSI_BULLISH_ZONE
            elif rsi < 32.0:
                score -= self.W_RSI_OVERBOUGHT_PEN   # oversold — risky short

        # ── 3. MACD histogram confirmation ──────────────────────────────────────
        if regime == "trend_up":
            if macd_hist > 0:
                if macd_hist >= macd_hist_p:          # histogram growing → strong confirm
                    score += self.W_MACD_CONFIRM
                else:                                 # histogram positive but shrinking
                    score += self.W_MACD_WEAK
            elif macd_hist < 0:
                score -= self.W_MACD_WRONG_PEN        # MACD against long direction

        elif regime == "trend_down":
            if macd_hist < 0:
                if macd_hist <= macd_hist_p:          # histogram more negative → strong confirm
                    score += self.W_MACD_CONFIRM
                else:
                    score += self.W_MACD_WEAK
            elif macd_hist > 0:
                score -= self.W_MACD_WRONG_PEN

        # ── 4. Volume confirmation ───────────────────────────────────────────────
        if volume_ma > 0:
            vol_ratio = volume / volume_ma
            if vol_ratio >= 1.3:
                score += self.W_VOLUME_STRONG
            elif vol_ratio >= 1.1:
                score += self.W_VOLUME_OK
            elif vol_ratio < 0.8:
                score -= self.W_VOLUME_WEAK_PEN

        # ── 5. Grade adjustment ──────────────────────────────────────────────────
        grade_val = str(grade or "").upper()
        if grade_val == "A+":
            score += 0.10
        elif grade_val == "A":
            score += 0.05
        elif grade_val == "C":
            score -= 0.10   # Grade C setups historically weak — penalise confidence

        # ── Floor and ceiling ────────────────────────────────────────────────────
        probability = max(self.BASE_FLOOR, min(0.95, score))

        # ── 6. Data-driven grade multiplier ─────────────────────────────────────
        # Applied after all heuristic factors. Uses real trade outcome winrate
        # for this grade to nudge confidence toward historical reality.
        grade_multiplier = 1.0
        grade_stats_used = False
        if grade_stats and grade_val and grade_val in grade_stats:
            stats = grade_stats[grade_val]
            if stats.get("count", 0) >= 3:
                wr = float(stats.get("winrate", 50.0))
                if wr >= 60.0:
                    grade_multiplier = 1.08
                elif wr >= 50.0:
                    grade_multiplier = 1.04
                elif wr >= 40.0:
                    grade_multiplier = 1.00   # neutral
                elif wr >= 30.0:
                    grade_multiplier = 0.94
                else:
                    grade_multiplier = 0.88
                grade_stats_used = True

        probability = max(self.BASE_FLOOR, min(0.95, probability * grade_multiplier))
        confidence  = round(probability * 100, 2)

        if probability >= 0.85:
            multiplier = 1.50
        elif probability >= 0.70:
            multiplier = 1.25
        else:
            multiplier = 1.00

        return MLScore(
            probability=probability,
            confidence=confidence,
            multiplier=multiplier,
            grade_multiplier=grade_multiplier,
            grade_stats_used=grade_stats_used,
        )
