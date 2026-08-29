import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { adjudicate } from "../src/judgment/policy.ts";
import { defineRubric } from "../src/judgment/rubric.ts";
import type { Assessment, Rubric } from "../src/judgment/types.ts";

const rubric: Rubric = defineRubric({
  id: "test",
  version: "1.0.0",
  preamble: "Test rubric.",
  criteria: [
    {
      id: "critical",
      title: "Critical",
      guidance: "Must hold.",
      weight: 3,
      blockBelow: 0.5,
    },
    { id: "major", title: "Major", guidance: "Should hold.", weight: 2 },
    { id: "minor", title: "Minor", guidance: "Nice to have.", weight: 1 },
  ],
  policy: {
    passAt: 0.75,
    rejectBelow: 0.3,
    minConfidence: 0.5,
    minCoverage: 0.6,
    requireEvidence: true,
  },
});

/** An assessment that passes screening, so tests vary only what they mean to. */
function ok(criterionId: string, score: number, confidence = 0.9): Assessment {
  return {
    criterionId,
    score,
    confidence,
    reasoning: "because",
    evidence: [{ locator: "file.ts:1", observation: "observed" }],
  };
}

const run = (assessments: Assessment[]) =>
  adjudicate({
    subjectId: "s1",
    rubric,
    assessments,
    judge: "test",
    now: () => new Date("2026-01-01T00:00:00Z"),
  });

describe("adjudicate", () => {
  it("passes when every criterion is strong", () => {
    const j = run([ok("critical", 0.9), ok("major", 0.9), ok("minor", 0.9)]);
    assert.equal(j.verdict, "PASS");
    assert.equal(j.score, 0.9);
    assert.equal(j.coverage, 1);
    assert.deepEqual(j.blockedBy, []);
  });

  it("weights criteria rather than averaging them evenly", () => {
    // Unweighted mean would be 0.6; weighted by 3/2/1 it is 0.5.
    const j = run([ok("critical", 0.3), ok("major", 0.8), ok("minor", 0.9)]);
    assert.equal(j.score, (0.3 * 3 + 0.8 * 2 + 0.9 * 1) / 6);
  });

  it("blocks on a failing blocking criterion even when the aggregate would pass", () => {
    // Weighted score is 0.7 - above rejectBelow, and the two non-blocking
    // criteria are perfect. The blocking criterion alone must force REVISE.
    const j = run([ok("critical", 0.4), ok("major", 1), ok("minor", 1)]);
    assert.equal(j.verdict, "REVISE");
    assert.deepEqual(j.blockedBy, ["critical"]);
    assert.match(j.rationale, /Critical/);
  });

  it("rejects rather than revises when the aggregate is below the floor", () => {
    const j = run([ok("critical", 0.1), ok("major", 0.2), ok("minor", 0.2)]);
    assert.equal(j.verdict, "REJECT");
  });

  it("revises when nothing blocks but the score is short of passing", () => {
    const j = run([ok("critical", 0.6), ok("major", 0.6), ok("minor", 0.6)]);
    assert.equal(j.verdict, "REVISE");
    assert.deepEqual(j.blockedBy, []);
  });

  it("discards evidence-free scores instead of counting them", () => {
    const j = run([
      { ...ok("critical", 0.9), evidence: [] },
      ok("major", 0.9),
      ok("minor", 0.9),
    ]);
    const critical = j.criteria.find((c) => c.criterion.id === "critical")!;
    assert.equal(critical.counted, false);
    assert.equal(critical.discardReason, "no-evidence");
    // Only major+minor counted, so the aggregate excludes the dropped score.
    assert.equal(j.score, 0.9);
    assert.equal(j.coverage, 0.5);
  });

  it("discards low-confidence scores", () => {
    const j = run([ok("critical", 0.9, 0.2), ok("major", 0.9), ok("minor", 0.9)]);
    const critical = j.criteria.find((c) => c.criterion.id === "critical")!;
    assert.equal(critical.counted, false);
    assert.equal(critical.discardReason, "low-confidence");
  });

  it("does not let an unreliable low score block", () => {
    // A low score the judge is unsure of must not block a PR. It costs
    // coverage instead, which is what can push the verdict to ABSTAIN.
    const j = run([ok("critical", 0.1, 0.2), ok("major", 0.9), ok("minor", 0.9)]);
    assert.deepEqual(j.blockedBy, []);
    assert.equal(j.verdict, "ABSTAIN");
  });

  it("abstains when too little of the rubric was assessed", () => {
    const j = run([ok("major", 0.9)]);
    assert.equal(j.verdict, "ABSTAIN");
    assert.ok(j.coverage < 0.6);
    assert.match(j.rationale, /reliably assessed/);
  });

  it("abstains rather than passing when nothing at all was assessed", () => {
    const j = run([]);
    assert.equal(j.verdict, "ABSTAIN");
    assert.equal(j.score, null);
    assert.equal(j.coverage, 0);
    for (const c of j.criteria) assert.equal(c.discardReason, "not-assessed");
  });

  it("records rubric identity so a verdict can be traced to its policy", () => {
    const j = run([ok("critical", 0.9), ok("major", 0.9), ok("minor", 0.9)]);
    assert.equal(j.rubricId, "test");
    assert.equal(j.rubricVersion, "1.0.0");
    assert.equal(j.judge, "test");
    assert.equal(j.subjectId, "s1");
  });

  it("is deterministic: identical assessments give identical verdicts", () => {
    const input = [ok("critical", 0.62), ok("major", 0.81), ok("minor", 0.44)];
    const a = run(input);
    const b = run(input);
    assert.deepEqual({ ...a, judgedAt: "" }, { ...b, judgedAt: "" });
  });
});
