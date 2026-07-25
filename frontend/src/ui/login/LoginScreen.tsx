import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/core/transport";
import { authenticateWithPasskey, PasskeyUnavailableError } from "@/core/webauthnBrowser";

// Three ways in, all landing on the same session cookie: passkey (primary factor),
// break-glass password, and single-use recovery codes (the password's own break-glass).
// Mirrors core/auth.py's own three entry points exactly -- see kernel/app.py's
// /api/auth/{password,recovery,webauthn/login/*} routes.
export function LoginScreen({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [mode, setMode] = useState<"password" | "recovery">("password");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function withBusy(fn: () => Promise<void>): Promise<void> {
    setError(null);
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }

  const submitPassword = (e: React.FormEvent) => {
    e.preventDefault();
    void withBusy(async () => {
      await api.loginPassword(password);
      onLoggedIn();
    });
  };

  const submitRecovery = (e: React.FormEvent) => {
    e.preventDefault();
    void withBusy(async () => {
      await api.loginRecovery(recoveryCode);
      onLoggedIn();
    });
  };

  const usePasskey = () => {
    void withBusy(async () => {
      const options = await api.webauthnLoginOptions();
      const credential = await authenticateWithPasskey(options);
      await api.webauthnLoginVerify(credential);
      onLoggedIn();
    });
  };

  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 px-6">
      <div className="text-center">
        <p className="font-mono text-xs font-semibold tracking-[0.2em] text-text">AGENTOS</p>
        <p className="mt-1 font-mono text-[10px] uppercase tracking-widest text-text-dim">
          Sign in to continue
        </p>
      </div>

      <Button onClick={usePasskey} disabled={busy} className="w-64">
        Use a passkey
      </Button>

      <div className="flex w-64 items-center gap-3 text-text-mute">
        <div className="h-px flex-1 bg-line" />
        <span className="font-mono text-[10px] uppercase tracking-widest">or</span>
        <div className="h-px flex-1 bg-line" />
      </div>

      {mode === "password" ? (
        <form onSubmit={submitPassword} className="flex w-64 flex-col gap-2">
          <input
            type="password"
            autoComplete="current-password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-9 rounded-[var(--radius-control)] border border-line bg-surface px-3 text-sm text-text outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
          />
          <Button type="submit" variant="outline" disabled={busy || !password}>
            Sign in with password
          </Button>
          <button
            type="button"
            onClick={() => setMode("recovery")}
            className="font-mono text-[10px] uppercase tracking-widest text-text-mute hover:text-text-dim"
          >
            Use a recovery code instead
          </button>
        </form>
      ) : (
        <form onSubmit={submitRecovery} className="flex w-64 flex-col gap-2">
          <input
            type="text"
            placeholder="Recovery code"
            value={recoveryCode}
            onChange={(e) => setRecoveryCode(e.target.value)}
            className="h-9 rounded-[var(--radius-control)] border border-line bg-surface px-3 text-sm text-text outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
          />
          <Button type="submit" variant="outline" disabled={busy || !recoveryCode}>
            Sign in with recovery code
          </Button>
          <button
            type="button"
            onClick={() => setMode("password")}
            className="font-mono text-[10px] uppercase tracking-widest text-text-mute hover:text-text-dim"
          >
            Back to password
          </button>
        </form>
      )}

      {error && <p className="w-64 text-center text-xs text-bad">{error}</p>}
    </div>
  );
}

function describeError(err: unknown): string {
  if (err instanceof PasskeyUnavailableError) return err.message;
  if (err instanceof ApiError) {
    if (err.status === 429) {
      return err.retryAfter
        ? `Too many attempts — try again in ${err.retryAfter}s.`
        : "Too many attempts — try again shortly.";
    }
    return err.message || "Sign-in failed.";
  }
  if (err instanceof Error && err.name === "NotAllowedError") {
    return "Passkey ceremony was cancelled or timed out.";
  }
  return "Sign-in failed.";
}
