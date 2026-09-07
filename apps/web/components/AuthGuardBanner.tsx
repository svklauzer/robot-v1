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
    <div className="rounded-2xl border border-amber-500/70 bg-amber-950/60 px-4 py-2 text-sm font-semibold text-amber-100 shadow-lg">
      <span className="mr-2 text-base font-extrabold">ЗАСЛОН НЕ НАСТРОЕН</span>
      BASIC_AUTH_USER и BASIC_AUTH_PASS не заданы. Здесь это пропускается, потому что
      сборка не production; в production такой же конфиг вернёт 503 и внутрь не пустит.
      Перед деплоем задать обе переменные на сервисе robot-web.
    </div>
  );
}
