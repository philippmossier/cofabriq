import type { ProofNote } from "@/api/generated";

/** How the fix is proven, in a few words (the summary line). */
export const PROOF_SHORT: Record<ProofNote["kind"], string> = {
  unit: "unit test",
  browser: "browser test",
  proxy: "stand-in measure",
  none: "nothing: no test can",
};

type Facts = {
  size: string | null | undefined;
  files: unknown[];
  tests: unknown[];
  proof?: ProofNote | null;
  assumptions: string[];
  open_questions: string[];
};

const n = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/** A spec at a glance: "size S · 1 file to change · tests in 1 file ·
 * proven by a unit test · 3 things to check". */
export function specFacts(s: Facts): string[] {
  const toCheck = s.assumptions.length + s.open_questions.length;
  return [
    `size ${s.size ?? "?"}`,
    n(s.files.length, "file to change", "files to change"),
    `tests in ${n(s.tests.length, "file", "files")}`,
    ...(s.proof ? [`proven by a ${PROOF_SHORT[s.proof.kind]}`] : []),
    n(toCheck, "thing to check", "things to check"),
  ];
}
