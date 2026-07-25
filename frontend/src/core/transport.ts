import type { PendingApproval, Turn } from "./types";

// Every call is relative + same-origin: the web dashboard is served from the same kernel
// it talks to (https://<domain>/dashboard/ -> https://<domain>/api/...), so the session
// cookie (SameSite=Strict) rides along automatically. This is deliberately NOT shell-aware
// -- see client/hud_window.py's own comment for why the desktop shell now navigates to the
// real deployed origin instead of serving frontend/ from a local loopback port (a
// SameSite=Strict cookie set for the real domain is never sent back to a 127.0.0.1 origin,
// which would silently break login for the desktop shell only).
export class ApiError extends Error {
  status: number;
  retryAfter?: number;

  constructor(status: number, message: string, retryAfter?: number) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      // no JSON body -- keep statusText
    }
    const retryAfter = res.headers.get("Retry-After");
    throw new ApiError(res.status, detail, retryAfter ? Number(retryAfter) : undefined);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export interface SessionState {
  session_id: string;
  active_skill: string | null;
  turns: Turn[];
  last_channel: string | null;
  pending_approvals: PendingApproval[];
  updated_at: number | null;
  live_skill_session: string | null;
  current_plan: { id: number; goal: string; status: string } | null;
}

export interface CommandResult {
  ok: boolean;
  text: string;
  intent: string;
  skill: string;
  via: string;
  data: unknown;
}

export const api = {
  session: () => get<SessionState>("/api/session"),

  command: (text: string, opts: { channel?: string; useLlm?: boolean; spoken?: boolean } = {}) =>
    post<CommandResult>("/api/command", {
      text,
      channel: opts.channel ?? "dashboard",
      use_llm: opts.useLlm ?? true,
      spoken: opts.spoken ?? false,
    }),

  loginPassword: (password: string) => post<{ ok: boolean }>("/api/auth/password", { password }),

  loginRecovery: (code: string) =>
    post<{ ok: boolean; notice: string; remaining_recovery_codes: number }>(
      "/api/auth/recovery",
      { code },
    ),

  logout: () => post<{ ok: boolean }>("/api/auth/logout"),

  webauthnLoginOptions: () => post<Record<string, unknown>>("/api/auth/webauthn/login/options"),

  webauthnLoginVerify: (credential: Record<string, unknown>) =>
    post<{ ok: boolean }>("/api/auth/webauthn/login/verify", { credential }),

  wsTicket: () => post<{ ticket: string }>("/api/auth/ws-ticket"),

  resolveApproval: (id: number, approve: boolean, stepUpCredential?: Record<string, unknown>) =>
    post<{ ok: boolean; status: string }>(`/api/approvals/${id}/resolve`, {
      approve,
      step_up_credential: stepUpCredential ?? null,
    }),
};
