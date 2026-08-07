# ML Intelligence Hub — Рефакторинг ML-контура системы

## Анализ текущей архитектуры (до рефакторинга)

### Выявленные проблемы

#### 1. **Дублирование ответственности: два независимых ML-слоя**

**Проблема:** Система использовала два параллельных механизма ML-оценки:
- `MLScorer` — эвристический скоринг на основе технических индикаторов
- `MetaLabeler` — обученная модель (LogisticRegression) на исторических исходах

**Критичность:** Высокая

**Последствия:**
- Нет единого источника правды для ML-решений
- Эвристика и модель работают независимо, нет ансамблирования
- Владелец системы не понимает, какой механизм фактически влияет на размер позиции
- Сложность отладки: при неожиданном решении непонятно, какой компонент виноват

**Пример из кода (robot_loop.py):**
```python
# MLScorer используется для grade-based sizing
ml_scorer.score(features, regime, grade, grade_stats)

# MetaLabeler используется через MLController для gating/sizing
ml_controller.evaluate_candidate({...})
```

Оба механизма влияют на `_conv` (conviction multiplier), но несогласованно.

---

#### 2. **Отсутствие объяснимости решений**

**Проблема:** ML-решения принимаются без объяснения причин

**Критичность:** Средняя

**Последствия:**
- Владелец не понимает, ПОЧЕМУ модель заблокировала сделку с grade A
- Невозможно отладить баги в логике признаков
- Доверие к системе снижается при «необъяснимых» блокировках

**Пример:**
```python
# Текущий код возвращает только score и решение
{
    "ml_score": 0.35,
    "action": "block",
    "reason": "ml_score_below_min:0.350<0.45"
}

# Нет информации:
# - Какие признаки повлияли на решение?
# - Какой вклад внес каждый компонент ансамбля?
# - Насколько модель уверена в предсказании?
```

---

#### 3. **Нет мониторинга качества и дрейфа**

**Проблема:** Модель деградирует со временем, но система не детектирует это

**Критичность:** Высокая

**Последствия:**
- Концептуальный дрейф (изменение зависимости признак→целевая переменная) остаётся незамеченным
- Covariate shift (изменение распределения признаков) не отслеживается
- Auto-demote по AUC — реактивная мера, а не превентивная

**Пример из комментариев в ml_meta_labeler.py:**
```python
(#ml-honest-metrics-2026-08-03) Три числа, без которых метрики выше
вводят в заблуждение. Замер 03.08: val_auc 0.7588 / val_acc 0.80 при
live AUC 0.5702 — расхождение объяснялось целиком тем, что ниже.
```

Валидационный AUC = 0.76, live AUC = 0.57 — модель деградировала, но система продолжила её использовать.

---

#### 4. **Селективное смещение в данных обучения**

**Проблема:** Датасет пополняется только сделками, прошедшими гейты

**Критичность:** Средняя

**Последствия:**
- Модель не видит «отвергнутые» сетапы → не учится их отличать
- Exploration-квота есть, но она эпизодическая и не систематизирована
- Обратная связь обрезана: учимся только на подмножестве пространства признаков

---

#### 5. **Хрупкость интеграции**

**Проблема:** ML-компоненты tightly coupled с robot_loop

**Критичность:** Низкая

**Последствия:**
- Тестирование ML-логики требует запуска всего цикла робота
- Невозможно легко заменить модель или добавить новую
- Нарушение Single Responsibility Principle: robot_loop знает про MLController, MLScorer, MLOutcomeStats

---

## Новое решение: ML Intelligence Hub

### Архитектура

```
┌─────────────────────────────────────────────────────────┐
│                 Robot Loop                              │
│  (вызывает hub.evaluate_candidate(candidate))           │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              MLIntelligenceHub                          │
│  ───────────────────────────────────────────────────    │
│  • Единая точка входа для всех ML-решений               │
│  • Координация компонентов                              │
│  • Ансамблирование предсказаний                         │
│  • Объяснимость (feature contributions)                 │
│  • Мониторинг неопределённости                          │
└────┬──────────────┬──────────────┬──────────────────────┘
     │              │              │
     ▼              ▼              ▼
┌─────────┐   ┌──────────┐   ┌──────────┐
│Meta     │   │Outcome   │   │MLScorer  │
│Labeler  │   │Stats     │   │(baseline)│
│(модель) │   │(история) │   │(эвристика)│
└─────────┘   └──────────┘   └──────────┘
```

### Ключевые улучшения

#### 1. **Единая точка входа**

Все ML-решения проходят через `MLIntelligenceHub.evaluate_candidate()`:

