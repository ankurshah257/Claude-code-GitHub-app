/**
 * HTTP entry point.
 *
 * GitHub expects a webhook response within seconds and retries deliveries that
 * time out; a judgment takes considerably longer than that. So the handler
 * verifies, acknowledges with 202, and does the work afterwards. The tradeoff
 * is that a crash mid-judgment loses that delivery - acceptable here because
 * the next push re-triggers it, and because the alternative (holding the
 * connection open) turns every slow judgment into a duplicate delivery.
 */

import { createServer, type IncomingMessage } from "node:http";
import { JudgmentApp } from "./app.ts";
import { GitHubApp } from "./github/client.ts";
import { parsePullRequestEvent, SignatureError, verifySignature } from "./github/webhook.ts";
import { AnthropicJudge } from "./judgment/providers/anthropic.ts";

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `Missing required environment variable ${name}. See .env.example for the full set.`,
    );
  }
  return value;
}

/** Read the exact bytes GitHub signed. Parsing must not happen before this. */
async function readRawBody(req: IncomingMessage, limit = 25 * 1024 * 1024): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("Request body exceeded the size limit.");
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks);
}

export function start(): void {
  const webhookSecret = required("GITHUB_WEBHOOK_SECRET");

  const app = new JudgmentApp({
    github: new GitHubApp({
      appId: required("GITHUB_APP_ID"),
      privateKey: required("GITHUB_PRIVATE_KEY"),
    }),
    judge: new AnthropicJudge(),
    // Blocking a contributor's PR on a model's reading is opt-in, deliberately.
    allowRequestChanges: process.env.ALLOW_REQUEST_CHANGES === "true",
    onJudgment: (judgment, event) => {
      console.log(
        JSON.stringify({
          at: judgment.judgedAt,
          pr: `${event.owner}/${event.repo}#${event.number}`,
          verdict: judgment.verdict,
          score: judgment.score,
          coverage: judgment.coverage,
          blockedBy: judgment.blockedBy,
          rubric: `${judgment.rubricId}@${judgment.rubricVersion}`,
          judge: judgment.judge,
        }),
      );
    },
  });

  const server = createServer(async (req, res) => {
    if (req.method === "GET" && req.url === "/health") {
      res.writeHead(200).end("ok");
      return;
    }
    if (req.method !== "POST" || req.url !== "/webhook") {
      res.writeHead(404).end();
      return;
    }

    let raw: Buffer;
    try {
      raw = await readRawBody(req);
      verifySignature(
        raw,
        req.headers["x-hub-signature-256"] as string | undefined,
        webhookSecret,
      );
    } catch (err) {
      // Do not distinguish a bad signature from a malformed body in the
      // response: an unauthenticated caller learns nothing either way.
      if (err instanceof SignatureError) console.warn("Rejected delivery:", err.message);
      res.writeHead(401).end();
      return;
    }

    const eventName = req.headers["x-github-event"] as string | undefined;
    const delivery = req.headers["x-github-delivery"] as string | undefined;

    let event;
    try {
      event = parsePullRequestEvent(eventName ?? "", JSON.parse(raw.toString("utf8")));
    } catch {
      res.writeHead(400).end();
      return;
    }

    if (!event) {
      res.writeHead(204).end();
      return;
    }

    // Acknowledge before judging: the work outlives this response.
    res.writeHead(202).end();

    try {
      await app.handlePullRequest(event);
    } catch (err) {
      console.error(
        `Judgment failed for ${event.owner}/${event.repo}#${event.number} (delivery ${delivery}):`,
        err,
      );
    }
  });

  const port = Number(process.env.PORT ?? 3000);
  server.listen(port, () => console.log(`Listening on :${port}`));
}

if (import.meta.url === `file://${process.argv[1]}`) start();
