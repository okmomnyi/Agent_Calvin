import { create } from "zustand";
import type { HudState, JobListing, PendingApproval, Turn } from "./types";
import type { StageDirective, StageWidget } from "./stageTypes";

export type AuthState = "unknown" | "anonymous" | "authenticated";
export type SocketStatus = "connecting" | "open" | "closed";

// Voice "pin that" targets whichever widget is currently most prominent -- a chart (the
// specific thing being looked at) outranks a supporting fact, which outranks the article
// list. Ticker/map are never pin targets: a ticker is a strip of many things, not "the"
// widget, and map is optional/lower-priority per the build order.
const PIN_PRIORITY: StageWidget["type"][] = ["chart", "fact", "articles"];

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
  jobs: JobListing[];
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
  setJobs: (j: JobListing[]) => void;
  // Phase 38: the self-driving stage. `stageDirective` is whatever the most recent WS
  // message carried (or null -- the calm Phase 36 HUD); `pinnedWidget` survives across ANY
  // number of later setStageDirective calls until explicitly unpinned (StageCanvas merges
  // it back in on top of whatever a new directive brings for that widget's slot).
  stageDirective: StageDirective | null;
  pinnedWidget: StageWidget | null;
  setStageDirective: (d: StageDirective | null) => void;
  pinWidget: (w: StageWidget) => void;
  pinCurrentWidget: () => void;
  unpinWidget: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  hudState: "idle",
  micLevel: 0,
  connected: false,
  authState: "unknown",
  socketStatus: "closed",
  turns: [],
  pendingApprovals: [],
  jobs: [],
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
  setJobs: (jobs) => set({ jobs }),
  stageDirective: null,
  pinnedWidget: null,
  setStageDirective: (stageDirective) => set({ stageDirective }),
  pinWidget: (pinnedWidget) => set({ pinnedWidget }),
  pinCurrentWidget: () =>
    set((s) => {
      const widgets = s.stageDirective?.widgets ?? [];
      for (const type of PIN_PRIORITY) {
        const found = widgets.find((w) => w.type === type);
        if (found) return { pinnedWidget: found };
      }
      return {}; // nothing pinnable on the stage right now -- leave any existing pin alone
    }),
  unpinWidget: () => set({ pinnedWidget: null }),
}));
