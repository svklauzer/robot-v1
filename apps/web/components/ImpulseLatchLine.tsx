// (#entry-impulse-2026-09-04) Защёлка импульса входа, одна строка на двух
// страницах: карточка сигнала и лента решений.
//
// Трендовый кандидат — СОСТОЯНИЕ, оно требует согласия 4h и 1h и держится
// сутками. Условия ТЗ — СОБЫТИЯ длиной в бар. Требовать их одновременно значит
// почти всегда опаздывать: замер 04.09 дал 68 отказов из 71 по `adx_not_rising`
// при медиане adx_delta −0.589. Защёлка помнит импульс, пока состояние
// подтверждается, и её возраст объясняет, ПОЧЕМУ вход прошёл при падающем ADX.
//
// Компонент общий намеренно: GradeBadge жил в трёх копиях, и правка палитры в
// одной из них оставила две страницы, продолжавшие врать. Повторять это на
// защёлке не с чего — тем более что подменять условие имеет право только
// импульс ПО ADX, и это различие обязано выглядеть одинаково везде.

const IMPULSE_KIND: Record<string, string> = {
  adx_turned_up: "ADX развернулся вверх",
  stoch_crossed: "Stoch пересёк сигнальную",
};

const TITLE =
  "Импульс на младшем ТФ случается раньше, чем тренд проступит на 4h и 1h. " +
  "Защёлка держит событие, пока состояние подтверждается; ни одно условие при " +
  "этом не ослаблено. Снять отказ adx_rising может только импульс ПО ADX — " +
  "кросс Stoch записывается как наблюдение, но основанием не является.";

export default function ImpulseLatchLine({ latch }: { latch?: any }) {
  if (!latch) return null;

  const impulse = latch.impulse;
  const kind = impulse?.kind ? IMPULSE_KIND[impulse.kind] || impulse.kind : null;
  // Только разворот ADX подменяет собственное условие; всё прочее — наблюдение.
  const substitutes = latch.mode === "enforce" && latch.live && impulse?.kind === "adx_turned_up";

  return (
    <div className="mt-1 text-[11px]" title={TITLE}>
      <span className="text-emerald-100/50">Импульс: </span>
      {latch.live ? (
        <span className={substitutes ? "text-emerald-300" : "text-emerald-100/60"}>
          {kind}
          {impulse?.age_sec != null && ` ${Math.round(impulse.age_sec / 60)} мин назад`}
        </span>
      ) : (
        <span className="text-emerald-100/40">не было в окне</span>
      )}
      {latch.live && !substitutes && (
        <span className="text-emerald-100/30"> · отказ не снимает</span>
      )}
      {latch.mode === "shadow" && <span className="text-emerald-100/30"> · наблюдение</span>}
    </div>
  );
}
