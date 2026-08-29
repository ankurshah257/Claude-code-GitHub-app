/**
 * The application: webhook event in, pull request review out.
 *
 * This module is the only place the judgment core and GitHub meet. The core
 * knows nothing about pull requests, and the GitHub layer knows nothing about
 * rubrics; the wiring lives here so either can be replaced without touching
 * the other.
 */

import { GitHubApp } from "./github/client.ts";
import { buildSubject } from "./github/pr.ts";
import { renderReview } from "./github/review.ts";
import type { PullRequestEvent } from "./github/webhook.ts";
import { judge as runJudgment } from "./judgment/judge.ts";
import type { Judge } from "./judgment/judge.ts";
import type { Judgment, Rubric } from "./judgment/types.ts";
import { codeReviewRubric } from "./rubrics/code-review.ts";

export interface AppOptions {
  github: GitHubApp;
  judge: Judge;
  rubric?: Rubric;
  /** Let a blocking verdict submit REQUEST_CHANGES. Off by default. */
  allowRequestChanges?: boolean;
  /** Called with every completed judgment - use it to persist the audit trail. */
  onJudgment?: (judgment: Judgment, event: PullRequestEvent) => void | Promise<void>;
}

export class JudgmentApp {
  #github: GitHubApp;
  #judge: Judge;
  #rubric: Rubric;
  #allowRequestChanges: boolean;
  #onJudgment: AppOptions["onJudgment"];

  constructor(opts: AppOptions) {
    this.#github = opts.github;
    this.#judge = opts.judge;
    this.#rubric = opts.rubric ?? codeReviewRubric;
    this.#allowRequestChanges = opts.allowRequestChanges ?? false;
    this.#onJudgment = opts.onJudgment;
  }

  async handlePullRequest(event: PullRequestEvent): Promise<Judgment> {
    const files = await this.#github.listPullRequestFiles(
      event.installationId,
      event.owner,
      event.repo,
      event.number,
    );

    const { subject, omitted } = buildSubject({
      owner: event.owner,
      repo: event.repo,
      number: event.number,
      title: event.title,
      body: event.body,
      author: event.author,
      baseRef: event.baseRef,
      headSha: event.headSha,
      files,
    });

    const judgment = await runJudgment(subject, this.#rubric, this.#judge);
    await this.#onJudgment?.(judgment, event);

    const review = renderReview(judgment, {
      allowRequestChanges: this.#allowRequestChanges,
      omitted,
    });

    await this.#github.createReview(
      event.installationId,
      event.owner,
      event.repo,
      event.number,
      { ...review, commitId: event.headSha },
    );

    return judgment;
  }
}
