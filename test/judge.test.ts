import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { judge as runJudgment, normalize } from "../src/judgment/judge.ts";
import { Panel } from "../src/judgment/panel.ts";
import { ScriptedJudge } from "../src/judgment/providers/scripted.ts";
import { defineRubric, RubricError, validateRubric } from "../src/judgment/rubric.ts";
import type { Assessment, Rubric } from "../src/judgment/types.ts";

const rubric: Rubric = defineRubric({
  id: "test",
  version: "1.0.0",
  preamble: "Test rubric.",
  criteria: [
    { id: "a", title: "A", guidance: "g", weight: 1 },
    { id: "b", title: "B", guidance: "g", weight: 1 },
  ],
});

const ev = [{ locator: "f:1", observation: "o" }];
const at = (criterionId: string, score: number, confidence = 0.9): Assessment => ({
  criterionId,
  score,
  confidence,
  reasoning: "r",
  evidence: ev,
});

describe("normalize", () => {
  it("clamps scores and confidence into range", () => {
    const [out] = normalize([at("a", 1.7, 4)], rubric);
    assert.equal(out!.score, 1);
    assert.equal(out!.confidence, 1);
  });

  it("floors negative values at zero", () => {
    const [out] = normalize([at("a", -0.5, -2)], rubric);
    assert.equal(out!.score, 0);
    assert.equal(out!.confidence, 0);
  });

  it("treats non-finite values as unusable rather than propagating NaN", () => {
    // Infinity becomes 0, not 1. Reading a garbage confidence as *maximum*
    // confidence would let a meaningless score past the minConfidence gate and
    // count toward a verdict; zeroing it gets the assessment discarded instead.
    const [out] = normalize([at("a", NaN, Infinity)], rubric);
    assert.equal(out!.score, 0);
    assert.equal(out!.confidence, 0);
  });

  it("drops criteria the rubric does not define", () => {
    const out = normalize([at("a", 0.5), at("invented", 0.9)], rubric);
    assert.deepEqual(out.map((o) => o.criterionId), ["a"]);
  });

  it("keeps the first of a repeated criterion", () => {
    const out = normalize([at("a", 0.2), at("a", 0.9)], rubric);
    assert.equal(out.length, 1);
    assert.equal(out[0]!.score, 0.2);
  });

  it("drops malformed evidence entries without dropping the assessment", () => {
    const raw = [
      { ...at("a", 0.5), evidence: [{ locator: "f:1" } as never, ...ev] },
    ];
    const [out] = normalize(raw, rubric);
    assert.equal(out!.evidence.length, 1);
  });
});

describe("validateRubric", () => {
  const base = {
    id: "r",
    version: "1",
    preamble: "p",
    criteria: [{ id: "a", title: "A", guidance: "g", weight: 1 }],
  };

  it("rejects duplicate criterion ids", () => {
    assert.throws(
      () =>
        defineRubric({
          ...base,
          criteria: [...base.criteria, { id: "a", title: "A2", guidance: "g", weight: 1 }],
        }),
      RubricError,
    );
  });

  it("rejects a rubric whose weights are all zero", () => {
    assert.throws(
      () => defineRubric({ ...base, criteria: [{ ...base.criteria[0]!, weight: 0 }] }),
      RubricError,
    );
  });

  it("rejects guidance-free criteria", () => {
    assert.throws(
      () => defineRubric({ ...base, criteria: [{ ...base.criteria[0]!, guidance: "  " }] }),
      RubricError,
    );
  });

  it("rejects a policy where rejectBelow exceeds passAt", () => {
    assert.throws(
      () => defineRubric({ ...base, policy: { passAt: 0.4, rejectBelow: 0.8 } }),
      /leaving no band for REVISE/,
    );
  });

  it("rejects thresholds outside the unit interval", () => {
    assert.throws(() => defineRubric({ ...base, policy: { passAt: 1.5 } }), RubricError);
  });

  it("accepts a well-formed rubric unchanged", () => {
    const r = defineRubric(base);
    assert.equal(validateRubric(r), r);
    assert.equal(r.policy.passAt, 0.75); // default filled in
  });
});

describe("Panel", () => {
  it("takes the median score across members", async () => {
    const panel = new Panel([
      new ScriptedJudge([at("a", 0.2)], "j1"),
      new ScriptedJudge([at("a", 0.8)], "j2"),
      new ScriptedJudge([at("a", 0.9)], "j3"),
    ]);
    const [out] = await panel.assess({ id: "s", title: "t", content: "c" }, rubric);
    assert.equal(out!.score, 0.8);
  });

  it("discounts confidence when members disagree", async () => {
    const agree = new Panel([
      new ScriptedJudge([at("a", 0.8, 1)], "j1"),
      new ScriptedJudge([at("a", 0.8, 1)], "j2"),
    ]);
    const split = new Panel([
      new ScriptedJudge([at("a", 0.1, 1)], "j1"),
      new ScriptedJudge([at("a", 0.9, 1)], "j2"),
    ]);

    const [agreed] = await agree.assess({ id: "s", title: "t", content: "c" }, rubric);
    const [disputed] = await split.assess({ id: "s", title: "t", content: "c" }, rubric);

    assert.equal(agreed!.confidence, 1);
    assert.ok(
      disputed!.confidence < 0.3,
      `expected a split panel to lose confidence, got ${disputed!.confidence}`,
    );
  });

  it("a split panel abstains rather than reporting the midpoint as fact", async () => {
    const split = new Panel([
      new ScriptedJudge([at("a", 0, 1), at("b", 0, 1)], "j1"),
      new ScriptedJudge([at("a", 1, 1), at("b", 1, 1)], "j2"),
    ]);
    const j = await runJudgment({ id: "s", title: "t", content: "c" }, rubric, split);
    assert.equal(j.verdict, "ABSTAIN");
  });

  it("keeps the dissenting member's evidence", async () => {
    const panel = new Panel([
      new ScriptedJudge([{ ...at("a", 0.9), evidence: ev }], "j1"),
      new ScriptedJudge(
        [{ ...at("a", 0.1), evidence: [{ locator: "f:99", observation: "the bug" }] }],
        "j2",
      ),
    ]);
    const [out] = await panel.assess({ id: "s", title: "t", content: "c" }, rubric);
    assert.ok(out!.evidence.some((e) => e.observation === "the bug"));
  });

  it("refuses to be constructed empty", () => {
    assert.throws(() => new Panel([]), /at least one member/);
  });
});

describe("judge", () => {
  it("normalizes provider output before adjudicating", async () => {
    // Out-of-range and hallucinated output must not reach the policy layer.
    const messy = new ScriptedJudge([at("a", 5), at("b", 0.9), at("ghost", 1)], "messy");
    const j = await runJudgment({ id: "s", title: "t", content: "c" }, rubric, messy);
    assert.equal(j.score, 0.95);
    assert.equal(j.criteria.length, 2);
  });
});
