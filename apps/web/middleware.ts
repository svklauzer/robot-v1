import { NextRequest, NextResponse } from "next/server";

// ── Заслон на ВЕСЬ owner-дашборд (страницы + /api/proxy) ──────────────────────
// Проблема: прокси /api/proxy/* сам подставляет OWNER_API_TOKEN на сервере, т.е.
// ЛЮБОЙ посетитель публичного URL получал права владельца (Start/Stop, kill-switch,
// закрытие сигналов, платежи, подписчики, Telegram). Этот middleware требует
// HTTP Basic Auth ПЕРЕД тем, как запрос дойдёт до страниц и до прокси.
//
// (#fail-closed-2026-09-07) Раньше при незаданных BASIC_AUTH_USER/PASS заслон
// пропускал ВСЕХ — чтобы владелец не заперся до настройки. Компромисс держался
// на том, что про него помнят; проверка 07.09 показала, что переменные заданы,
// и его больше нечем оправдывать: цена ошибки — публичный URL с правами
// владельца.
//
// Теперь в production без пары — 503 и никакого доступа. Ответ намеренно НЕ 401:
// пароля, который подойдёт, не существует, и предлагать его ввести значило бы
// отправить владельца подбирать несуществующее вместо того, чтобы задать env.
//
// В разработке (NODE_ENV !== production) пропускаем: там заслон не нужен, а
// поднять локальный фронт без двух переменных должно оставаться возможным.

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

  if (!user || !pass) {
    if (process.env.NODE_ENV !== "production") return NextResponse.next();
    return new NextResponse(
      "Owner dashboard is not configured: set BASIC_AUTH_USER and BASIC_AUTH_PASS on robot-web.",
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }

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
