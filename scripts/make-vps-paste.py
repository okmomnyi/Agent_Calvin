"""Generate a paste-ready block for a VPS **web console** (no ssh, so no scp).

Three files can never come from `git pull` — .env, secrets/token.json and
config/timetable.yaml are all gitignored, precisely because they're secrets or say where
Calvin physically is. On a normal box you'd scp them. From a browser console you can only
paste, so this renders them as shell here-docs you paste in one go.

Design notes, all learned the hard way:
  * Output goes to a FILE, never stdout — it contains a live NIM key and bot token, and a
    terminal transcript is the last place those should land.
  * The here-doc delimiter is QUOTED ('AGENTOS_EOF'), so the shell treats every line as
    literal. Unquoted, a `$` or a backtick in a token would be expanded into nothing.
  * token.json is base64'd and folded to 76 columns. It's one long line of quoted JSON, and
    browser consoles mangle both long lines and quotes.
  * Every file gets a sha256 check appended, because a silent partial paste is the normal
    failure here, and "it pasted fine" is not evidence.

    python scripts/make-vps-paste.py
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "vps-paste.txt"          # gitignored

FILES = [
    (".env", "heredoc", "secrets + identity; the containers read this"),
    ("config/timetable.yaml", "heredoc", "your real class schedule"),
    ("secrets/token.json", "base64", "Gmail refresh token (one long line of JSON)"),
]


def sha(p: Path, mode: str) -> str:
    """Checksum of what will exist ON THE VPS, not of the local file.

    A here-doc emits LF, but .env here is CRLF — so hashing the local bytes reports a
    mismatch for a paste that arrived perfectly. A check that cries wolf on a good paste is
    worse than no check: you'd re-paste, see it "fail" again, and stop trusting it.
    base64 round-trips the bytes exactly, so there the local file IS what lands.
    """
    if mode == "base64":
        return hashlib.sha256(p.read_bytes()).hexdigest()
    written = p.read_text(encoding="utf-8").rstrip("\n") + "\n"
    return hashlib.sha256(written.encode("utf-8")).hexdigest()


def main() -> int:
    missing = [f for f, _, _ in FILES if not (ROOT / f).exists()]
    if missing:
        print(f"Cannot generate — missing locally: {', '.join(missing)}")
        return 1

    out: list[str] = [
        "# ── PASTE EVERYTHING BELOW INTO THE VPS WEB CONSOLE ──────────────────",
        "# Run it from INSIDE the cloned repo — wherever you put it. It works off the",
        "# current directory, so the folder can be called anything.",
        "# It creates the 3 files git can't carry, then verifies each checksum.",
        "",
        # Guarded by `if`, NOT `cd ... || exit`: this is pasted into an interactive shell,
        # and a bare `exit` would close the web console session on the one path where you
        # most need to read the error.
        "if [ ! -f docker-compose.yml ] || [ ! -d skills ]; then",
        '  echo "STOP: not in the repo. cd into it first (e.g. cd ~/Agent_Calvin), then paste again."',
        "else",
        "mkdir -p secrets config",
        "",
    ]

    for rel, mode, why in FILES:
        p = ROOT / rel
        out.append(f"# ── {rel}  ({why})")
        if mode == "heredoc":
            # Quoted delimiter: nothing inside is expanded by the shell.
            out.append(f"cat > {rel} <<'AGENTOS_EOF'")
            out.append(p.read_text(encoding="utf-8").rstrip("\n"))
            out.append("AGENTOS_EOF")
        else:
            b64 = base64.b64encode(p.read_bytes()).decode()
            folded = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
            out.append(f"base64 -d > {rel} <<'AGENTOS_EOF'")
            out.append(folded)
            out.append("AGENTOS_EOF")
        out.append("")

    out += ["chmod 600 .env secrets/token.json", "",
            "# ── verify the paste survived (a mangled paste is the normal failure) ──",
            'echo "--- files landed in: $(pwd)"',
            "echo '--- expected vs actual ---'"]
    for rel, mode, _ in FILES:
        out.append(f"echo '{sha(ROOT / rel, mode)}  {rel}   <- expected'")
        out.append(f"sha256sum {rel} | sed 's|$|   <- actual|'")
    out += ["fi",
            "",
            "# Identical pairs = every byte arrived. Any mismatch = re-paste that one file.",
            "# ── END ──────────────────────────────────────────────────────────────"]

    # newline="\n" is load-bearing: on Windows, write_text() emits CRLF, and bash on the VPS
    # then sees `AGENTOS_EOF\r`, which never matches the here-doc delimiter — every file
    # collapses into one unterminated here-doc and you get `$'\r': command not found`.
    with OUT.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"Wrote {OUT}")
    print(f"  {OUT.stat().st_size} bytes, {len(out)} lines")
    print("\n  Open it, copy ALL of it, paste into the web console.")
    print("  It is gitignored and contains live secrets — delete it when you're done:")
    print(f"    rm '{OUT}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
