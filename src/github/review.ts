/**
 * Rendering a judgment as a pull request review.
 *
 * Two deliberate defaults, both about not overclaiming:
 *
 *  - The system never submits APPROVE. A passing judgment means "nothing was
 *    found against this", which is not the same as a human vouching for it, and
 *    an approval carries institutional weight this system has not earned.
 *  - REQUEST_CHANGES is opt-in. Blocking someone's PR on a model's reading is a
 *    real cost to them, so a repository has to ask for it explicitly.
 *
 * The rendered comment always shows what was discarded and what was not read,
 * because a confident verdict over half the diff is the failure mode worth
 * making visible.
 */

import type { Judgment, ScoredCriterion } from "../judgment/types.ts";

export type ReviewEvent = "COMMENT" | "REQUEST_CHANGES" | "APPROVE";

export interface ReviewRenderOptions {
  /** Allow the review to block the PR when the verdict is REVISE or REJECT. */
  allowRequestChanges?: boolean;
  /** Files excluded from the subject, surfaced so readers know the blind spots. */
  omitted?: { filename: string; reason: string }[];
}

export interface RenderedReview {
  event: ReviewEvent;
  body: string;
}

export function renderReview(
  judgment: Judgment,
  opts: ReviewRenderOptions = {},
): RenderedReview {
  const blocking = judgment.verdict === "REVISE" || judgment.verdict === "REJECT";
  const event: ReviewEvent =
    blocking && opts.allowRequestChanges ? "REQUEST_CHANGES" : "COMMENT";

  return { event, body: renderBody(judgment, opts) };
}

const HEADLINE: Record<Judgment["verdict"], string> = {
  PASS: "Nothing found against this change",
  REVISE: "Changes requested",
  REJECT: "This change should not merge as written",
  ABSTAIN: "No verdict - not enough was reliably assessed",
};

function renderBody(judgment: Judgment, opts: ReviewRenderOptions): string {
  const out: string[] = [];

  out.push(`### ${HEADLINE[judgment.verdict]}`);
  out.push("");
  out.push(judgment.rationale);
  out.push("");

  const counted = judgment.criteria.filter((c) => c.counted);
  if (counted.length > 0) {
    out.push("| Criterion | Score | |");
    out.push("| --- | --- | --- |");
    for (const c of counted) {
      const flag = c.blocking ? "**blocking**" : "";
      out.push(`| ${c.criterion.title} | ${pct(c.assessment.score)} | ${flag} |`);
    }
    out.push("");
  }

  const findings = counted
    .filter((c) => c.blocking || c.assessment.score < 0.7)
    .sort((a, b) => Number(b.blocking) - Number(a.blocking));

  if (findings.length > 0) {
    out.push("#### Findings");
    out.push("");
    for (const c of findings) out.push(renderFinding(c));
  }

  const discarded = judgment.criteria.filter((c) => !c.counted);
  if (discarded.length > 0) {
    out.push("#### Not counted");
    out.push("");
    for (const c of discarded) {
      out.push(`- **${c.criterion.title}** - ${explainDiscard(c)}`);
    }
    out.push("");
  }

  if (opts.omitted && opts.omitted.length > 0) {
    out.push("#### Not reviewed");
    out.push("");
    out.push("These files were not part of what was judged:");
    out.push("");
    for (const o of opts.omitted) out.push(`- \`${o.filename}\` - ${o.reason}`);
    out.push("");
  }

  out.push("<details><summary>How this verdict was reached</summary>");
  out.push("");
  out.push(
    `Rubric \`${judgment.rubricId}\` v${judgment.rubricVersion}, judged by \`${judgment.judge}\`. ` +
      `Weighted score ${judgment.score === null ? "n/a" : pct(judgment.score)} over ` +
      `${pct(judgment.coverage)} of the rubric.`,
  );
  out.push("");
  out.push(
    "Scores come from a model; the verdict does not. A fixed policy combines the " +
      "scores above, so the same scores always produce the same verdict. Criteria " +
      "scored without cited evidence, or with low confidence, are discarded rather " +
      "than counted - which is why coverage can be below 100%.",
  );
  out.push("");
  out.push("</details>");

  return out.join("\n");
}

function renderFinding(c: ScoredCriterion): string {
  const lines: string[] = [];
  lines.push(
    `**${c.criterion.title}** - ${pct(c.assessment.score)}${c.blocking ? " (blocking)" : ""}`,
  );
  lines.push("");
  lines.push(c.assessment.reasoning);
  lines.push("");
  for (const e of c.assessment.evidence) {
    lines.push(`> \`${e.locator}\` - ${e.observation}`);
  }
  lines.push("");
  return lines.join("\n");
}

function explainDiscard(c: ScoredCriterion): string {
  switch (c.discardReason) {
    case "no-evidence":
      return "the judge cited no specific evidence, so the score was not counted.";
    case "low-confidence":
      return `the judge reported low confidence (${pct(c.assessment.confidence)}), so the score was not counted.`;
    case "not-assessed":
      return "the judge returned no assessment for this criterion.";
    default:
      return "not counted.";
  }
}

const pct = (n: number) => `${Math.round(n * 100)}%`;
