export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const csrf = document.cookie.split("; ").find(s => s.startsWith("itura_csrf="))?.split("=")[1];
  const response = await fetch(path, { method, credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, typeof data.detail === "string" ? data.detail : "Please check the supplied values");
  return data as T;
}
export const requestId = () => crypto.randomUUID();
