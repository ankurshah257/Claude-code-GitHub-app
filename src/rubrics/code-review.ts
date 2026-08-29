/**
 * The default rubric for judging a pull request.
 *
 * Guidance is written in terms of what is observable in a diff, because a judge
 * given abstract standards ("is this good code?") returns its priors rather
 * than its reading. Correctness and security can block on their own: a change
 * can be well-tested, clear, and appropriately scoped and still be one that
 * must not merge.
 */

import { defineRubric } from "../judgment/rubric.ts";

export const codeReviewRubric = defineRubric({
  id: "code-review",
  version: "1.0.0",
  preamble: `You are reviewing a pull request diff. Judge the change as submitted,
not the codebase around it, and not the change you would have written instead.

Only lines the diff touches are in scope. Pre-existing problems in surrounding
context are not this change's fault; say so rather than scoring them against it.
A small, correct change that does exactly what it claims is a good change - do
not penalize it for what it did not attempt.`,

  criteria: [
    {
      id: "correctness",
      title: "The change does what it claims, without introducing defects",
      weight: 3,
      blockBelow: 0.5,
      guidance: `Look for logic that is wrong on some input the code will actually see:
off-by-one bounds, inverted conditions, unhandled null/undefined, mishandled
errors, race conditions, resource leaks, changed behavior the description does
not mention. Cite the specific line and the input that breaks it. If you cannot
name a concrete failing case, this is not a correctness finding - score it as
met and raise your concern under a different criterion.`,
    },
    {
      id: "security",
      title: "The change introduces no new vulnerability",
      weight: 3,
      blockBelow: 0.6,
      guidance: `Consider only what this diff adds or exposes: injection via
unsanitized input reaching an interpreter or query, secrets committed or logged,
authentication or authorization checks removed or bypassed, unsafe deserialization,
path traversal, SSRF. Judge exploitability, not resemblance to a bad pattern -
a parameterized query that merely contains a string concatenation is not an
injection. If nothing in the diff touches a security boundary, score this met
with high confidence and say that plainly.`,
    },
    {
      id: "tests",
      title: "Behavior changes are covered by tests",
      weight: 2,
      guidance: `Does the diff add or update tests for the behavior it changes,
and would those tests actually fail if the change were reverted? A test that
asserts on a mock of the thing under test does not count. Pure refactors with
unchanged behavior and existing coverage need no new tests - score those met.
Weigh coverage of the risky path more than raw test count.`,
    },
    {
      id: "clarity",
      title: "The change is readable by someone who did not write it",
      weight: 1,
      guidance: `Judge naming, control-flow complexity, and whether comments
explain why rather than restate what. Consistency with the conventions already
visible in the surrounding file counts for more than any general style rule.
This is the lowest-weight criterion: do not let a stylistic preference drive
the overall verdict.`,
    },
    {
      id: "scope",
      title: "The change is as small as its stated purpose allows",
      weight: 1,
      guidance: `Flag unrelated edits bundled in - drive-by reformatting, an
opportunistic refactor of untouched code, a second feature. These make review
harder and revert riskier. Do not flag changes genuinely required by the stated
purpose, and do not flag a large diff that is large because the task is.`,
    },
  ],

  // Tuned so that a change is blocked by a specific, cited defect rather than
  // by a merely mediocre average: passAt is reachable without being perfect,
  // and correctness/security block on their own.
  policy: {
    passAt: 0.75,
    rejectBelow: 0.3,
    minConfidence: 0.5,
    minCoverage: 0.6,
    requireEvidence: true,
  },
});
