const BASE = import.meta.env.VITE_API_URL || "";

async function req(path, opts) {
  const res = await fetch(BASE + path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = body?.error || body?.detail?.error;
    throw new Error(err?.message || `Request failed (HTTP ${res.status})`);
  }
  return body;
}

export const api = {
  stats: () => req("/api/stats"),
  handoffs: () => req("/api/handoffs?status=open"),
  resolve: (id) => req(`/api/handoffs/${encodeURIComponent(id)}/resolve`, { method: "POST" }),
  conversations: () => req("/api/conversations"),
  conversation: (id) => req(`/api/conversations/${encodeURIComponent(id)}`),
  scripts: () => req("/api/scripts"),
  run: (body) => req("/agent/run", { method: "POST", body: JSON.stringify(body) }),
};
