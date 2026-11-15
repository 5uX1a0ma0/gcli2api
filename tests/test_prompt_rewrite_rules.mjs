import assert from "node:assert/strict";
import test from "node:test";
import { validatePromptRewriteRules } from "../frontend/src/utils/promptRewrite.mjs";

test("identity rule validation rejects incomplete pairs without a partial save", () => {
  for (const value of [null, {}, "[]", ["Codex"], [["Codex"]], [["Codex", null]],
    [["Codex", ""]], [[" ", "Agent"]], [["Codex", "Agent"], ["", "invalid"]]]) {
    assert.equal(validatePromptRewriteRules(value).valid, false);
  }
  assert.equal(validatePromptRewriteRules([]).valid, true);
});

test("literal names and duplicate rules stay intact for backend precedence", () => {
  const value = [["A+B", "\\1"], ["Codex [", "Agent"], ["cOdEx", "Last Agent"]];
  assert.deepEqual(validatePromptRewriteRules(value).rules, value);
});

test("canonical whitespace matches Python str.strip and preserves the source", () => {
  const value = [["\u0085Codex\u001c", "\u3000Agent\u00a0"]];
  assert.deepEqual(validatePromptRewriteRules(value).rules, [["Codex", "Agent"]]);
  assert.deepEqual(value, [["\u0085Codex\u001c", "\u3000Agent\u00a0"]]);
  assert.deepEqual(validatePromptRewriteRules([["\ufeff", "\ufeff"]]).rules, [["\ufeff", "\ufeff"]]);
  assert.equal(validatePromptRewriteRules([["Codex", "\u0085\u001c"]]).valid, false);
});
