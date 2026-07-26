import { useState } from "react";
import type { ArticleItem, ArticlesWidgetData } from "@/core/stageTypes";

// Hard content-safety line (Phase 38): `item.image` is a feed thumbnail URL or nothing --
// never populated by an image search anywhere in this codebase (see
// skills/world_news.py's `_extract_image`). Missing OR broken (dead URL, load error) both
// fall through to the SAME typed fallback card below; there is no third path that
// substitutes a searched or generic stock photo.
function Thumb({ item }: { item: ArticleItem }) {
  const [failed, setFailed] = useState(false);
  if (item.image && !failed) {
    return (
      <img
        src={item.image}
        alt=""
        loading="lazy"
        onError={() => setFailed(true)}
        className="aspect-video w-full object-cover"
      />
    );
  }
  return (
    <div
      className="flex aspect-video w-full items-center justify-center bg-surface"
      data-testid="article-fallback-thumb"
      aria-hidden="true"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.4} className="h-8 w-8 text-text-mute">
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <circle cx="9" cy="10" r="2" />
        <path d="M3 17l5-4 4 3 3-2 6 5" />
      </svg>
    </div>
  );
}

function timeAgo(publishedEpochSeconds: number, nowMs = Date.now()): string {
  const minutes = Math.max(0, Math.round((nowMs - publishedEpochSeconds * 1000) / 60000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

export function ArticlesWidget({ data }: { data: ArticlesWidgetData }) {
  return (
    <div>
      <h4 className="mb-2 font-mono text-[11px] uppercase tracking-widest text-text-dim">{data.topic}</h4>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {data.items.map((item) => (
          <a
            key={item.url}
            href={item.url}
            target="_blank"
            rel="noreferrer noopener"
            className="block overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface-hi transition-transform hover:-translate-y-0.5 hover:border-primary-dim"
          >
            <Thumb item={item} />
            <div className="p-3">
              <p className="text-[13.5px] leading-snug text-text">{item.title}</p>
              <p className="mt-2 font-mono text-[10px] tracking-wide text-text-mute">
                {item.source} · {timeAgo(item.published)}
              </p>
            </div>
          </a>
        ))}
      </div>
    </div>
  );
}
