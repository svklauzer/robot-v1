// (#ux-errors-2026-07-09, вынесено 07.09.2026) Ошибка действия не должна
// выглядеть как «кнопка не работает».
//
// `apiGet`/`apiPost` бросают на любом не-2xx. Журнал сигналов это ловил и
// объяснял, а страницы подписчиков и платежей — нет: там `await apiPost(...)`
// стоял голым. Нажал «продлить на 30 дней», запрос упал — экран просто не
// изменился, и это неотличимо от «продлил, но список не обновился». На
// страницах, которые выдают платный доступ и подтверждают оплаты, молчание
// дороже всего.
//
// Второй слой — мягкие ошибки: бэкенд отвечает 200 с телом
// `{"status": "error", "error": ...}`. Такой ответ до сюда не бросит ничего,
// поэтому его превращаем в исключение явно.

export function assertOk(resp: any) {
  if (resp && resp.status === "error") {
    throw new Error(String(resp.error || "unknown_error"));
  }
  return resp;
}

/** Показывает причину отказа. `hints` — подстрока ответа → человеческий текст. */
export function reportActionError(e: unknown, hints: Record<string, string> = {}) {
  const msg = String((e as any)?.message ?? e);

  for (const [needle, text] of Object.entries(hints)) {
    if (msg.includes(needle)) {
      alert(text);
      return;
    }
  }

  alert(`Действие не выполнено: ${msg}`);
}
