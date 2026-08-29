/**
 * Core vocabulary of the judgment system.
 *
 * The central split: an `Assessment` is what the (fallible, fuzzy) judge
 * produces, a `Verdict` is what deterministic policy computes from it.
 * Nothing in this file knows about GitHub, code review, or any LLM.
 */

/** A normalized score in [0, 1]. 0 = fails the criterion, 1 = fully satisfies it. */
export type Score = number;

/** How sure the judge is about a score. Low confidence can trigger abstention. */
export type Confidence = number;

/** What the subject under judgment consists of. Free-form; rubrics interpret it. */
export interface Subject {
  /** Stable identifier, used for audit records and caching. */
  id: string;
  /** Short human-readable label, e.g. "PR #42: add retry logic". */
  title: string;
  /** The material to be judged. */
  content: string;
  /** Optional side-channel facts the judge may use but must not judge. */
  context?: Record<string, string>;
}

/** A single dimension the subject is judged on. */
export interface Criterion {
  /** Stable slug, e.g. "correctness". Referenced by policy, so it must not drift. */
  id: string;
  /** One line stating what this criterion measures. */
  title: string;
  /**
   * Instructions to the judge: what evidence counts, and what the score anchors
   * mean. Vague guidance is the single biggest source of judge noise, so this
   * should describe observable properties, not vibes.
   */
  guidance: string;
  /**
   * Relative weight in the aggregate score. Weights are normalized across the
   * rubric, so absolute magnitude does not matter.
   */
  weight: number;
  /**
   * If the score falls below this, the criterion blocks regardless of how good
   * the aggregate looks. This is what stops a strong average from burying a
   * fatal flaw. Omit for non-blocking criteria.
   */
  blockBelow?: number;
}

/** A named, versioned set of criteria plus the policy for turning scores into a verdict. */
export interface Rubric {
  id: string;
  /** Bump whenever criteria or policy change: recorded in every judgment. */
  version: string;
  /** Framing given to the judge before the criteria. Sets the standard applied. */
  preamble: string;
  criteria: Criterion[];
  policy: Policy;
}

/** Deterministic rules mapping assessments to a verdict. No model involved. */
export interface Policy {
  /** At or above this aggregate score, and with nothing blocking, the subject passes. */
  passAt: number;
  /** Below this aggregate score the subject is rejected outright. */
  rejectBelow: number;
  /**
   * Scores carrying less confidence than this are treated as unreliable: they
   * are excluded from the aggregate and force the verdict to ABSTAIN rather
   * than silently dragging the average around.
   */
  minConfidence: number;
  /**
   * How much of the rubric (by weight) must be reliably scored for a verdict to
   * be issued at all. Guards against judging on a fragment of the evidence.
   */
  minCoverage: number;
  /**
   * Require at least one piece of cited evidence for a score to count. Evidence-free
   * scores are the judge's least reliable output, so they are dropped by default.
   */
  requireEvidence: boolean;
}

/** A specific, checkable observation backing a score. */
export interface Evidence {
  /** Where in the subject this was observed, e.g. "src/retry.ts:41". */
  locator: string;
  /** What was observed. Should be verifiable against the subject, not a summary. */
  observation: string;
}

/** The judge's raw output for one criterion, before policy is applied. */
export interface Assessment {
  criterionId: string;
  score: Score;
  confidence: Confidence;
  reasoning: string;
  evidence: Evidence[];
}

/** An assessment after policy has decided whether and how it counts. */
export interface ScoredCriterion {
  criterion: Criterion;
  assessment: Assessment;
  /** False when dropped for low confidence or missing evidence. */
  counted: boolean;
  /** Present when `counted` is false: why it was discarded. */
  discardReason?: DiscardReason;
  /** True when this criterion independently blocks a pass. */
  blocking: boolean;
}

export type DiscardReason = "low-confidence" | "no-evidence" | "not-assessed";

export type VerdictKind =
  /** Meets the standard. */
  | "PASS"
  /** Falls short but is fixable; the blocking criteria say what to fix. */
  | "REVISE"
  /** Falls short badly enough that revision is not the right frame. */
  | "REJECT"
  /** Not enough reliable signal to judge. Never a soft "no" — it means "ask again". */
  | "ABSTAIN";

/** The complete, auditable record of one judgment. */
export interface Judgment {
  subjectId: string;
  rubricId: string;
  rubricVersion: string;
  verdict: VerdictKind;
  /** Weighted mean of counted scores, or null when coverage was insufficient. */
  score: Score | null;
  /** Fraction of rubric weight that was reliably assessed. */
  coverage: number;
  /** Criterion ids that independently block a pass. */
  blockedBy: string[];
  criteria: ScoredCriterion[];
  /** One-line statement of why this verdict, derived from policy, not the model. */
  rationale: string;
  judgedAt: string;
  /** Identifies the judge that produced the assessments, for reproducibility. */
  judge: string;
}
