// Все вызовы идут через same-origin серверный прокси (app/api/proxy/[...path]).
// Owner-токен НЕ хранится на клиенте — его подставляет прокси на сервере.
const API_BASE = "/api/proxy";

// (#ux-errors-2026-07-09) В ошибку включается detail тела ответа — иначе 403
// debug-гейта в production выглядел как «кнопка не работает» без объяснения.
async function throwApiError(res: Response): Promise<never> {
  let detail = "";
  try {
    const data = await res.json();
    detail = data?.detail || data?.error || JSON.stringify(data);
  } catch {
    /* тело не JSON — оставляем только статус */
  }
  throw new Error(`API ${res.status}${detail ? `: ${detail}` : ""}`);
}

export async function apiGet(path: string) {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });

  if (!res.ok) {
    await throwApiError(res);
  }

  return res.json();
}

export async function apiPost(path: string, body?: any) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    await throwApiError(res);
  }

  return res.json();
}
