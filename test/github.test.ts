import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { describe, it } from "node:test";
import { buildSubject, type PullRequest } from "../src/github/pr.ts";
import { renderReview } from "../src/github/review.ts";
import {
  parsePullRequestEvent,
  SignatureError,
  verifySignature,
} from "../src/github/webhook.ts";
import { adjudicate } from "../src/judgment/policy.ts";
import { defineRubric } from "../src/judgment/rubric.ts";

const SECRET = "shhh";
const sign = (body: string) =>
  `sha256=${createHmac("sha256", SECRET).update(body).digest("hex")}`;

describe("verifySignature", () => {
  it("accepts a correctly signed body", () => {
    const body = '{"hello":"world"}';
    assert.doesNotThrow(() => verifySignature(body, sign(body), SECRET));
  });

  it("rejects a tampered body", () => {
    const body = '{"hello":"world"}';
    assert.throws(
      () => verifySignature('{"hello":"evil"}', sign(body), SECRET),
      SignatureError,
    );
  });

  it("rejects a signature made with the wrong secret", () => {
    const body = '{"a":1}';
    const wrong = `sha256=${createHmac("sha256", "other").update(body).digest("hex")}`;
    assert.throws(() => verifySignature(body, wrong, SECRET), SignatureError);
  });

  it("rejects a missing signature header", () => {
    assert.throws(() => verifySignature("{}", undefined, SECRET), SignatureError);
  });

  it("rejects a signature of the wrong length without throwing from timingSafeEqual", () => {
    assert.throws(() => verifySignature("{}", "sha256=abc", SECRET), SignatureError);
  });

  it("refuses to verify when no secret is configured", () => {
    const body = "{}";
    assert.throws(() => verifySignature(body, sign(body), ""), SignatureError);
  });

  it("verifies raw bytes, so key reordering does not pass", () => {
    // The signature covers exact bytes. A semantically identical but
    // re-serialized body must fail, which is why the server signs raw input.
    const original = '{"a":1,"b":2}';
    assert.throws(
      () => verifySignature('{"b":2,"a":1}', sign(original), SECRET),
      SignatureError,
    );
  });
});

describe("parsePullRequestEvent", () => {
  const payload = {
    action: "opened",
    installation: { id: 42 },
    repository: { name: "repo", owner: { login: "owner" } },
    pull_request: {
      number: 7,
      title: "Add retries",
      body: "why",
      draft: false,
      user: { login: "alice" },
      base: { ref: "main" },
      head: { sha: "abc123" },
    },
  };

  it("extracts the fields the app needs", () => {
    const e = parsePullRequestEvent("pull_request", payload)!;
    assert.equal(e.owner, "owner");
    assert.equal(e.repo, "repo");
    assert.equal(e.number, 7);
    assert.equal(e.installationId, 42);
    assert.equal(e.headSha, "abc123");
  });

  it("ignores events that change no code", () => {
    assert.equal(parsePullRequestEvent("pull_request", { ...payload, action: "labeled" }), null);
  });

  it("ignores drafts but judges them once marked ready", () => {
    const draft = { ...payload, pull_request: { ...payload.pull_request, draft: true } };
    assert.equal(parsePullRequestEvent("pull_request", draft), null);
    const ready = { ...payload, action: "ready_for_review" };
    assert.ok(parsePullRequestEvent("pull_request", ready));
  });

  it("ignores other event types and malformed payloads", () => {
    assert.equal(parsePullRequestEvent("push", payload), null);
    assert.equal(parsePullRequestEvent("pull_request", {}), null);
    assert.equal(parsePullRequestEvent("pull_request", null), null);
  });

  it("ignores a payload with no installation to authenticate as", () => {
    const { installation, ...rest } = payload;
    assert.equal(parsePullRequestEvent("pull_request", rest), null);
  });
});

