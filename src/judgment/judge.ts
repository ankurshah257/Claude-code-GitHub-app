/**
 * The assessment layer: anything that can score a subject against a rubric.
 *
 * Providers live behind this interface so the decision layer never depends on
 * a particular model, or on there being a model at all — a human panel or a
 * static analyzer can implement `Judge` just as well.
 */

import { adjudicate } from "./policy.ts";
import type { Assessment, Judgment, Rubric, Subject } from "./types.ts";

export interface Judge {
  /** Stable name recorded in the judgment for reproducibility, e.g. "anthropic:claude-opus-5". */
  readonly name: string;
  assess(subject: Subject, rubric: Rubric): Promise<Assessment[]>;
}

/** Assess a subject and adjudicate the result into a verdict. */
export async function judge(
  subject: Subject,
  rubric: Rubric,
  judgeImpl: Judge,
): Promise<Judgment> {
  const raw = await judgeImpl.assess(subject, rubric);
  return adjudicate({
    subjectId: subject.id,
    rubric,
    assessments: normalize(raw, rubric),
    judge: judgeImpl.name,
  });
}

/**
 * Coerce provider output into the shape the policy layer assumes.
 *
 * Models routinely return scores slightly outside [0, 1], repeat a criterion,
 * or invent one that is not in the rubric. Silently normalizing here keeps
 * every downstream consumer from having to defend against it. Anything that
 * cannot be salvaged is dropped, which costs coverage — the honest outcome,
 * since a criterion we could not read is one we did not assess.
 */
export function normalize(raw: Assessment[], rubric: Rubric): Assessment[] {
  const known = new Set(rubric.criteria.map((c) => c.id));
  const seen = new Set<string>();
  const out: Assessment[] = [];

  for (const a of raw) {
    if (!known.has(a.criterionId)) continue; // hallucinated criterion
    if (seen.has(a.criterionId)) continue; // keep the first, ignore repeats
    seen.add(a.criterionId);

    out.push({
      criterionId: a.criterionId,
      score: clamp(a.score),
      confidence: clamp(a.confidence),
      reasoning: typeof a.reasoning === "string" ? a.reasoning : "",
      evidence: Array.isArray(a.evidence)
        ? a.evidence.filter(
            (e) => e && typeof e.locator === "string" && typeof e.observation === "string",
          )
        : [],
    });
  }
  return out;
}

/** Non-finite input means the provider gave us nothing usable; score it 0. */
const clamp = (n: number): number =>
  Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0;

/** Render a rubric as the instruction block given to a model-backed judge. */
export function buildPrompt(subject: Subject, rubric: Rubric): string {
  const criteria = rubric.criteria
    .map(
      (c) =>
        `- id: ${c.id}\n  measures: ${c.title}\n  how to score: ${c.guidance}`,
    )
    .join("\n");

  const context = subject.context
    ? Object.entries(subject.context)
        .map(([k, v]) => `${k}: ${v}`)
        .join("\n")
    : "(none)";

  return `${rubric.preamble}

Score the subject below against every criterion. Return one assessment per
criterion — do not skip any, and do not invent criteria that are not listed.

CRITERIA
${criteria}

SCORING RULES
- score is 0 to 1: 0 means the criterion is clearly not met, 1 means fully met.
- confidence is 0 to 1: how sure you are of your own score. Report low
  confidence when the subject does not give you enough to judge on. A low
  confidence is not a penalty against the subject — it is discarded, not
  counted against it, so report it honestly rather than guessing.
- evidence must cite specific, checkable locations in the subject. An
  assessment with no evidence is discarded, so do not pad it with restatements
  of the criterion. If you genuinely found nothing to cite, return empty
  evidence and low confidence rather than inventing a citation.
- Judge only what the subject contains. Context is given as background and
  must not itself be scored.

CONTEXT
${context}

SUBJECT: ${subject.title}
<subject>
${subject.content}
</subject>`;
}

/**
 * JSON Schema for the structured tool call a model-backed judge must return.
 *
 * `additionalProperties: false` on every object is required by strict tool use,
 * which is what guarantees the input validates rather than merely tending to.
 */
export const ASSESSMENT_SCHEMA: {
  type: "object";
  properties: Record<string, unknown>;
  required: string[];
  additionalProperties: boolean;
} = {
  type: "object",
  properties: {
    assessments: {
      type: "array",
      description: "One entry per rubric criterion.",
      items: {
        type: "object",
        properties: {
          criterionId: { type: "string", description: "Must match a rubric criterion id." },
          score: { type: "number", description: "0 to 1." },
          confidence: { type: "number", description: "0 to 1." },
          reasoning: {
            type: "string",
            description: "Why this score, in one or two sentences.",
          },
          evidence: {
            type: "array",
            description:
              "Specific, checkable citations. Empty is allowed and honest when nothing was found; do not invent one.",
            items: {
              type: "object",
              properties: {
                locator: {
                  type: "string",
                  description: "Where in the subject, e.g. a file:line or section.",
                },
                observation: {
                  type: "string",
                  description: "What is actually there, checkable against the subject.",
                },
              },
              required: ["locator", "observation"],
              additionalProperties: false,
            },
          },
        },
        required: ["criterionId", "score", "confidence", "reasoning", "evidence"],
        additionalProperties: false,
      },
    },
  },
  required: ["assessments"],
  additionalProperties: false,
};
