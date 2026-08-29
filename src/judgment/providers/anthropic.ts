/**
 * A judge backed by the Claude API.
 *
 * Structure is enforced rather than hoped for: the assessment schema is a
 * strict tool, and `tool_choice` forces it, so the model cannot answer in
 * prose. Everything the model returns still passes through `normalize` before
 * it reaches the policy layer — strict output guarantees the shape, not that
 * the values are sane.
 */

import Anthropic from "@anthropic-ai/sdk";
import { ASSESSMENT_SCHEMA, buildPrompt, type Judge } from "../judge.ts";
import type { Assessment, Rubric, Subject } from "../types.ts";

const TOOL_NAME = "record_assessments";

export interface AnthropicJudgeOptions {
  client?: Anthropic;
  model?: string;
  /** Thinking depth. Judging benefits from deliberation, so this defaults high. */
  effort?: "low" | "medium" | "high" | "xhigh" | "max";
  maxTokens?: number;
}

export class AnthropicJudge implements Judge {
  readonly name: string;
  #client: Anthropic;
  #model: string;
  #effort: NonNullable<AnthropicJudgeOptions["effort"]>;
  #maxTokens: number;

  constructor(opts: AnthropicJudgeOptions = {}) {
    // The zero-arg client resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or
    // an `ant auth login` profile, so callers need not plumb credentials.
    this.#client = opts.client ?? new Anthropic();
    this.#model = opts.model ?? "claude-opus-5";
    this.#effort = opts.effort ?? "high";
    this.#maxTokens = opts.maxTokens ?? 16000;
    this.name = `anthropic:${this.#model}`;
  }

  async assess(subject: Subject, rubric: Rubric): Promise<Assessment[]> {
    const response = await this.#client.messages.create({
      model: this.#model,
      max_tokens: this.#maxTokens,
      thinking: { type: "adaptive" },
      output_config: { effort: this.#effort },
      system: [
        {
          type: "text",
          // The rubric is the stable prefix across every subject judged under
          // it, so it is the right cache breakpoint: subjects vary, this does not.
          text: rubricSystemPrompt(rubric),
          cache_control: { type: "ephemeral" },
        },
      ],
      tools: [
        {
          name: TOOL_NAME,
          description:
            "Record one assessment per rubric criterion. This is the only way to report scores.",
          strict: true,
          input_schema: ASSESSMENT_SCHEMA,
        },
      ],
      tool_choice: { type: "tool", name: TOOL_NAME },
      messages: [{ role: "user", content: buildPrompt(subject, rubric) }],
    });

    // A safety decline is not a judgment. Surfacing it as an empty assessment
    // list would silently become an ABSTAIN with no explanation, so throw.
    if (response.stop_reason === "refusal") {
      throw new JudgeRefusedError(
        response.stop_details?.explanation ?? "The model declined to judge this subject.",
      );
    }

    const call = response.content.find(
      (block): block is Anthropic.ToolUseBlock =>
        block.type === "tool_use" && block.name === TOOL_NAME,
    );
    if (!call) {
      throw new JudgeProtocolError(
        `The model returned no ${TOOL_NAME} call (stop_reason: ${response.stop_reason}).`,
      );
    }

    const input = call.input as { assessments?: Assessment[] };
    return input.assessments ?? [];
  }
}

/** Framing that is constant for a given rubric, and therefore cacheable. */
function rubricSystemPrompt(rubric: Rubric): string {
  return `You are an impartial judge applying a fixed rubric (${rubric.id} v${rubric.version}).

You score individual criteria; you do not decide the overall outcome. A separate
deterministic policy combines your scores into a verdict, so do not try to steer
the result by inflating or deflating a score to reach a conclusion you prefer.
Score each criterion on its own terms and report your uncertainty honestly.`;
}

export class JudgeRefusedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "JudgeRefusedError";
  }
}

export class JudgeProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "JudgeProtocolError";
  }
}
