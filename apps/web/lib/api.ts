const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const OWNER_API_TOKEN = process.env.NEXT_PUBLIC_OWNER_API_TOKEN || "";

function ownerHeaders(extra?: Record<string, string>) {
  return {
    ...(OWNER_API_TOKEN ? { "X-Owner-Token": OWNER_API_TOKEN } : {}),
    ...(extra || {}),
  };
}

export async function apiGet(path: string) {
  const res = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    headers: ownerHeaders(),
  });

  if (!res.ok) {
    throw new Error(`API error: ${res.status}`);
  }

  return res.json();
}

export async function apiPost(path: string, body?: any) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: ownerHeaders({
      "Content-Type": "application/json",
    }),
    body: body ? JSON.stringify(body) : undefined
  });

  if (!res.ok) {
    throw new Error(`API error: ${res.status}`);
  }

  return res.json();
}
