/**
 * A judge composed of several judges.
 *
 * The point is not to average away noise but to notice it. When members
 * disagree sharply about a criterion, that disagreement is itself evidence
 * that the criterion is not cleanly decidable on this subject - so the panel
 * reports the median score with a confidence *reduced* by the spread, which
 * lets the existing `minConfidence` policy discard it rather than passing a
 * coin-flip off as a finding.
 */

import type { Judge } from "./judge.ts";
import { normalize } from "./judge.ts";
import type { Assessment, Evidence, Rubric, Subject } from "./types.ts";

export interface PanelOptions {
  /**
   * How strongly disagreement discounts confidence. At 1.0, a panel split
   * between 0 and 1 on a criterion lands at zero confidence.
   */
  dissentPenalty?: number;
  name?: string;
}

export class Panel implements Judge {
  readonly name: string;
  #members: Judge[];
  #dissentPenalty: number;

  constructor(members: Judge[], opts: PanelOptions = {}) {
    if (members.length === 0) throw new Error("A panel needs at least one member.");
    this.#members = members;
    this.#dissentPenalty = opts.dissentPenalty ?? 1;
    this.name = opts.name ?? `panel(${members.map((m) => m.name).join(", ")})`;
  }

  async assess(subject: Subject, rubric: Rubric): Promise<Assessment[]> {
    const rounds = await Promise.all(
      this.#members.map(async (m) => normalize(await m.assess(subject, rubric), rubric)),
    );

    return rubric.criteria.flatMap((criterion) => {
      const votes = rounds
        .map((r) => r.find((a) => a.criterionId === criterion.id))
        .filter((a): a is Assessment => a !== undefined);
      if (votes.length === 0) return [];
      return [combine(criterion.id, votes, this.#dissentPenalty)];
    });
  }
}

function combine(
  criterionId: string,
  votes: Assessment[],
  dissentPenalty: number,
): Assessment {
  const scores = votes.map((v) => v.score);
  const score = median(scores);

  // Spread, not variance: it is in score units, so the penalty is interpretable.
  const spread = Math.max(...scores) - Math.min(...scores);
  const confidence = clamp(
    mean(votes.map((v) => v.confidence)) * (1 - dissentPenalty * spread),
  );

  // Keep every member's evidence. A dissenting judge usually dissents because
  // it saw something the others did not, and that citation is the most useful
  // thing in the whole assessment.
  const evidence = dedupe(votes.flatMap((v) => v.evidence));

  const reasoning =
    spread > 0.25
      ? `Panel split (scores ${scores.map((s) => s.toFixed(2)).join(", ")}). ` +
        votes.map((v, i) => `(${i + 1}) ${v.reasoning}`).join(" ")
      : votes[0]!.reasoning;

  return { criterionId, score, confidence, reasoning, evidence };
}

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 === 0 ? (s[mid - 1]! + s[mid]!) / 2 : s[mid]!;
}

const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
const clamp = (n: number) => Math.min(1, Math.max(0, n));

function dedupe(xs: Evidence[]): Evidence[] {
  const seen = new Set<string>();
  return xs.filter((x) => {
    // Joined on NUL rather than a space: a locator containing a space could
    // otherwise collide with a different locator/observation split.
    const key = `${x.locator}\u0000${x.observation}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
