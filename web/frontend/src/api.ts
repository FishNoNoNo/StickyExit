// 管理 token：把地址栏里的 ?token= 透传给后端，保证开启认证后所有接口都能通过。
const TOKEN = (() => {
  const m = location.search.match(/[?&]token=([^&]*)/);
  return m ? decodeURIComponent(m[1]) : "";
})();

function withToken(url: string): string {
  if (!TOKEN) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}

export async function api<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(withToken(url), opts);
  const d: { ok?: boolean; error?: string } = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) {
    throw new Error(d.error || "HTTP " + r.status);
  }
  return d as unknown as T;
}

export function post<T>(url: string): Promise<T> {
  return api<T>(url, { method: "POST" });
}

export function postJSON<T>(url: string, body: unknown): Promise<T> {
  return api<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