describe("buildSubject", () => {
  const pr = (files: PullRequest["files"]): PullRequest => ({
    owner: "o",
    repo: "r",
    number: 1,
    title: "t",
    body: "b",
    author: "a",
    baseRef: "main",
    headSha: "sha",
    files,
  });

  it("puts the diff in the subject and intent in the context", () => {
    const { subject } = buildSubject(
      pr([{ filename: "a.ts", status: "modified", additions: 1, deletions: 0, patch: "@@ +x" }]),
    );
    assert.match(subject.content, /a\.ts/);
    assert.equal(subject.context?.description, "b");
    // The description must not be part of what gets scored.
    assert.doesNotMatch(subject.content, /^b$/m);
  });

  it("excludes lockfiles and reports them as omitted", () => {
    const { subject, omitted } = buildSubject(
      pr([
        { filename: "package-lock.json", status: "modified", additions: 900, deletions: 4, patch: "x" },
        { filename: "a.ts", status: "modified", additions: 1, deletions: 0, patch: "@@ real" },
      ]),
    );
    assert.ok(omitted.some((o) => o.filename === "package-lock.json"));
    assert.doesNotMatch(subject.content, /@@ x/);
    assert.match(subject.content, /NOT INCLUDED/);
  });

  it("reports binary files rather than silently skipping them", () => {
    const { omitted } = buildSubject(
      pr([{ filename: "logo.png", status: "added", additions: 0, deletions: 0 }]),
    );
    assert.equal(omitted[0]!.reason, "binary or no patch available");
  });

  it("drops whole files when over budget instead of truncating a hunk", () => {
    const big = "x".repeat(5000);
    const { subject, omitted } = buildSubject(
      pr([
        { filename: "big.ts", status: "modified", additions: 1, deletions: 0, patch: big },
        { filename: "small.ts", status: "modified", additions: 1, deletions: 0, patch: "@@ small" },
      ]),
      { maxDiffChars: 1000 },
    );
    // The small file fits and survives; the large one is dropped and named.
    assert.match(subject.content, /small\.ts/);
    assert.ok(omitted.some((o) => o.filename === "big.ts" && /budget/.test(o.reason)));
  });

  it("identifies the subject by head sha so a new push is a new judgment", () => {
    const { subject } = buildSubject(pr([]));
    assert.equal(subject.id, "o/r#1@sha");
  });
});

describe("renderReview", () => {
  const rubric = defineRubric({
    id: "r",
    version: "1",
    preamble: "p",
    criteria: [
      { id: "correctness", title: "Correctness", guidance: "g", weight: 2, blockBelow: 0.5 },
      { id: "style", title: "Style", guidance: "g", weight: 1 },
    ],
  });

  const judgmentWith = (correctness: number) =>
    adjudicate({
      subjectId: "s",
      rubric,
      judge: "test",
      assessments: [
        {
          criterionId: "correctness",
          score: correctness,
          confidence: 0.9,
          reasoning: "the loop is off by one",
          evidence: [{ locator: "a.ts:10", observation: "i <= len" }],
        },
        {
          criterionId: "style",
          score: 0.9,
          confidence: 0.9,
          reasoning: "fine",
          evidence: [{ locator: "a.ts:1", observation: "consistent" }],
        },
      ],
    });

  it("never submits APPROVE, even on a passing judgment", () => {
    const r = renderReview(judgmentWith(1), { allowRequestChanges: true });
    assert.equal(r.event, "COMMENT");
  });

  it("only blocks when the repository opted in", () => {
    const blocking = judgmentWith(0.1);
    assert.equal(renderReview(blocking).event, "COMMENT");
    assert.equal(renderReview(blocking, { allowRequestChanges: true }).event, "REQUEST_CHANGES");
  });

  it("includes the cited evidence for a blocking finding", () => {
    const { body } = renderReview(judgmentWith(0.2));
    assert.match(body, /a\.ts:10/);
    assert.match(body, /off by one/);
  });

  it("surfaces discarded criteria so coverage gaps are visible", () => {
    const j = adjudicate({
      subjectId: "s",
      rubric,
      judge: "test",
      assessments: [
        {
          criterionId: "correctness",
          score: 0.9,
          confidence: 0.9,
          reasoning: "ok",
          evidence: [{ locator: "a.ts:1", observation: "o" }],
        },
        { criterionId: "style", score: 0.9, confidence: 0.9, reasoning: "ok", evidence: [] },
      ],
    });
    const { body } = renderReview(j);
    assert.match(body, /Not counted/);
    assert.match(body, /cited no specific evidence/);
  });

  it("names the files it did not read", () => {
    const { body } = renderReview(judgmentWith(1), {
      omitted: [{ filename: "big.bin", reason: "binary or no patch available" }],
    });
    assert.match(body, /Not reviewed/);
    assert.match(body, /big\.bin/);
  });
});
