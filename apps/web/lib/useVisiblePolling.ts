"use client";

import { useEffect, useRef } from "react";

// (#visible-polling-2026-09-16) Опрос API, который спит в скрытой вкладке.
//
// 16.09 Render прислал два письма: исчерпан лимит трафика 5 ГБ и robot-api
// перезапущен за превышение памяти. Страницы опрашивали API раз в 5–10 с и
// продолжали это делать в свёрнутой вкладке — забытый дашборд расходовал
// трафик и запускал на бэке живой скан круглые сутки.
//
// Интервал прежний, но тик в скрытой вкладке пропускается; при возврате на
// вкладку данные обновляются сразу, если за время отсутствия тик был пропущен.
// Торговли это не касается: она идёт в фоновых циклах бэка, а не от опроса.
export function useVisiblePolling(
  fn: () => unknown,
  intervalMs: number,
  options: { enabled?: boolean; immediate?: boolean } = {},
  deps: unknown[] = [],
) {
  const { enabled = true, immediate = true } = options;
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;

    const visible = () =>
      typeof document === "undefined" || document.visibilityState === "visible";
    let missed = false;

    const tick = () => {
      if (visible()) {
        missed = false;
        void fnRef.current();
      } else {
        missed = true;
      }
    };

    if (immediate) tick();
    const timer = setInterval(tick, intervalMs);
    const onVisibility = () => {
      if (visible() && missed) tick();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, immediate, ...deps]);
}