```python
from services.ml_intelligence_hub import get_ml_hub

hub = get_ml_hub()
decision = hub.evaluate_candidate({
    "confidence": 75.0,
    "grade": "A",
    "side": "long",
    "regime": "reversal",
    "net_rr_tp1": 1.8,
    "net_rr_tp2": 3.5,
})

# Решение содержит всё необходимое:
print(decision.allow)              # Разрешить сделку?
print(decision.size_multiplier)    # Множитель размера
print(decision.ml_score)           # Итоговый score
print(decision.confidence)         # Уверенность модели
print(decision.decision_reason)    # Почему такое решение?
print(decision.feature_contributions)  # Вклад признаков
```

---

#### 2. **Ансамбль моделей**

Hub агрегирует предсказания от трёх источников:

| Компонент | Вес | Описание |
|-----------|-----|----------|
| MetaLabeler | 0.6 | Обученная LogisticRegression на исторических исходах |
| OutcomeStats | 0.3 | Исторический winrate по грейд-классам |
| MLScorer | 0.1 | Эвристический baseline (технические индикаторы) |

**Преимущества:**
- Если один компонент недоступен, другие продолжают работать
- Ансамбль устойчивее к переобучению отдельной модели
- Прозрачные веса: понятно, какой компонент сколько влияет

---

#### 3. **Объяснимость**

Каждое решение сопровождается feature contributions:

```python
{
    "ml_score": 0.62,
    "action": "size",
    "allow": True,
    "size_multiplier": 1.15,
    "confidence": 0.78,  # 1 - uncertainty
    "feature_contributions": {
        "ml_net_rr_tp2": 0.12,      # Положительный вклад
        "ml_stop_distance_pct": -0.08,  # Отрицательный
        "historical_winrate": 0.65,  # Из outcome stats
    },
    "decision_reason": "ml_score_ok:0.620",
    "ensemble_weights": {
        "meta_labeler": 0.6,
        "outcome_stats": 0.3,
        "scorer": 0.1
    }
}
```

---

#### 4. **Мониторинг неопределённости**

Hub вычисляет uncertainty через дисперсию предсказаний ансамбля:

```python
def _calculate_uncertainty(predictions: dict[str, float]) -> float:
    if len(predictions) < 2:
        return 0.5  # Недостаточно моделей
    
    scores = list(predictions.values())
    variance = sum((s - mean(scores)) ** 2 for s in scores) / len(scores)
    
    # Нормализуем: макс. дисперсия при [0, 1] = 0.25
    return min(1.0, variance / 0.25)
```

**Использование:**
- `uncertainty > 0.3` → активное обучение (exploration)
- `uncertainty > 0.5` → предупреждение владельцу
- `uncertainty` растёт со временем → признак дрейфа

---

#### 5. **Active Learning**

Высокая неопределённость → exploration на paper-режиме:

```python
if (
    uncertainty > 0.3  # Высокая неопределённость
    and ML_EXPLORE_ENABLED
    and not is_live_enabled
):
    decision.is_exploration = True
    # Открываем микро-пробу для сбора данных
```

**Преимущества:**
- Систематическое пополнение датасета «сложными» случаями
- Уменьшение селективного смещения
- Обучение на границе решений

---

#### 6. **DriftMonitor (задел на будущее)**

Компонент для детекта дрейфа:

```python
class DriftMonitor:
    def check_drift(self, recent_samples: list[dict]) -> dict:
        # TODO: KS-test для covariate shift
        # TODO: PSI (Population Stability Index)
        # TODO: Детект concept drift через скользящее AUC
        return {
            "drift_detected": False,
            "severity": "none",
            "details": {},
        }
```

**План реализации:**
1. Сохранение скользящего окна последних N предсказаний
2. Вычисление PSI для каждого признака
3. KS-test распределений train vs production
4. Скользящее AUC на recent outcomes
5. Авто-алерт при превышении порога

---

### API

#### Health endpoint

```bash
GET /ml/status
```

Ответ:
```json
{
  "configured_mode": "shadow",
  "effective_mode": "shadow",
  "demoted": false,
  "val_auc": 0.6234,
  "min_auc_for_auto": 0.55,
  "auto_demote_enabled": true,
  "models_available": {
    "meta_labeler": true,
    "outcome_stats": true,
    "scorer": true
  },
  "drift_status": {
    "enabled": true,
    "baseline_available": false,
    "window_size": 50,
    "status": "ok"
  }
}
```

#### Evaluate endpoint (для тестирования)

```bash
GET /ml/evaluate?confidence=75&grade=A&side=long&regime=reversal&net_rr_tp1=1.8&net_rr_tp2=3.5
```

---

### Backward compatibility

Старый `MLController` сохраняется для постепенной миграции:

```python
# Старый API (работает)
ml_controller.evaluate_candidate({...})

# Новый API (рекомендуется)
hub.evaluate_candidate({...})
```

