import assert from "node:assert/strict";
import test from "node:test";
import { parseErrorCodes } from "../frontend/src/utils/errorCodes.mjs";

test("plain and bracketed codes normalize duplicates without truncating input", () => {
  for (const input of ["429,403,429", " [429, 403, 429] ", [429, 403, 429]]) {
    assert.deepEqual(parseErrorCodes(input), { valid: true, codes: [403, 429] });
  }
  for (const input of ["", "[]", []]) assert.deepEqual(parseErrorCodes(input), { valid: true, codes: [] });
  assert.deepEqual(parseErrorCodes("100,599").codes, [100, 599]);
});

test("incomplete, quoted, decimal, out-of-range and non-numeric codes are rejected", () => {
  for (const input of ["403abc,429", "403,", ",403", "403,,429", "403 429", "403，429",
    "[403,429", "403,429]", "[403,]", '["403"]', "[403.5]", "[403.0]", "[4e2]",
    "[403, false]", "[[403]]", "[null]", "99,429", "600", "0x193", "403.0",
    [true], ["403"], {}, 403]) {
    const result = parseErrorCodes(input);
    assert.equal(result.valid, false, JSON.stringify(input));
    assert(result.error);
    assert.equal(result.codes, undefined);
  }
});
