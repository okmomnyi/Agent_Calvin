import { useCallback, useEffect, useRef, useState } from "react";
import { AppFrame } from "@/ui/shell/AppFrame";
import { HudStatesDevRoute } from "@/dev/HudStates";
import { detectCapabilities, type Capabilities } from "@/core/capabilities";
import { wireDesktopBridge } from "@/core/desktopBridge";
import { api, ApiError } from "@/core/transport";
import { VoiceSocket, type VoiceMessage } from "@/core/ws";
import { useAppStore } from "@/core/store";
import { LoginScreen } from "@/ui/login/LoginScreen";
import { ChatPanel } from "@/ui/chat/ChatPanel";
import { ApprovalsList } from "@/ui/approvals/ApprovalsList";

const SESSION_POLL_MS = 15000;

function isDevHudRoute(): boolean {
  return import.meta.env.DEV && new URLSearchParams(location.search).get("dev") === "hud";
}

export function App() {
  const [caps, setCaps] = useState<Capabilities | null>(null);

  useEffect(() => {
    detectCapabilities().then(setCaps);
  }, []);

  if (isDevHudRoute()) return <HudStatesDevRoute />;

  // Capability detection is async (it may wait on pywebviewready); render the web-shaped
  // frame immediately rather than a blank screen; it self-corrects once caps resolve.
  return <Live shell={caps?.shell ?? "web"} />;
}

function Live({ shell }: { shell: "web" | "desktop" }) {
  const authState = useAppStore((s) => s.authState);
  const setAuthState = useAppStore((s) => s.setAuthState);
  const setTurns = useAppStore((s) => s.setTurns);
  const addTurn = useAppStore((s) => s.addTurn);
  const setPendingApprovals = useAppStore((s) => s.setPendingApprovals);
  const setHudState = useAppStore((s) => s.setHudState);
  const setSocketStatus = useAppStore((s) => s.setSocketStatus);
  const [sending, setSending] = useState(false);

  const socketRef = useRef<VoiceSocket | null>(null);
  const lastSentRef = useRef<string>("");

  useEffect(() => {
    if (shell === "desktop") wireDesktopBridge();
  }, [shell]);

  const bootstrap = useCallback(async () => {
    try {
      const session = await api.session();
      setTurns(session.turns);
      setPendingApprovals(session.pending_approvals);
      setAuthState("authenticated");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setAuthState("anonymous");
      } else {
        // A non-auth failure (network hiccup, 5xx) still lands on the login screen -- there
        // is no third state to render, and retrying a real login surfaces the real error.
        setAuthState("anonymous");
      }
    }
  }, [setAuthState, setPendingApprovals, setTurns]);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  // Live socket + a slow session poll (picks up turns/approvals from Telegram or another
  // tab) only once actually authenticated.
  useEffect(() => {
    if (authState !== "authenticated") return;

    const socket = new VoiceSocket({
      onStatusChange: setSocketStatus,
      onMessage: (msg: VoiceMessage) => {
        addTurn({
          text: lastSentRef.current,
          reply: msg.text,
          channel: "dashboard",
          at: Date.now() / 1000,
          skill: msg.skill ?? null,
        });
        setHudState(msg.ok ? "idle" : "error");
        setSending(false);
      },
    });
    socketRef.current = socket;
    void socket.connect();

    const poll = setInterval(() => {
      api
        .session()
        .then((s) => {
          setTurns(s.turns);
          setPendingApprovals(s.pending_approvals);
        })
        .catch(() => {
          // a transient poll failure isn't worth surfacing -- the socket carries the
          // real-time state, this is only a periodic resync
        });
    }, SESSION_POLL_MS);

    return () => {
      clearInterval(poll);
      socket.close();
      socketRef.current = null;
    };
  }, [authState, addTurn, setHudState, setPendingApprovals, setSocketStatus, setTurns]);

  const sendText = useCallback(
    (text: string) => {
      lastSentRef.current = text;
      setSending(true);
      setHudState("thinking");
      if (socketRef.current?.send(text)) return;

      // Socket not open -- fall back to the plain REST command endpoint so a send never
      // silently does nothing just because the WS is between reconnect attempts.
      api
        .command(text)
        .then((result) => {
          addTurn({
            text,
            reply: result.text,
            channel: "dashboard",
            at: Date.now() / 1000,
            skill: result.skill ?? null,
          });
          setHudState(result.ok ? "idle" : "error");
        })
        .catch((err) => {
          addTurn({
            text,
            reply: err instanceof ApiError ? err.message : "Couldn't reach AgentOS.",
            channel: "dashboard",
            at: Date.now() / 1000,
            skill: null,
          });
          setHudState("error");
        })
        .finally(() => setSending(false));
    },
    [addTurn, setHudState],
  );

  if (authState === "anonymous") {
    return <LoginScreen onLoggedIn={() => void bootstrap()} />;
  }

  return (
    <AppFrame shell={shell}>
      {authState === "authenticated" && (
        <>
          <ApprovalsList />
          <ChatPanel onSend={sendText} sending={sending} />
        </>
      )}
    </AppFrame>
  );
}
