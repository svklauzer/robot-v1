const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function apiGet(path: string) {
  const res = await fetch(`${API_URL}${path}`, {
    cache: "no-store"
  });

  if (!res.ok) {
    throw new Error(`API error: ${res.status}`);
  }

  return res.json();
}

export async function apiPost(path: string, body?: any) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: body ? JSON.stringify(body) : undefined
  });

  if (!res.ok) {
    throw new Error(`API error: ${res.status}`);
  }

  return res.json();
}