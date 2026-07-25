import { create } from "zustand";
import type { HudState } from "./types";

// Single user, one app -- Zustand kept lean on purpose (no slices/middleware ceremony a
// multi-tenant app would need). Transport (Slice 0's cookie+ticket flow) and real panel
// data land in S2; this is deliberately just what S1's static HUD needs to run.
interface AppState {
  hudState: HudState;
  micLevel: number;
  connected: boolean;
  setHudState: (s: HudState) => void;
  setMicLevel: (l: number) => void;
  setConnected: (c: boolean) => void;
}

export const useAppStore = create<AppState>((set) => ({
  hudState: "idle",
  micLevel: 0,
  connected: false,
  setHudState: (hudState) => set({ hudState }),
  setMicLevel: (micLevel) => set({ micLevel }),
  setConnected: (connected) => set({ connected }),
}));