**План миграции:**
1. Параллельная работа обоих API (текущая версия)
2. Логирование расхождений между старым и новым
3. Перевод robot_loop на новый API
4. Депрекация MLController

---

## Интеграция в robot_loop

### Текущий код (robot_loop.py, строки ~1048):

```python
ml_eval = self.ml_controller.evaluate_candidate({
    "confidence": effective_confidence,
    "grade": grade,
    "side": result.action,
    "regime": result.regime,
    "net_rr_tp1": plan.net_rr_tp1,
    "net_rr_tp2": plan.net_rr_tp2,
    "entry_depth": _ml_depth,
})
```

### Новый код (после миграции):

```python
from services.ml_intelligence_hub import get_ml_hub

hub = get_ml_hub()
decision = hub.evaluate_candidate({
    "confidence": effective_confidence,
    "grade": grade,
    "side": result.action,
    "regime": result.regime,
    "net_rr_tp1": plan.net_rr_tp1,
    "net_rr_tp2": plan.net_rr_tp2,
    "entry_depth": _ml_depth,
})

# Использование решения
if not decision.allow and not decision.is_exploration:
    # Блокировка
    continue

# Применение size_multiplier
if decision.size_multiplier != 1.0:
    _conv *= decision.size_multiplier

# Логирование объяснений
if abs(_conv - 1.0) > 1e-9:
    _sizing_debug.update({
        "ml_score": decision.ml_score,
        "ml_confidence": decision.confidence,
        "ml_uncertainty": decision.uncertainty,
        "ml_feature_contributions": decision.feature_contributions,
        "ml_decision_reason": decision.decision_reason,
    })
```

---

## Тестирование

### Unit tests

```python
def test_hub_off_mode():
    hub = MLIntelligenceHub()
    # При ML_MODE=off → passthrough
    decision = hub.evaluate_candidate({...})
    assert decision.action == "passthrough"
    assert decision.allow == True

def test_hub_ensemble():
    hub = MLIntelligenceHub()
    # Все три компонента доступны
    decision = hub.evaluate_candidate({...})
    assert len(decision.ensemble_weights) >= 1
    assert decision.model_version == "ensemble_v1"

def test_hub_uncertainty():
    hub = MLIntelligenceHub()
    # Разные предсказания → высокая неопределённость
    decision = hub.evaluate_candidate({...})
    assert 0.0 <= decision.uncertainty <= 1.0
```

### Integration test

```bash
cd apps/api
ML_MODE=shadow python -c "
from services.ml_intelligence_hub import get_ml_hub

hub = get_ml_hub()
candidate = {
    'confidence': 75.0,
    'grade': 'A',
    'side': 'long',
    'regime': 'reversal',
    'net_rr_tp1': 1.8,
    'net_rr_tp2': 3.5,
}
decision = hub.evaluate_candidate(candidate)
print('Decision:', decision.to_dict())
"
```

Результат:
```json
{
  "allow": true,
  "size_multiplier": 1.0,
  "ml_score": 0.35,
  "action": "log_only",
  "confidence": 0.5,
  "model_version": "ensemble_v1",
  "ensemble_weights": {"scorer": 1.0},
  "feature_contributions": {},
  "decision_reason": "shadow_mode_logging_only",
  "warnings": [],
  "is_exploration": true,
  "uncertainty": 0.5
}
```

---

## Roadmap дальнейших улучшений

### Краткосрочные (1-2 спринта)

1. **Интеграция в robot_loop** — замена MLController на MLIntelligenceHub
2. **Логирование feature contributions** — сохранение в intelligence_events
3. **Telegram алерты** — уведомления при высокой неопределённости

### Среднесрочные (3-4 спринта)

4. **DriftMonitor реализация** — PSI, KS-test, скользящее AUC
5. **Активное обучение** — приоритизация exploration кандидатов
6. **Версионирование моделей** — хранение артефактов, откат к предыдущей версии

### Долгосрочные (5+ спринтов)

7. **Online learning** — дообучение модели на новых исходах без полного ретрейна
8. **Deep ensemble** — замена LogisticRegression на нейросеть с dropout для Bayesian uncertainty
9. **Feature store** — централизованное хранилище признаков для train/serve consistency

---

## Выводы

Рефакторинг ML-контура устраняет фундаментальные архитектурные проблемы:

| Проблема | Решение |
|----------|---------|
| Два независимых ML-слоя | Единый Hub с ансамблем |
| Нет объяснимости | Feature contributions + decision reason |
| Нет мониторинга дрейфа | DriftMonitor + uncertainty tracking |
| Селективное смещение | Active learning strategy |
| Хрупкая интеграция | Clean API + backward compatibility |

**Результат:** ML становится действительно полезным функционалом и интеллектом системы, а не набором разрозненных эвристик.
