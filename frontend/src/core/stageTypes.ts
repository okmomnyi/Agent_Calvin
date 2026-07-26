// Phase 38 stage directive protocol. Mirrors core/stage.py's StageDirective/Widget shapes
// EXACTLY -- a change to one side is a change to both, same discipline this file's sibling
// types.ts already holds with core/session.py's Turn. Never constructed client-side from
// scratch: every StageDirective on the wire came from core/presenter.py asking an existing
// skill's own display() for real data.
export type StageAccent = "primary" | "alert";
export type StageTransition = "bloom" | "swap" | "settle";

export interface ChartPoint {
  t: number;
  v: number;
}

export interface ChartWidgetData {
  type: "chart";
  asset: string;
  klass: string; // crypto | equity | fx | commodity | nse
  range: string; // 1d | 1w | 1m
  series: ChartPoint[];
  as_of: number;
  delayed_label: string;
  source: string;
}

export interface ArticleItem {
  title: string;
  source: string;
  url: string;
  published: number;
  image?: string | null;
}

export interface ArticlesWidgetData {
  type: "articles";
  topic: string;
  items: ArticleItem[];
}

export interface TickerItemData {
  symbol: string;
  price: number;
  change_pct: number;
  as_of: number;
}

export interface TickerWidgetData {
  type: "ticker";
  items: TickerItemData[];
}

export interface FactWidgetData {
  type: "fact";
  title: string;
  stat: string;
  sub: string;
  sources: string[];
}

export interface MapMarker {
  lat: number;
  lng: number;
  label: string;
}

export interface MapWidgetData {
  type: "map";
  region: string;
  markers?: MapMarker[];
}

export type StageWidget =
  | ChartWidgetData
  | ArticlesWidgetData
  | TickerWidgetData
  | FactWidgetData
  | MapWidgetData;

export interface StageDirective {
  focus: string | null;
  headline?: string | null;
  accent?: StageAccent;
  transition: StageTransition;
  ttl_s?: number | null;
  widgets: StageWidget[];
}

/** A stable identity for a widget across directives -- used as the AnimatePresence/React
 * key so the Director only re-blooms a widget that genuinely changed subject, not one
 * that's merely been re-sent with the same content. */
export function widgetKey(widget: StageWidget): string {
  switch (widget.type) {
    case "chart":
      return `chart:${widget.asset}`;
    case "articles":
      return `articles:${widget.topic}`;
    case "fact":
      return `fact:${widget.title}`;
    case "map":
      return `map:${widget.region}`;
    case "ticker":
      return "ticker";
  }
}
