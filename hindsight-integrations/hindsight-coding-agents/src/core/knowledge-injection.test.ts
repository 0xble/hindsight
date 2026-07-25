import { describe, expect, it } from "vitest";
import { parsePageList, buildKnowledgePreamble, buildRosterRefresh } from "./knowledge-injection";

describe("parsePageList", () => {
  it("extracts {id,title} from the mental-model list shape, tolerating junk", () => {
    const raw = {
      items: [
        { id: "p1", name: "Component map" },
        { id: "p2", name: "Core concepts" },
        { nope: 1 },
      ],
    };
    expect(parsePageList(raw)).toEqual([
      { id: "p1", title: "Component map" },
      { id: "p2", title: "Core concepts" },
    ]);
  });
  it("returns [] for null/garbage", () => {
    expect(parsePageList(null)).toEqual([]);
    expect(parsePageList(42 as unknown)).toEqual([]);
  });
});

describe("buildKnowledgePreamble", () => {
  it("includes guidance, a roster of pages, a read tool, and the capture-initiative nudge", () => {
    const out = buildKnowledgePreamble([{ id: "p1", title: "Component map" }]);
    expect(out).toContain("<hindsight_knowledge>");
    expect(out).toContain("Component map");
    expect(out).toContain("p1");
    expect(out).toMatch(/hindsight_read_knowledge_page/);
    // Must nudge the agent to RECORD major features, not just read pages.
    expect(out).toContain("hindsight_capture_initiative");
  });
  it("has an empty-state line when there are no pages", () => {
    const out = buildKnowledgePreamble([]);
    expect(out).toMatch(/no knowledge pages yet|still learning/i);
  });
});

describe("buildRosterRefresh", () => {
  it("is a compact 'current pages' block listing ids+titles, with a capture reminder", () => {
    const out = buildRosterRefresh([{ id: "p1", title: "Component map" }]);
    expect(out).toContain("Component map");
    expect(out).toContain("p1");
    expect(out).toContain("hindsight_capture_initiative");
  });
  it("returns undefined when there are no pages (nothing to refresh)", () => {
    expect(buildRosterRefresh([])).toBeUndefined();
  });
});
