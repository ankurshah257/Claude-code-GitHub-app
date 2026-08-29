# judgment

A system for judgment: it scores a subject against a rubric, then applies a
fixed policy to turn those scores into a verdict. Ships with a GitHub App that
judges pull requests, but the engine has no idea what a pull request is.

## The one idea

Most "AI reviewer" systems ask a model for a verdict. This one never does.

```
      model                          deterministic code
  ┌─────────────┐                   ┌──────────────────┐
  │  Assessment │ ── scores ──────► │      Policy      │ ──► Verdict
  │ per criterion│  + confidence    │ weights, blocking│
  │  + evidence  │                  │ thresholds, cover│
  └─────────────┘                   └──────────────────┘
     fallible                            auditable
```

The model scores individual criteria and reports how sure it is. Deterministic
code decides what those scores add up to. That split buys three things:

- **The verdict is reproducible.** Same assessments, same rubric, same verdict —
  always. There is a test asserting exactly this.
- **You can disagree with a score without the decision being a mystery.** The
  rule that turned 57% into "changes requested" is readable code, not a vibe.
- **A model cannot talk itself into a conclusion.** It never sees the thresholds,
  so it cannot nudge a score to reach a verdict it prefers.

## What makes a judgment trustworthy

Scoring is the easy part. These are the parts that decide whether a verdict
means anything:

**Evidence or it doesn't count.** Every score must cite a checkable location in
the subject. Scores that cite nothing are discarded, not counted. This is the
single most effective guard against a model producing fluent, confident,
entirely ungrounded findings.

**Uncertainty is discarded, not averaged.** A score the judge isn't sure of is
dropped rather than dragging the mean around. Crucially, an unreliable low score
is *not allowed to block* — being unsure is not evidence against the subject.

**Coverage is tracked and enforced.** If too much of the rubric got discarded,
the system returns `ABSTAIN` instead of confidently judging on a fragment. A
verdict over 40% of the rubric is worse than no verdict, because it looks the
same as a real one.

**Abstention is a real outcome.** `ABSTAIN` means "ask again", not a soft no. It
is never a failure and never blocks.

**A blocking criterion outranks a good average.** A change can be well-tested,
clear, and well-scoped and still be one that must not merge. Correctness and
security block on their own, so a strong average cannot bury a fatal flaw.

**Disagreement lowers confidence.** With a `Panel` of several judges, the median
score is reported with confidence *reduced by the spread*. A panel split between
0 and 1 lands at zero confidence and gets discarded — so a coin flip abstains
instead of being reported as 0.5.

## Verdicts

| Verdict | Meaning |
| --- | --- |
| `PASS` | Meets the standard; nothing found against it. |
| `REVISE` | Falls short but is fixable. `blockedBy` names what to fix. |
| `REJECT` | Falls short badly enough that revision is not the right frame. |
| `ABSTAIN` | Not enough reliable signal to judge. Ask again. |

## Try it without GitHub

```bash
npm install
npm test

export ANTHROPIC_API_KEY=...          # or: ant auth login
git diff main... | npm run judge      # add --json for the full record
```

Exits non-zero on a blocking verdict, so it composes into a pre-push hook or a
CI step. `ABSTAIN` exits zero — the system declining to say is not a failure.

## Defining a rubric

A rubric is the actual work. Guidance is written in terms of what is *observable*,
because a judge given abstract standards ("is this good code?") returns its
priors rather than its reading.

```ts
import { defineRubric } from "./src/judgment/rubric.ts";

export const rubric = defineRubric({
  id: "code-review",
  version: "1.0.0",
  preamble: "Judge the change as submitted, not the change you would have written.",
  criteria: [
    {
      id: "correctness",
      title: "The change does what it claims",
      weight: 3,
      blockBelow: 0.5,        // fails on its own, regardless of the average
      guidance: `Cite the specific line and the input that breaks it. If you
cannot name a concrete failing case, this is not a correctness finding.`,
    },
  ],
  policy: { passAt: 0.75, rejectBelow: 0.3, minCoverage: 0.6 },
});
```

`defineRubric` validates at load time and throws on the mistakes that are
otherwise invisible at runtime: duplicate criterion ids, all-zero weights,
guidance-free criteria, thresholds that leave no band for `REVISE`.

Version your rubrics. The version is recorded in every judgment, so a verdict
can always be traced back to the policy that produced it.

## Using the engine directly

```ts
const judgment = await judge(subject, rubric, new AnthropicJudge());
// judgment.verdict, .score, .coverage, .blockedBy, .criteria, .rationale
```

`Judge` is a one-method interface, so a static analyzer or a human panel
implements it as readily as a model. `ScriptedJudge` returns assessments you
hand it — useful for testing the decision layer, and for probing a rubric
("what would it take to get a PASS here?") before trusting it on real subjects.

## Running the GitHub App

```bash
cp .env.example .env    # fill in the four required values
npm run serve
```

Point the App's webhook at `POST /webhook` and subscribe to **Pull requests**.
Required permissions: **Pull requests** read & write, **Contents** read.

Two deliberate defaults:

- **It never submits APPROVE.** A passing judgment means "nothing was found
  against this", which is not a human vouching for the change. An approval
  carries institutional weight this system has not earned.
- **REQUEST_CHANGES is opt-in** (`ALLOW_REQUEST_CHANGES=true`). Blocking a
  contributor's PR on a model's reading is a real cost to them, so a repository
  has to ask for it. The default posts a comment.

Every review discloses what was discarded and which files were never read.
A confident verdict over half the diff is the failure mode worth making visible.

## Layout

```
src/judgment/      the engine — knows nothing about GitHub or code review
  types.ts         Assessment (fallible) vs. Verdict (computed)
  policy.ts        the deterministic decision layer — pure, heavily tested
  rubric.ts        rubric definition + load-time validation
  judge.ts         Judge interface, prompt, output normalization
  panel.ts         multi-judge; disagreement lowers confidence
  providers/       AnthropicJudge (strict tool output), ScriptedJudge
src/rubrics/       rubric definitions
src/github/        webhook verification, PR → Subject, Judgment → review
src/app.ts         the only place the engine and GitHub meet
```

The engine has no GitHub imports and the GitHub layer has no rubric imports, so
either side can be replaced without touching the other.

## Notes

Model output is normalized before it reaches the policy layer — scores are
clamped, non-finite values are treated as unusable, unknown criterion ids are
dropped. Strict tool output guarantees the *shape*, not that the values are sane.

Webhook signatures are verified against raw request bytes with a timing-safe
compare, before any parsing. Verifying a re-serialized object is the classic way
to make signature checking silently useless.

The server acknowledges with `202` and judges afterwards, because GitHub retries
deliveries it considers timed out and a judgment takes far longer than that
budget. A crash mid-judgment loses that delivery; the next push re-triggers it.
