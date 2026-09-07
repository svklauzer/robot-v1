import { NextResponse } from "next/server";

// (#dashboard-auth-visible-2026-09-07) Состояние заслона дашборда — булев ответ,
// без имён и секретов.
//
// `middleware.ts` требует Basic Auth перед страницами И перед /api/proxy, но
// при незаданных BASIC_AUTH_USER/PASS пропускает всех — чтобы не залочить
// владельца до настройки. Компромисс разумный, а вот его следствие нигде не
// видно: прокси подставляет OWNER_API_TOKEN на сервере, то есть при незаданной
// паре ЛЮБОЙ посетитель публичного URL получает права владельца — Start/Stop,
// kill-switch, закрытие позиций, платежи, подписчики.
//
// Предохранитель, о состоянии которого нельзя спросить, — это предохранитель,
// про который узнают постфактум. Здесь про него можно спросить.

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const configured = Boolean(process.env.BASIC_AUTH_USER && process.env.BASIC_AUTH_PASS);
  return NextResponse.json({ basic_auth_configured: configured });
}
