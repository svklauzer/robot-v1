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

const OWNER_API_TOKEN = process.env.OWNER_API_TOKEN || "";

async function proxy(req: NextRequest, path: string[]) {
  const search = req.nextUrl.search || "";
  const target = `${API_BASE_URL}/${path.join("/")}${search}`;

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
    const res = await fetch(target, init);
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
