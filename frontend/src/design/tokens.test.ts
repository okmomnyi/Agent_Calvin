/// <reference types="node" />
// Tailwind v4's @theme block in index.css is CSS-first and can't import this .ts file, so
// its values are a hand-mirrored copy of tokens.color. This test is the tripwire for that
// duplication drifting silently -- change one without the other and this fails loudly
// instead of a component quietly rendering the wrong shade of cyan.
//
// Reads index.css via node:fs, not Vite's `?raw` suffix: `?raw` returns an EMPTY string
// under vitest once @tailwindcss/vite is in the plugin chain (confirmed by hand -- the
// plugin's own CSS transform intercepts the file before the raw-loader sees it, in test
// mode specifically; `vite build`/`vite dev` are unaffected). fs.readFileSync sidesteps
// Vite's asset pipeline entirely. The `/// <reference types="node" />` above scopes Node's
// types to this one file rather than pulling them into the whole browser-targeted app build.
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { tokens } from "./tokens";

const cssPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../index.css");
const css = readFileSync(cssPath, "utf-8");

function cssVar(name: string): string | null {
  const match = css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`));
  return match ? match[1].toLowerCase() : null;
}

describe("design tokens stay in sync with index.css's @theme block", () => {
  const cssVarByColorKey: Record<keyof typeof tokens.color, string> = {
    bg: "color-bg",
    surface: "color-surface",
    surfaceHi: "color-surface-hi",
    line: "color-line",
    primary: "color-primary",
    primaryDim: "color-primary-dim",
    attention: "color-attention",
    good: "color-good",
    mid: "color-mid",
    bad: "color-bad",
    text: "color-text",
    textDim: "color-text-dim",
    textMute: "color-text-mute",
  };

  for (const [key, cssName] of Object.entries(cssVarByColorKey)) {
    it(`${key} matches --${cssName}`, () => {
      const expected = tokens.color[key as keyof typeof tokens.color].toLowerCase();
      expect(cssVar(cssName)).toBe(expected);
    });
  }
});
