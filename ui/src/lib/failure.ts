// What a red test run says, in a few lines: the failing test's name, the assertion,
// expected and received. Vitest and Playwright both print these; the rest is noise.

const KEY = /(✗|×|FAIL\b|Error:|AssertionError|Expected|Received|^\s*>\s*\d+\s*\|)/;

/** The lines a reviewer needs to judge why the test fails, at most `max`. */
export function failureLines(output: string, max = 14): string[] {
  const lines = output.split("\n").filter((l) => KEY.test(l));
  const unique = [...new Set(lines.map((l) => l.trimEnd()))];
  return unique.slice(0, max);
}
