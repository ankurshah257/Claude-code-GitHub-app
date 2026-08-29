/**
 * Judge a diff from the command line.
 *
 *   git diff main... | npm run judge
 *   npm run judge -- --rubric code-review < changes.patch
 *
 * A rubric is a piece of writing, and writing needs iteration. Waiting on a
 * webhook to see whether a guidance change helped is slow enough that nobody
 * does it, so the same engine runs here against a local diff.
 */

import { judge as runJudgment } from "./judgment/judge.ts";
import { AnthropicJudge } from "./judgment/providers/anthropic.ts";
import type { Judgment } from "./judgment/types.ts";
import { codeReviewRubric } from "./rubrics/code-review.ts";

const RUBRICS = { "code-review": codeReviewRubric };

async function readStdin(): Promise<string> {
  if (process.stdin.isTTY) return "";
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString("utf8");
}

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main(): Promise<void> {
  const diff = await readStdin();
  if (!diff.trim()) {
    console.error("No diff on stdin. Try: git diff main... | npm run judge");
    process.exit(2);
  }

  const rubricName = arg("rubric") ?? "code-review";
  const rubric = RUBRICS[rubricName as keyof typeof RUBRICS];
  if (!rubric) {
    console.error(
      `Unknown rubric "${rubricName}". Available: ${Object.keys(RUBRICS).join(", ")}`,
    );
    process.exit(2);
  }

  const judgment = await runJudgment(
    {
      id: `local:${new Date().toISOString()}`,
      title: arg("title") ?? "Local diff",
      content: diff,
      context: { source: "local working tree" },
    },
    rubric,
    new AnthropicJudge(),
  );

  if (process.argv.includes("--json")) {
    console.log(JSON.stringify(judgment, null, 2));
  } else {
    console.log(format(judgment));
  }

  // Exit non-zero on a blocking verdict so this composes into a pre-push hook
  // or a CI step. ABSTAIN is not a failure - it is the system declining to say.
  process.exit(judgment.verdict === "PASS" || judgment.verdict === "ABSTAIN" ? 0 : 1);
}

function format(j: Judgment): string {
  const out: string[] = [];
  const pct = (n: number) => `${Math.round(n * 100)}%`;

  out.push("");
  out.push(`  ${j.verdict}  ${j.rationale}`);
  out.push("");

  for (const c of j.criteria) {
    if (!c.counted) {
      out.push(`  ${"·".padEnd(6)} ${c.criterion.title}  (${c.discardReason})`);
      continue;
    }
    const mark = c.blocking ? "BLOCK" : pct(c.assessment.score);
    out.push(`  ${mark.padEnd(6)} ${c.criterion.title}`);
    if (c.blocking || c.assessment.score < 0.7) {
      out.push(`         ${c.assessment.reasoning}`);
      for (const e of c.assessment.evidence) {
        out.push(`         - ${e.locator}: ${e.observation}`);
      }
    }
  }

  out.push("");
  out.push(
    `  ${j.rubricId}@${j.rubricVersion} via ${j.judge}  ·  coverage ${pct(j.coverage)}`,
  );
  out.push("");
  return out.join("\n");
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(3);
});
