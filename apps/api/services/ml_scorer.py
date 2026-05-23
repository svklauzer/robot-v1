from dataclasses import dataclass

@dataclass
class MLScore:
    probability: float
    confidence: float
    multiplier: float

class MLScorer:
    def score(self, features: dict, regime: str) -> MLScore:
        score = 0.5

        if regime == "trend_up" and features["last_close"] > features["ema20"]:
            score += 0.1
        if regime == "trend_down" and features["last_close"] < features["ema20"]:
            score += 0.1
        if features["volume"] > features["volume_ma"]:
            score += 0.1

        probability = max(0.0, min(0.95, score))
        confidence = probability * 100
        multiplier = 1.0 if probability < 0.7 else 1.25

        return MLScore(probability=probability, confidence=confidence, multiplier=multiplier)