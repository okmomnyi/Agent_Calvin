import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ArticlesWidget } from "./ArticlesWidget";
import type { ArticlesWidgetData } from "@/core/stageTypes";

afterEach(cleanup);

function widget(items: ArticlesWidgetData["items"]): ArticlesWidgetData {
  return { type: "articles", topic: "World", items };
}

describe("ArticlesWidget", () => {
  it("renders a real feed thumbnail when the item has one", () => {
    const { container } = render(
      <ArticlesWidget
        data={widget([{ title: "A story", source: "BBC", url: "https://x.test/1", published: 0, image: "https://img.test/a.jpg" }])}
      />,
    );
    const img = container.querySelector("img") as HTMLImageElement;
    expect(img).not.toBeNull();
    expect(img.src).toBe("https://img.test/a.jpg");
    expect(screen.queryByTestId("article-fallback-thumb")).not.toBeInTheDocument();
  });

  it("renders the typed fallback card when the item has no image", () => {
    render(
      <ArticlesWidget
        data={widget([{ title: "No image", source: "AP", url: "https://x.test/2", published: 0, image: null }])}
      />,
    );
    expect(screen.getByTestId("article-fallback-thumb")).toBeInTheDocument();
  });

  it("falls back to the typed card when the feed image URL is dead (fails to load)", () => {
    const { container } = render(
      <ArticlesWidget
        data={widget([{ title: "Dead link", source: "Reuters", url: "https://x.test/3", published: 0, image: "https://img.test/dead.jpg" }])}
      />,
    );
    const img = container.querySelector("img") as HTMLImageElement;
    fireEvent.error(img);
    expect(screen.getByTestId("article-fallback-thumb")).toBeInTheDocument();
  });

  it("never fetches or references an image-search endpoint for a missing image", () => {
    const { container } = render(
      <ArticlesWidget
        data={widget([{ title: "No image", source: "AP", url: "https://x.test/2", published: 0 }])}
      />,
    );
    expect(container.querySelectorAll("img")).toHaveLength(0);
  });

  it("links each card straight to the article url, opened in a new tab", () => {
    render(
      <ArticlesWidget
        data={widget([{ title: "A story", source: "BBC", url: "https://x.test/1", published: 0 }])}
      />,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "https://x.test/1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});
