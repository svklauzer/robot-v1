// (#okx-satellite-exchange-routing-2026-09-02) Биржа, на которой сделка реально
// открыта — не текущая ACTIVE_EXCHANGE, которая может быть уже переключена.
//
// (#exchange-default-2026-09-07) Подстановки «htx» при отсутствии поля больше
// нет. Сейчас торгует OKX, то есть умолчание давало прямо неверный ответ на
// вопрос, ради которого значок и стоит: строка выглядела как замер, хотя
// означала «поля нет». Неизвестное показывается неизвестным.
//
// Компонент общий: значок жил в двух копиях (журнал сигналов и позиции), и обе
// несли одно и то же умолчание. GradeBadge в трёх копиях уже показал, чем это
// кончается — правка в одной оставляет остальные врать.
export default function ExchangeBadge({ exchange }: { exchange?: string | null }) {
  const ex = exchange ? String(exchange).toLowerCase() : null;

  if (!ex) {
    return (
      <span
        className="rounded-lg border border-slate-700 px-2 py-1 text-xs font-semibold uppercase text-slate-400"
        title="Биржа в записи не проставлена — это не значение по умолчанию, а отсутствие данных."
      >
        —
      </span>
    );
  }

  const cls = ex === "okx" ? "bg-sky-700 text-white" : "bg-slate-700 text-white";
  return (
    <span className={`rounded-lg px-2 py-1 text-xs font-semibold uppercase ${cls}`}>
      {ex}
    </span>
  );
}
