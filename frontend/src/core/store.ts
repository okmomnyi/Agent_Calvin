import { create } from "zustand";
import type { HudState, PendingApproval, Turn } from "./types";

export type AuthState = "unknown" | "anonymous" | "authenticated";
export type SocketStatus = "connecting" | "open" | "closed";

// Single user, one app -- Zustand kept lean on purpose (no slices/middleware ceremony a
// multi-tenant app would need). S2: transport.ts populates turns/pendingApprovals/authState
// from the real backend; nothing here is mock data anymore.
interface AppState {
  hudState: HudState;
  micLevel: number;
  connected: boolean;
  authState: AuthState;
  socketStatus: SocketStatus;
  turns: Turn[];
  pendingApprovals: PendingApproval[];
  // Desktop shell only -- true while AssistantCore's OS mic stream is actually open (the
  // "consent boundary", client/assistant_core.py). Always false on the web shell, which has
  // no local mic access at all.
  micOn: boolean;
  setHudState: (s: HudState) => void;
  setMicLevel: (l: number) => void;
  setConnected: (c: boolean) => void;
  setAuthState: (s: AuthState) => void;
  setSocketStatus: (s: SocketStatus) => void;
  setTurns: (t: Turn[]) => void;
  addTurn: (t: Turn) => void;
  setPendingApprovals: (a: PendingApproval[]) => void;
  removePendingApproval: (id: number) => void;
  setMicOn: (on: boolean) => void;
}

export const useAppStore = create<AppState>((set) => ({
  hudState: "idle",
  micLevel: 0,
  connected: false,
  authState: "unknown",
  socketStatus: "closed",
  turns: [],
  pendingApprovals: [],
  micOn: false,
  setHudState: (hudState) => set({ hudState }),
  setMicLevel: (micLevel) => set({ micLevel }),
  setConnected: (connected) => set({ connected }),
  setAuthState: (authState) => set({ authState }),
  setSocketStatus: (socketStatus) => set({ socketStatus, connected: socketStatus === "open" }),
  setTurns: (turns) => set({ turns }),
  addTurn: (t) => set((s) => ({ turns: [...s.turns, t].slice(-40) })),
  setPendingApprovals: (pendingApprovals) => set({ pendingApprovals }),
  removePendingApproval: (id) =>
    set((s) => ({ pendingApprovals: s.pendingApprovals.filter((a) => a.id !== id) })),
  setMicOn: (micOn) => set({ micOn }),
}));
