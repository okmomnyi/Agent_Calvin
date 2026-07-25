// The single source of truth for the HUD's look. A component never hardcodes a hex --
// everything here, wired into the Tailwind theme (src/index.css's @theme block) so classes
// like bg-surface / text-primary resolve to these values. Change the aesthetic in exactly
// one place.
export const tokens = {
  color: {
    bg: "#05070A", // page — near black
    surface: "#0B0F14", // panel
    surfaceHi: "#111821", // raised card
    line: "#1C2A34", // hairline border
    primary: "#38E0D0", // cyan — the HUD accent
    primaryDim: "#1B8F86",
    attention: "#F0A93B", // amber — approvals, pending
    good: "#5DCAA5", // score band: strong
    mid: "#EFB54A", // score band: worth a look
    bad: "#E2564A", // score band: filtered / too senior
    text: "#E6F0F2",
    textDim: "#7C93A0",
    textMute: "#4A5A66",
  },
  motion: { fast: 140, base: 280, slow: 620, idleDriftMs: 24000 },
  radius: { card: 14, control: 10, pill: 999 },
  glow: { idle: "rgba(56,224,208,0.18)", alert: "rgba(240,169,59,0.22)" },
} as const;

export type Tokens = typeof tokens;
