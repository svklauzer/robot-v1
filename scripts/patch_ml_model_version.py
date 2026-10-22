"""Версия модели в каждой записи ml_score (plan.ml -> trade_outcomes.jsonl).

Зачем: модель переобучается ежедневно, а сделка закрывается позже входа, поэтому по
закрытой сделке нельзя восстановить, КАКАЯ версия считала её score. Без этого живой AUC
смешивает версии (на дашборде 0.49 — смесь до и после исправлений serve-parity).

Что делает
  ml_controller.py : evaluate_candidate добавляет в ответ model_trained_at и feature_version
                     (когда ml_score посчитан). Сбой чтения версии -> поля None, ответ не меняется.
  ml_trade_logger.py: пишет их в строку как ml_model_trained_at и ml_feature_version.
Торговлю и значения score не меняет. Старые строки остаются без версии (поля None/отсутствуют).

Запуск из корня репозитория: python scripts/patch_ml_model_version.py
Идемпотентен, делает .bak-before-model-version, UTF-8, переводы строк сохраняются.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
CTL = ROOT / "apps/api/services/ml_controller.py"
LOG = ROOT / "apps/api/services/ml_trade_logger.py"


def load(p):
    raw = p.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def save(p, text, crlf):
    p.with_name(p.name + ".bak-before-model-version").write_bytes(p.read_bytes())
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] якорь найден {n} раз(а), ожидался 1 - файл не менялся")
    return text.replace(old, new)


# ── ml_controller.py ─────────────────────────────────────────────────────────
t, crlf = load(CTL)
if "_model_version" in t:
    print("ml_controller: уже применено")
else:
    method = '''    def _model_version(self) -> dict:
        """Версия модели, посчитавшей score (#model-version-2026-10-22).

        Пишется в plan.ml и дальше в trade_outcomes.jsonl: без неё живой AUC смешивает
        версии модели. Любой сбой -> None-поля, на ответ контроллера не влияет.
        """
        try:
            labeler = self._get_labeler()
            st = labeler.status() if labeler else {}
            fv = st.get("feature_version")
            if fv is None:
                from services.ml_features import FEATURE_VERSION
                fv = FEATURE_VERSION
            return {"model_trained_at": st.get("trained_at"), "feature_version": fv}
        except Exception:  # noqa: BLE001 — версия не критична
            return {"model_trained_at": None, "feature_version": None}

    def evaluate_candidate(self, candidate: dict) -> dict:
'''
    t = once(t, "    def evaluate_candidate(self, candidate: dict) -> dict:\n", method, "ctl-method")
    t = once(t, '        if mode == "shadow":\n            return {"mode": "shadow", "ml_score"',
             '        ver = self._model_version()\n        if mode == "shadow":\n            return {**ver, "mode": "shadow", "ml_score"', "ctl-shadow")
    t = once(t, 'return {"mode": "advisory", "ml_score"', 'return {**ver, "mode": "advisory", "ml_score"', "ctl-advisory")
    t = once(t, 'return {"mode": "full_auto", "ml_score": round(ml_score, 4), "action": "block",',
             'return {**ver, "mode": "full_auto", "ml_score": round(ml_score, 4), "action": "block",', "ctl-block")
    t = once(t, 'return {"mode": "full_auto", "ml_score": round(ml_score, 4), "action": "size",',
             'return {**ver, "mode": "full_auto", "ml_score": round(ml_score, 4), "action": "size",', "ctl-size")
    save(CTL, t, crlf)
    print("ml_controller: ok")

# ── ml_trade_logger.py ───────────────────────────────────────────────────────
t, crlf = load(LOG)
if "ml_model_trained_at" in t:
    print("ml_trade_logger: уже применено")
else:
    t = once(t, '            "ml_action": ml_data.get("action"),\n',
             '            "ml_action": ml_data.get("action"),\n'
             '            # (#model-version-2026-10-22) Какая версия модели считала score на входе.\n'
             '            "ml_model_trained_at": ml_data.get("model_trained_at"),\n'
             '            "ml_feature_version": ml_data.get("feature_version"),\n', "log")
    save(LOG, t, crlf)
    print("ml_trade_logger: ok")
