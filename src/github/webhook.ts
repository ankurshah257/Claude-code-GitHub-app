/**
 * Webhook receipt: proving a delivery really came from GitHub.
 *
 * Everything here operates on the raw request body. Verifying a re-serialized
 * object instead of the exact bytes GitHub signed is the classic way to make
 * signature checking silently useless - `JSON.parse` then `JSON.stringify` can
 * reorder keys and change escaping, so parse only after the signature holds.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

export class SignatureError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SignatureError";
  }
}

/**
 * Verify GitHub's `X-Hub-Signature-256` header against the raw body.
 *
 * Returns normally on success and throws on any failure, so a caller cannot
 * accidentally treat a falsy return as "verified".
 */
export function verifySignature(
  rawBody: Buffer | string,
  signatureHeader: string | undefined,
  secret: string,
): void {
  if (!secret) throw new SignatureError("No webhook secret configured.");
  if (!signatureHeader) throw new SignatureError("Request has no X-Hub-Signature-256 header.");

  const expected = `sha256=${createHmac("sha256", secret)
    .update(rawBody)
    .digest("hex")}`;

  const a = Buffer.from(signatureHeader);
  const b = Buffer.from(expected);

  // timingSafeEqual throws on length mismatch, which would itself leak length
  // through an exception path, so compare lengths explicitly and constant-time
  // compare only equal-length buffers.
  if (a.length !== b.length) throw new SignatureError("Signature does not match.");
  if (!timingSafeEqual(a, b)) throw new SignatureError("Signature does not match.");
}

/** The subset of a `pull_request` event this system acts on. */
export interface PullRequestEvent {
  action: string;
  installationId: number;
  owner: string;
  repo: string;
  number: number;
  title: string;
  body: string;
  author: string;
  baseRef: string;
  headSha: string;
  draft: boolean;
}

/** Actions worth re-judging. Others (labels, assignment) change no code. */
const JUDGED_ACTIONS = new Set(["opened", "reopened", "synchronize", "ready_for_review"]);

/**
 * Extract a judgable event, or null when this delivery is not one.
 *
 * Returning null rather than throwing keeps the "not for us" path - the large
 * majority of deliveries - from being an error condition.
 */
export function parsePullRequestEvent(
  eventName: string,
  payload: unknown,
): PullRequestEvent | null {
  if (eventName !== "pull_request") return null;

  const p = payload as Record<string, any>;
  if (!p?.pull_request || typeof p.action !== "string") return null;
  if (!JUDGED_ACTIONS.has(p.action)) return null;

  // A draft is explicitly unfinished; judging it is noise. `ready_for_review`
  // is how it comes back.
  if (p.pull_request.draft === true) return null;

  const installationId = p.installation?.id;
  if (typeof installationId !== "number") return null;

  return {
    action: p.action,
    installationId,
    owner: p.repository.owner.login,
    repo: p.repository.name,
    number: p.pull_request.number,
    title: p.pull_request.title ?? "",
    body: p.pull_request.body ?? "",
    author: p.pull_request.user?.login ?? "unknown",
    baseRef: p.pull_request.base?.ref ?? "unknown",
    headSha: p.pull_request.head?.sha ?? "unknown",
    draft: Boolean(p.pull_request.draft),
  };
}
