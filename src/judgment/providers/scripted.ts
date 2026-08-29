/**
 * A judge that returns assessments handed to it.
 *
 * Exists so the decision layer can be tested without a model in the loop, and
 * so a rubric's policy can be probed against hypothetical scores ("what would
 * it take to get a PASS here?") before it is trusted on real subjects.
 */

import type { Judge } from "../judge.ts";
import type { Assessment, Rubric, Subject } from "../types.ts";

export class ScriptedJudge implements Judge {
  readonly name: string;
  #script: Assessment[] | ((subject: Subject, rubric: Rubric) => Assessment[]);

  constructor(
    script: Assessment[] | ((subject: Subject, rubric: Rubric) => Assessment[]),
    name = "scripted",
  ) {
    this.#script = script;
    this.name = name;
  }

  async assess(subject: Subject, rubric: Rubric): Promise<Assessment[]> {
    return typeof this.#script === "function"
      ? this.#script(subject, rubric)
      : this.#script;
  }
}
