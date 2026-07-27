import { describe, expect, it } from "vitest";
import {
  buildPagesIndex,
  formatPageInjection,
  selectSections,
  splitSections,
  tokenize,
  type PageContent,
} from "./pages-index";

const PAGE: PageContent = {
  id: "p1",
  title: "Uploader guide",
  content:
    "Preamble prose introducing this page.\n\n" +
    "## Retry backoff\n" +
    "Uploads retry with exponential backoff and a 200ms jitter window.\n\n" +
    "## Auth tokens\n" +
    "Tokens rotate daily via the auth service.\n",
};

describe("tokenize", () => {
  it("lowercases, drops stopwords and tokens shorter than 3 chars", () => {
    expect(tokenize("The Upload AND a retry of it")).toEqual(["upload", "retry"]);
  });

  it("keeps path-like tokens (dots/slashes/dashes/underscores) as single terms", () => {
    expect(tokenize("fix src/core/hook.ts and retry-loop_2")).toEqual([
      "fix",
      "src/core/hook.ts",
      "retry-loop_2",
    ]);
  });

  it("returns [] for empty or all-stopword text", () => {
    expect(tokenize("")).toEqual([]);
    expect(tokenize("the and for that")).toEqual([]);
  });
});

describe("splitSections", () => {
  it("splits at headings, with the preamble as its own section headed by the page title", () => {
    const sections = splitSections(PAGE);
    expect(sections.map((s) => s.heading)).toEqual([
      "Uploader guide", // preamble inherits the page title as its heading
      "Retry backoff",
      "Auth tokens",
    ]);
    expect(sections[0].body).toBe("Preamble prose introducing this page.");
    expect(sections[1].body).toContain("200ms jitter window");
    for (const s of sections) {
      expect(s.pageId).toBe("p1");
      expect(s.pageTitle).toBe("Uploader guide");
    }
  });

  it("weights heading terms above body terms", () => {
    const [, retrySection] = splitSections(PAGE);
    // "retry" appears in the heading (weight 3) and once in the body (+1).
    expect(retrySection.terms.get("retry")).toBe(4);
    // "backoff": heading 3 + body 1.
    expect(retrySection.terms.get("backoff")).toBe(4);
    // body-only term: weight 1
    expect(retrySection.terms.get("jitter")).toBe(1);
    // page-title term folded in at weight 1
    expect(retrySection.terms.get("uploader")).toBe(1);
  });
});

describe("selectSections", () => {
  const index = buildPagesIndex([PAGE]);

  it("picks the section whose heading/terms overlap the prompt", () => {
    const picked = selectSections(index, "why does the upload retry backoff fail?");
    expect(picked).toHaveLength(1);
    expect(picked[0]).toMatchObject({
      pageId: "p1",
      pageTitle: "Uploader guide",
      heading: "Retry backoff",
    });
    expect(picked[0].text).toContain("200ms jitter window");
  });

  it("respects the score floor: an unrelated prompt selects nothing", () => {
    expect(selectSections(index, "completely unrelated banana smoothie question")).toEqual([]);
  });

  it("honors maxSections", () => {
    const picked = selectSections(index, "retry backoff and auth tokens", { maxSections: 1 });
    expect(picked).toHaveLength(1);
  });

  it("trims a section over the token budget at a paragraph boundary and appends …", () => {
    const para1 = "Uploads retry with exponential backoff.";
    const para2 = "Much later detail. ".repeat(20).trim();
    const longPage: PageContent = {
      id: "p2",
      title: "Long page",
      content: `## Retry backoff\n${para1}\n\n${para2}\n`,
    };
    // budget = 20 tokens * 4 chars = 80 chars: enough for para1 but not para2.
    const picked = selectSections(buildPagesIndex([longPage]), "retry backoff", {
      budgetTokens: 20,
    });
    expect(picked).toHaveLength(1);
    expect(picked[0].text).toBe(`${para1}\n…`);
    expect(picked[0].text).not.toContain("Much later detail");
  });
});

describe("formatPageInjection", () => {
  it("renders provenance (page title + heading) and the full-page tool pointer per section", () => {
    const out = formatPageInjection([
      {
        pageId: "p1",
        pageTitle: "Uploader guide",
        heading: "Retry backoff",
        text: "Uploads retry with exponential backoff.",
      },
    ]);
    expect(out).toContain("Relevant project knowledge");
    expect(out).toContain('From "Uploader guide" › "Retry backoff":');
    expect(out).toContain("Uploads retry with exponential backoff.");
    expect(out).toContain("hindsight_read_knowledge_page p1");
  });

  it("returns '' for an empty selection", () => {
    expect(formatPageInjection([])).toBe("");
  });
});
