/**
 * The decision layer.
 *
 * Everything here is pure and deterministic: the same assessments and rubric
 * always produce the same verdict. The judge supplies fallible per-criterion
 * scores; this module decides what they add up to. Keeping the two separate is
 * what makes a judgment auditable — you can disagree with a score without the
 * decision rule being a mystery.
 */

import type {
  Assessment,
  Judgment,
  Rubric,
  ScoredCriterion,
  VerdictKind,
} from "./types.ts";

export interface AdjudicateOptions {
  subjectId: string;
  rubric: Rubric;
  assessments: Assessment[];
  /** Identifier of the judge that produced the assessments. */
  judge: string;
  /** Injectable for deterministic tests. */
  now?: () => Date;
}

/** Apply rubric policy to raw assessments, producing the final judgment record. */
export function adjudicate(opts: AdjudicateOptions): Judgment {
  const { rubric, assessments, subjectId, judge } = opts;
  const now = opts.now ?? (() => new Date());
  const { policy } = rubric;

  const byId = new Map(assessments.map((a) => [a.criterionId, a]));
  const scored: ScoredCriterion[] = rubric.criteria.map((criterion) =>
    screen(criterion, byId.get(criterion.id), policy),
  );

  const totalWeight = sum(rubric.criteria.map((c) => c.weight));
  const countedWeight = sum(
    scored.filter((s) => s.counted).map((s) => s.criterion.weight),
  );

  // A rubric with no weight at all is a definition error, not a judgment about
  // the subject. Treat it as zero coverage rather than dividing by zero.
  const coverage = totalWeight > 0 ? countedWeight / totalWeight : 0;

  const score =
    countedWeight > 0
      ? sum(
          scored
            .filter((s) => s.counted)
            .map((s) => s.assessment.score * s.criterion.weight),
        ) / countedWeight
      : null;

  const blockedBy = scored.filter((s) => s.blocking).map((s) => s.criterion.id);
  const verdict = decide({ coverage, score, blockedBy, policy });

  return {
    subjectId,
    rubricId: rubric.id,
    rubricVersion: rubric.version,
    verdict,
    score,
    coverage,
    blockedBy,
    criteria: scored,
    rationale: explain({ verdict, score, coverage, blockedBy, rubric }),
    judgedAt: now().toISOString(),
    judge,
  };
}

/**
 * Decide whether a single assessment is trustworthy enough to count, and
 * whether it independently blocks a pass.
 */
function screen(
  criterion: Rubric["criteria"][number],
  assessment: Assessment | undefined,
  policy: Rubric["policy"],
): ScoredCriterion {
  if (!assessment) {
    return {
      criterion,
      assessment: {
        criterionId: criterion.id,
        score: 0,
        confidence: 0,
        reasoning: "The judge returned no assessment for this criterion.",
        evidence: [],
      },
      counted: false,
      discardReason: "not-assessed",
      blocking: false,
    };
  }

  // Order matters only for which reason gets reported; both are disqualifying.
  if (policy.requireEvidence && assessment.evidence.length === 0) {
    return {
      criterion,
      assessment,
      counted: false,
      discardReason: "no-evidence",
      blocking: false,
    };
  }
  if (assessment.confidence < policy.minConfidence) {
    return {
      criterion,
      assessment,
      counted: false,
      discardReason: "low-confidence",
      blocking: false,
    };
  }

  // Only a score we trust is allowed to block. An unreliable low score reduces
  // coverage instead, which can push the whole judgment to ABSTAIN.
  const blocking =
    criterion.blockBelow !== undefined && assessment.score < criterion.blockBelow;

  return { criterion, assessment, counted: true, blocking };
}

function decide(args: {
  coverage: number;
  score: number | null;
  blockedBy: string[];
  policy: Rubric["policy"];
}): VerdictKind {
  const { coverage, score, blockedBy, policy } = args;

  // Not enough of the rubric was reliably assessed to say anything at all.
  if (score === null || coverage < policy.minCoverage) return "ABSTAIN";

  // Rejection outranks blocking: both are failures, this one is the worse call.
  if (score < policy.rejectBelow) return "REJECT";
  if (blockedBy.length > 0) return "REVISE";
  if (score >= policy.passAt) return "PASS";
  return "REVISE";
}

function explain(args: {
  verdict: VerdictKind;
  score: number | null;
  coverage: number;
  blockedBy: string[];
  rubric: Rubric;
}): string {
  const { verdict, score, coverage, blockedBy, rubric } = args;
  const pct = (n: number) => `${Math.round(n * 100)}%`;
  const titleOf = (id: string) =>
    rubric.criteria.find((c) => c.id === id)?.title ?? id;

  switch (verdict) {
    case "ABSTAIN":
      return `Only ${pct(coverage)} of the rubric could be reliably assessed, below the ${pct(rubric.policy.minCoverage)} needed to reach a verdict.`;
    case "REJECT":
      return `Scored ${pct(score ?? 0)}, below the ${pct(rubric.policy.rejectBelow)} floor.`;
    case "REVISE":
      return blockedBy.length > 0
        ? `Scored ${pct(score ?? 0)}, but ${blockedBy.length === 1 ? "a blocking criterion fell" : "blocking criteria fell"} short: ${blockedBy.map(titleOf).join(", ")}.`
        : `Scored ${pct(score ?? 0)}, short of the ${pct(rubric.policy.passAt)} needed to pass.`;
    case "PASS":
      return `Scored ${pct(score ?? 0)} against ${pct(coverage)} of the rubric, with nothing blocking.`;
  }
}

const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
