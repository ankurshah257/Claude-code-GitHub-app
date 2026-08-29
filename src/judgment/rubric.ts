/**
 * Rubric construction and validation.
 *
 * A malformed rubric produces confidently wrong verdicts rather than an error,
 * so it is worth failing loudly at load time. These checks catch the definition
 * mistakes that are invisible at runtime: thresholds that can never be met,
 * duplicate ids that silently shadow each other, weights that are all zero.
 */

import type { Policy, Rubric } from "./types.ts";

export class RubricError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RubricError";
  }
}

/** Policy defaults chosen to be cautious: abstain rather than guess. */
export const DEFAULT_POLICY: Policy = {
  passAt: 0.75,
  rejectBelow: 0.35,
  minConfidence: 0.5,
  minCoverage: 0.6,
  requireEvidence: true,
};

/** Validate a rubric, throwing `RubricError` on the first problem found. */
export function validateRubric(rubric: Rubric): Rubric {
  const where = `rubric "${rubric.id}"`;

  if (!rubric.id.trim()) throw new RubricError("Rubric id must not be empty.");
  if (!rubric.version.trim())
    throw new RubricError(`${where} must declare a version.`);
  if (rubric.criteria.length === 0)
    throw new RubricError(`${where} defines no criteria.`);

  const seen = new Set<string>();
  for (const c of rubric.criteria) {
    if (!c.id.trim())
      throw new RubricError(`${where} has a criterion with an empty id.`);
    if (seen.has(c.id))
      throw new RubricError(`${where} defines criterion "${c.id}" more than once.`);
    seen.add(c.id);

    if (!Number.isFinite(c.weight) || c.weight < 0)
      throw new RubricError(
        `${where}: criterion "${c.id}" has weight ${c.weight}; weights must be finite and non-negative.`,
      );
    if (c.blockBelow !== undefined && !inUnit(c.blockBelow))
      throw new RubricError(
        `${where}: criterion "${c.id}" has blockBelow ${c.blockBelow}, outside [0, 1].`,
      );
    if (!c.guidance.trim())
      throw new RubricError(
        `${where}: criterion "${c.id}" has no guidance, so the judge has nothing to anchor scores to.`,
      );
  }

  if (rubric.criteria.every((c) => c.weight === 0))
    throw new RubricError(
      `${where} gives every criterion zero weight, so no score can ever be computed.`,
    );

  validatePolicy(rubric.policy, where);
  return rubric;
}

function validatePolicy(policy: Policy, where: string): void {
  for (const key of ["passAt", "rejectBelow", "minConfidence", "minCoverage"] as const) {
    if (!inUnit(policy[key]))
      throw new RubricError(`${where}: policy.${key} is ${policy[key]}, outside [0, 1].`);
  }
  // Overlapping thresholds would make REVISE unreachable and hide the distinction
  // between "fix this" and "start over".
  if (policy.rejectBelow > policy.passAt)
    throw new RubricError(
      `${where}: policy.rejectBelow (${policy.rejectBelow}) exceeds policy.passAt (${policy.passAt}), leaving no band for REVISE.`,
    );
}

const inUnit = (n: number) => Number.isFinite(n) && n >= 0 && n <= 1;

/** Build a validated rubric, filling in the default policy where unspecified. */
export function defineRubric(
  spec: Omit<Rubric, "policy"> & { policy?: Partial<Policy> },
): Rubric {
  return validateRubric({
    ...spec,
    policy: { ...DEFAULT_POLICY, ...spec.policy },
  });
}
