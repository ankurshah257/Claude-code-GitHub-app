/**
 * Turning a pull request into something judgable.
 *
 * The diff is the subject; the title, description, and author are context. That
 * split is deliberate: a judge given the description as part of the subject
 * tends to score the description's claims rather than the code that is supposed
 * to implement them.
 */

import type { Subject } from "../judgment/types.ts";

export interface PullRequest {
  owner: string;
  repo: string;
  number: number;
  title: string;
  body: string;
  author: string;
  baseRef: string;
  headSha: string;
  files: ChangedFile[];
}

export interface ChangedFile {
  filename: string;
  status: "added" | "modified" | "removed" | "renamed" | string;
  additions: number;
  deletions: number;
  /** Unified diff for this file. Absent for binary files and very large diffs. */
  patch?: string;
}

export interface BuildSubjectOptions {
  /**
   * Cap on diff characters included. A diff that overflows the window silently
   * truncates the evidence the judge can cite, so we drop whole files and say
   * which, rather than cutting mid-hunk and pretending it was complete.
   */
  maxDiffChars?: number;
  /** Paths matching these are excluded as noise; they still count as omitted. */
  exclude?: RegExp[];
}

/** Files that are generated or vendored: real changes, but not ones to judge. */
export const DEFAULT_EXCLUDES: RegExp[] = [
  /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|go\.sum|poetry\.lock)$/,
  /(^|\/)(dist|build|vendor|node_modules)\//,
  /\.(min\.js|min\.css|map|snap)$/,
];

export interface SubjectBuild {
  subject: Subject;
  /** Files left out, with why. Reported to the reader so the gap is visible. */
  omitted: { filename: string; reason: string }[];
}

export function buildSubject(pr: PullRequest, opts: BuildSubjectOptions = {}): SubjectBuild {
  const maxDiffChars = opts.maxDiffChars ?? 180_000;
  const exclude = opts.exclude ?? DEFAULT_EXCLUDES;

  const omitted: SubjectBuild["omitted"] = [];
  const parts: string[] = [];
  let used = 0;

  // Smallest first, so a single enormous file cannot crowd out everything else.
  const ordered = [...pr.files].sort(
    (a, b) => (a.patch?.length ?? 0) - (b.patch?.length ?? 0),
  );

  for (const file of ordered) {
    if (exclude.some((re) => re.test(file.filename))) {
      omitted.push({ filename: file.filename, reason: "generated or vendored" });
      continue;
    }
    if (!file.patch) {
      omitted.push({ filename: file.filename, reason: "binary or no patch available" });
      continue;
    }

    const block = `--- ${file.filename} (${file.status}, +${file.additions} -${file.deletions})\n${file.patch}\n`;
    if (used + block.length > maxDiffChars) {
      omitted.push({ filename: file.filename, reason: "diff budget exhausted" });
      continue;
    }
    parts.push(block);
    used += block.length;
  }

  const content =
    parts.length > 0
      ? parts.join("\n")
      : "(No reviewable diff content: every changed file was excluded, binary, or too large.)";

  const notice =
    omitted.length > 0
      ? `\n\nNOT INCLUDED IN THIS DIFF (do not assume anything about these):\n${omitted
          .map((o) => `- ${o.filename}: ${o.reason}`)
          .join("\n")}`
      : "";

  return {
    subject: {
      id: `${pr.owner}/${pr.repo}#${pr.number}@${pr.headSha}`,
      title: `PR #${pr.number}: ${pr.title}`,
      content: content + notice,
      context: {
        author: pr.author,
        baseRef: pr.baseRef,
        // The description states intent, which the diff is judged against. It
        // is context, not subject: claims here are not evidence of anything.
        description: pr.body.trim() || "(no description provided)",
        filesChanged: String(pr.files.length),
      },
    },
    omitted,
  };
}
