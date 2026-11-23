export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const csrf = document.cookie.split("; ").find(s => s.startsWith("itura_csrf="))?.split("=")[1];
  const response = await fetch(path, { method, credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
  if (response.status === 204) return undefined as T;
  let data: unknown;
  try {
    data = JSON.parse(await response.text());
  } catch {
    throw new ApiError(response.ok ? 502 : response.status,
      "The application service is unavailable. Please try again later or contact your administrator.");
  }
  const detail = data && typeof data === "object" && "detail" in data ? data.detail : undefined;
  if (!response.ok) throw new ApiError(response.status, typeof detail === "string" ? detail : "Please check the supplied values");
  return data as T;
}
export const requestId = () => crypto.randomUUID();
