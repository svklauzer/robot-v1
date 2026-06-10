"""HTX affiliate verification — проверяет, что HTX UID зарегистрирован по нашей
партнёрской ссылке, через signed REST API affiliate-аккаунта.

КАРКАС: подпись запроса (HmacSHA256, SignatureVersion=2) реализована полностью.
Нужно ПОДСТАВИТЬ из affiliate-доков HTX:
  - settings.HTX_AFFILIATE_INVITEES_PATH (точный путь эндпоинта)
  - разбор ответа в _extract_uids() (имена полей зависят от конкретного API)

Фича выключена по умолчанию (HTX_AFFILIATE_VERIFY_ENABLED=false) — тогда
бот работает как раньше (self-claim без проверки).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from core.config import settings
from core.logging import get_logger, log_event

logger = get_logger(__name__)


class HTXAffiliateService:
    def is_configured(self) -> bool:
        return bool(
            settings.HTX_AFFILIATE_VERIFY_ENABLED
            and settings.HTX_AFFILIATE_API_KEY
            and settings.HTX_AFFILIATE_API_SECRET
        )

    async def verify_referral(self, htx_uid: str) -> tuple[bool, str]:
        """Возвращает (verified, reason)."""
        uid = str(htx_uid or "").strip()
        if not uid.isdigit():
            return False, "invalid_uid"
        if not self.is_configured():
            # Верификация не настроена — caller решит, что делать.
            return False, "verification_unconfigured"

        try:
            uids = await self._fetch_referred_uids()
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "htx_affiliate_api_error", error=str(exc))
            return False, f"htx_api_error:{type(exc).__name__}"

        if uid in uids:
            return True, "verified"
        return False, "uid_not_in_referrals"

    # ── Internals ────────────────────────────────────────────────────────────

    async def _fetch_referred_uids(self) -> set[str]:
        """Тянет список приглашённых UID. ПОДСТАВЬ путь и разбор под реальный API."""
        path = settings.HTX_AFFILIATE_INVITEES_PATH
        data = await self._signed_get(path, params={})
        return self._extract_uids(data)

    @staticmethod
    def _extract_uids(data: dict) -> set[str]:
        """Best-effort разбор: ищем UID в типичных местах ответа.
        TODO: заменить на точный разбор под формат affiliate-API HTX."""
        uids: set[str] = set()
        rows = []
        if isinstance(data, dict):
            for key in ("data", "list", "result", "rows"):
                value = data.get(key)
                if isinstance(value, list):
                    rows = value
                    break
        elif isinstance(data, list):
            rows = data
        for row in rows:
            if isinstance(row, dict):
                for k in ("uid", "userId", "user_id", "invitedUid", "inviteeUid"):
                    if row.get(k) is not None:
                        uids.add(str(row[k]))
            elif row is not None:
                uids.add(str(row))
        return uids

    async def _signed_get(self, path: str, params: dict) -> dict:
        """GET с подписью HTX (Huobi) v2: HmacSHA256 + base64."""
        host = settings.HTX_AFFILIATE_API_HOST
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        signed_params = {
            "AccessKeyId": settings.HTX_AFFILIATE_API_KEY,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": ts,
            **{k: str(v) for k, v in params.items()},
        }
        # Канонический payload: METHOD\nhost\npath\nsorted(urlencoded params)
        sorted_qs = urlencode(sorted(signed_params.items()))
        payload = f"GET\n{host}\n{path}\n{sorted_qs}"
        signature = base64.b64encode(
            hmac.new(
                settings.HTX_AFFILIATE_API_SECRET.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        signed_params["Signature"] = signature

        url = f"https://{host}{path}"
        timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=signed_params)
        if resp.status_code >= 400:
            raise RuntimeError(f"htx_affiliate_http_{resp.status_code}:{resp.text[:200]}")
        return resp.json() if resp.content else {}
