// Человекочитаемые ярлыки причин закрытия сделки.
//
// (#close-reason-labels-2026-09-07) Жили только в журнале сигналов, и лента
// решений показывала те же коды машинными: `stop_loss`, `tp2_reached`,
// `position_opened` стояли в её белом списке, но ярлыка не имели, а фолбэк там
// — сам код. Один и тот же исход подписан на одном экране и не подписан на
// соседнем.
//
// Общий модуль вместо копии: GradeBadge жил в трёх экземплярах, и правка в
// одном из них оставила две страницы, продолжавшие врать. Здесь та же ловушка —
// список причин пополняется каждым раундом правок выхода.
export const CLOSE_REASON_LABELS: Record<string, string> = {
  tp2_reached: "TP2 достигнут",
  tp1_reached: "TP1 достигнут",
  stop_loss: "Стоп",
  breakeven_stop: "Безубыток-стоп (после TP1)",
  scalp_time_stop: "Скальп: тайм-стоп",
  low_grade_capital_release: "Слабый грейд: высвобождение капитала",
  manual_close: "Закрыто вручную (по рынку)",
  manual_cancel: "Отменено вручную",
  manual_profit_close: "Закрыто вручную (+)",
  manual_loss_close: "Закрыто вручную (−)",
  failed_setup_exit: "Сетап не подтвердился",
  breakeven_lock: "Безубыток-замок",
  scalp_breakeven_lock: "Скальп: безубыток-замок",
  scalp_flow_exit: "Скальп: выход по потоку",
  trend_ride_trailing_stop: "Трейл по тренду",
  adaptive_post_tp1_stop: "Трейл после TP1",
  trend_trailing_stop: "Трейл по тренду",
  adaptive_trailing_stop: "Адаптивный трейл",
  protective_trailing_stop: "Защитный трейл",
  protective_breakeven_profit_guard: "Защита безубытка",
  adaptive_mfe_capture: "Фиксация MFE",
  wide_stop_tp2_guard: "Защита TP2 (широкий стоп)",
  // (#trend-capture-band-2026-07-25) Ярус 2: фиксация в модальной полосе MFE.
  // До правки сделки с MFE 0.35–0.8% в тренде не имели механизма фиксации.
  trend_capture_band: "Трендовая фиксация (полоса MFE)",
  // (#tz-mfe-giveback-backstop-2026-09-02) ТЗ-выход смотрит только на слом
  // структуры (KAMA/ADX/OBV), не на отданную прибыль — бэкстоп фиксирует по
  // текущей цене сделку, которая отдала бОльшую часть значимого MFE.
  tz_mfe_giveback_backstop: "ТЗ: фиксация отданной прибыли",
  // (#progressive-tp2-2026-09-03) TP2 стал этапом, а не потолком: на нём
  // фиксируется доля остатка, хвост едет под трейлом.
  tp2_partial: "TP2: частичная фиксация",
  tp2_trail_stop: "Трейл после TP2",
  tp2_trail_giveback: "TP2: хвост отдал прибыль",
  // (#post-tp1-dead-zone-2026-09-03) Защита прибыли между TP1 и TP2.
  post_tp1_giveback_trail: "Фиксация отдачи после TP1",
};

export function closeReasonLabel(code: string | null | undefined): string {
  if (!code) return "-";
  return CLOSE_REASON_LABELS[code] || code;
}
