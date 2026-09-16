import { NextRequest, NextResponse } from "next/server";

// Серверный прокси к backend API.
// Owner-токен живёт ТОЛЬКО на сервере (process.env.OWNER_API_TOKEN, без
// префикса NEXT_PUBLIC), поэтому в клиентский бандл он не попадает.
// Браузер ходит на same-origin /api/proxy/* → нет CORS и нет утечки токена.

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const API_BASE_URL = (
  process.env.API_BASE_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000"
).replace(/\/+$/, "");

// (#private-network-2026-09-16) Внутренний адрес robot-api в частной сети
// Render (render.yaml: fromService → hostport). 16.09 исчерпан лимит трафика
// 5 ГБ: прокси ходил в API по публичному адресу, и каждый ответ оплачивался
// дважды — robot-api → интернет → robot-web, потом robot-web → браузер. Трафик
// частной сети не тарифицируется. Пусто — прежнее поведение.
const INTERNAL_HOSTPORT = (process.env.API_INTERNAL_HOSTPORT || "").trim();
const API_INTERNAL_URL = INTERNAL_HOSTPORT
  ? (/^https?:\/\//.test(INTERNAL_HOSTPORT) ? INTERNAL_HOSTPORT : `http://${INTERNAL_HOSTPORT}`).replace(/\/+$/, "")
  : "";

// Ошибки, при которых запрос гарантированно НЕ дошёл до API: адрес не
// разрешился или соединение не принято. Только на них повторяется
// неидемпотентный запрос (POST kill switch, закрытие позиции) — иначе при
// обрыве посреди ответа команда исполнилась бы дважды.
const NOT_SENT_CODES = new Set(["ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED"]);
// После сбоя частной сети — публичный адрес на это время, без попытки на каждый запрос.
const INTERNAL_RETRY_AFTER_MS = 60_000;
let internalDownUntil = 0;

const OWNER_API_TOKEN = process.env.OWNER_API_TOKEN || "";

async function upstream(pathAndSearch: string, init: RequestInit): Promise<Response> {
  if (API_INTERNAL_URL && Date.now() >= internalDownUntil) {
    try {
      return await fetch(`${API_INTERNAL_URL}${pathAndSearch}`, init);
    } catch (err: any) {
      const code = err?.cause?.code || err?.code;
      const idempotent = init.method === "GET" || init.method === "HEAD";
      if (!idempotent && !NOT_SENT_CODES.has(code)) throw err;
      internalDownUntil = Date.now() + INTERNAL_RETRY_AFTER_MS;
      console.warn(`[proxy] private network failed (${code || err?.message}), using public URL`);
    }
  }
  return fetch(`${API_BASE_URL}${pathAndSearch}`, init);
}

async function proxy(req: NextRequest, path: string[]) {
  const search = req.nextUrl.search || "";
  const pathAndSearch = `/${path.join("/")}${search}`;

  const headers: Record<string, string> = {};
  if (OWNER_API_TOKEN) headers["X-Owner-Token"] = OWNER_API_TOKEN;
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

  const init: RequestInit = { method: req.method, headers, cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    const body = await req.text();
    if (body) init.body = body;
  }

  try {
    const res = await upstream(pathAndSearch, init);
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: {
        "content-type": res.headers.get("content-type") || "application/json",
      },
    });
  } catch (err: any) {
    return NextResponse.json(
      { error: "proxy_failed", detail: String(err?.message || err) },
      { status: 502 }
    );
  }
}

export async function GET(req: NextRequest, ctx: { params: { path: string[] } }) {
  return proxy(req, ctx.params.path);
}

export async function POST(req: NextRequest, ctx: { params: { path: string[] } }) {
  return proxy(req, ctx.params.path);
}

export async function PUT(req: NextRequest, ctx: { params: { path: string[] } }) {
  return proxy(req, ctx.params.path);
}

export async function DELETE(req: NextRequest, ctx: { params: { path: string[] } }) {
  return proxy(req, ctx.params.path);
}
