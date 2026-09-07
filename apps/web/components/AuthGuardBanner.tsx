"use client";

import { useEffect, useState } from "react";

// (#dashboard-auth-visible-2026-09-07) Красная полоса, когда заслон дашборда не
// настроен.
//
// `middleware.ts` при незаданных BASIC_AUTH_USER/PASS пропускает всех, а прокси
// подставляет OWNER_API_TOKEN на сервере — то есть публичный URL раздаёт права
// владельца. Полоса стоит рядом с баннером режима: там же, где написано «LIVE —
// настоящие деньги», должно быть видно, что дверь открыта.
export default function AuthGuardBanner() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    fetch("/api/auth-state", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        if (alive) setOpen(d?.basic_auth_configured === false);
      })
      .catch(() => {
        /* не смогли спросить — молчим, ложная тревога хуже молчания */
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!open) return null;

  return (
    <div className="rounded-2xl border border-red-500/70 bg-red-950/70 px-4 py-2 text-sm font-semibold text-red-100 shadow-lg">
      <span className="mr-2 text-base font-extrabold">ДАШБОРД ОТКРЫТ</span>
      BASIC_AUTH_USER и BASIC_AUTH_PASS не заданы — заслон пропускает всех, а прокси
      подставляет owner-токен. Любой, кто знает адрес, может остановить робота, закрыть
      позиции и увидеть подписчиков. Задать обе переменные на сервисе robot-web.
    </div>
  );
}
