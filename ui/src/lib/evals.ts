// The Evals page's tabs: the eval run kinds, then the eval specs.
export const EVAL_TABS = ["eval", "features", "gate", "wrong", "specs"] as const;
export type EvalTab = (typeof EVAL_TABS)[number];

export function evalTab(raw: unknown): EvalTab {
  return EVAL_TABS.includes(raw as EvalTab) ? (raw as EvalTab) : "eval";
}
