import { NextRequest, NextResponse } from "next/server";

// ── Заслон на ВЕСЬ owner-дашборд (страницы + /api/proxy) ──────────────────────
// Проблема: прокси /api/proxy/* сам подставляет OWNER_API_TOKEN на сервере, т.е.
// ЛЮБОЙ посетитель публичного URL получал права владельца (Start/Stop, kill-switch,
// закрытие сигналов, платежи, подписчики, Telegram). Этот middleware требует
// HTTP Basic Auth ПЕРЕД тем, как запрос дойдёт до страниц и до прокси.
//
// Включается, КОГДА заданы env BASIC_AUTH_USER и BASIC_AUTH_PASS на robot-web.
// Пока они не заданы — пропускает (чтобы не залочить себя до настройки), НО это
// небезопасно: задай обе переменные сразу после деплоя.

export const config = {
  // Защищаем всё, кроме статики Next и favicon (не чувствительно).
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

function timingSafeEqual(a: string, b: string): boolean {
  // Постоянное по времени сравнение, чтобы не утекала длина/совпадение по таймингу.
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function middleware(req: NextRequest) {
  const user = process.env.BASIC_AUTH_USER;
  const pass = process.env.BASIC_AUTH_PASS;

  // Защита не настроена → пропускаем (НЕБЕЗОПАСНО: задай env, чтобы закрыть).
  if (!user || !pass) return NextResponse.next();

  const header = req.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    try {
      const decoded = atob(header.slice(6));
      const sep = decoded.indexOf(":");
      const u = decoded.slice(0, sep);
      const p = decoded.slice(sep + 1);
      if (timingSafeEqual(u, user) && timingSafeEqual(p, pass)) {
        return NextResponse.next();
      }
    } catch {
      // битый заголовок → требуем авторизацию ниже
    }
  }

  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Finmt Owner", charset="UTF-8"' },
  });
}
