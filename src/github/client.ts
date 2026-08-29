/**
 * A minimal GitHub App client: just the calls this system makes.
 *
 * Written against `fetch` and `node:crypto` rather than pulling in an SDK,
 * because the surface needed here is three endpoints and a JWT. Installation
 * tokens are cached until shortly before expiry - GitHub issues them for an
 * hour, and minting one per webhook wastes a round trip on every delivery.
 */

import { createSign } from "node:crypto";
import type { ChangedFile } from "./pr.ts";
import type { ReviewEvent } from "./review.ts";

const API = "https://api.github.com";

export interface GitHubAppOptions {
  appId: string;
  /** PEM-encoded RSA private key from the app's settings page. */
  privateKey: string;
  userAgent?: string;
  fetchImpl?: typeof fetch;
}

export class GitHubApp {
  #appId: string;
  #privateKey: string;
  #userAgent: string;
  #fetch: typeof fetch;
  #tokens = new Map<number, { token: string; expiresAt: number }>();

  constructor(opts: GitHubAppOptions) {
    this.#appId = opts.appId;
    // Env vars commonly carry the PEM with literal \n; accept both forms.
    this.#privateKey = opts.privateKey.includes("\\n")
      ? opts.privateKey.replace(/\\n/g, "\n")
      : opts.privateKey;
    this.#userAgent = opts.userAgent ?? "judgment-app";
    this.#fetch = opts.fetchImpl ?? fetch;
  }

  /**
   * Sign a short-lived app JWT (RS256).
   *
   * `iat` is backdated 60s because GitHub rejects tokens whose issue time is in
   * its future, and small clock skew between hosts is normal.
   */
  #appJwt(): string {
    const now = Math.floor(Date.now() / 1000);
    const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
    const payload = b64url(
      JSON.stringify({ iat: now - 60, exp: now + 540, iss: this.#appId }),
    );
    const signer = createSign("RSA-SHA256");
    signer.update(`${header}.${payload}`);
    const signature = signer.sign(this.#privateKey).toString("base64url");
    return `${header}.${payload}.${signature}`;
  }

  async #installationToken(installationId: number): Promise<string> {
    const cached = this.#tokens.get(installationId);
    // 60s of headroom so a token cannot expire between this check and its use.
    if (cached && cached.expiresAt - 60_000 > Date.now()) return cached.token;

    const res = await this.#fetch(
      `${API}/app/installations/${installationId}/access_tokens`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${this.#appJwt()}`,
          accept: "application/vnd.github+json",
          "user-agent": this.#userAgent,
        },
      },
    );
    if (!res.ok) {
      throw new GitHubError(
        `Could not mint an installation token (${res.status}): ${await res.text()}`,
        res.status,
      );
    }
    const body = (await res.json()) as { token: string; expires_at: string };
    this.#tokens.set(installationId, {
      token: body.token,
      expiresAt: Date.parse(body.expires_at),
    });
    return body.token;
  }

  async #request<T>(
    installationId: number,
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const token = await this.#installationToken(installationId);
    const res = await this.#fetch(`${API}${path}`, {
      ...init,
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": this.#userAgent,
        ...(init.headers as Record<string, string> | undefined),
      },
    });
    if (!res.ok) {
      throw new GitHubError(
        `GitHub ${init.method ?? "GET"} ${path} failed (${res.status}): ${await res.text()}`,
        res.status,
      );
    }
    return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
  }

  /**
   * List the files in a PR, following pagination.
   *
   * GitHub caps this at 3000 files; beyond that the diff is not fully
   * retrievable, and `buildSubject` will report the shortfall as omitted files
   * rather than the judgment quietly covering less than it appears to.
   */
  async listPullRequestFiles(
    installationId: number,
    owner: string,
    repo: string,
    number: number,
  ): Promise<ChangedFile[]> {
    const files: ChangedFile[] = [];
    for (let page = 1; page <= 30; page++) {
      const batch = await this.#request<ChangedFile[]>(
        installationId,
        `/repos/${owner}/${repo}/pulls/${number}/files?per_page=100&page=${page}`,
      );
      files.push(...batch);
      if (batch.length < 100) break;
    }
    return files;
  }

  async createReview(
    installationId: number,
    owner: string,
    repo: string,
    number: number,
    review: { event: ReviewEvent; body: string; commitId?: string },
  ): Promise<void> {
    await this.#request(
      installationId,
      `/repos/${owner}/${repo}/pulls/${number}/reviews`,
      {
        method: "POST",
        body: JSON.stringify({
          event: review.event,
          body: review.body,
          ...(review.commitId ? { commit_id: review.commitId } : {}),
        }),
      },
    );
  }
}

export class GitHubError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "GitHubError";
    this.status = status;
  }
}

const b64url = (s: string) => Buffer.from(s).toString("base64url");
