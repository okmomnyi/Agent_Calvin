import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useAppStore } from "@/core/store";

// Turns list + input. Renders real fields only (turn.text/turn.reply/turn.skill) and
// degrades gracefully on a turn missing skill -- it never fabricates a label for one. Wired
// to whatever `onSend` the parent gives it (WS-first, REST fallback -- see App.tsx), so this
// component itself doesn't know or care which transport actually carried the message.
export function ChatPanel({ onSend, sending }: { onSend: (text: string) => void; sending: boolean }) {
  const turns = useAppStore((s) => s.turns);
  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [turns.length]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || sending) return;
    onSend(text);
    setDraft("");
  };

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface-hi">
      <div ref={listRef} className="min-h-0 flex-1 space-y-3 overflow-x-hidden overflow-y-auto px-4 py-3">
        {turns.length === 0 && (
          <p className="font-mono text-[11px] text-text-mute">No turns yet — say something below.</p>
        )}
        {turns.map((t, i) => (
          <div key={`${t.at}-${i}`} className="space-y-1">
            <div className="flex justify-end">
              {/* break-words: a reply/command can carry a bare URL with no spaces (job
                 links, apply targets) -- without it that single unbroken token forces the
                 bubble past max-w and the whole panel scrolls sideways instead of wrapping. */}
              <p className="max-w-[85%] break-words rounded-[var(--radius-control)] bg-surface-hi px-3 py-1.5 text-sm text-text">
                {t.text}
              </p>
            </div>
            <div className="flex min-w-0 items-baseline gap-2">
              <p className="max-w-[85%] min-w-0 break-words rounded-[var(--radius-control)] bg-surface px-3 py-1.5 text-sm text-text-dim">
                {t.reply}
              </p>
              {t.skill && (
                <span className="shrink-0 font-mono text-[10px] uppercase tracking-widest text-text-mute">
                  {t.skill}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
      <form onSubmit={submit} className="flex gap-2 border-t border-line p-3">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type a command…"
          className="h-9 flex-1 rounded-[var(--radius-control)] border border-line bg-surface px-3 text-sm text-text outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
        />
        <Button type="submit" size="default" disabled={sending || !draft.trim()}>
          Send
        </Button>
      </form>
    </section>
  );
}
