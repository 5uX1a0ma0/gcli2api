import assert from "node:assert/strict";
import test from "node:test";
import { runBatchVerification } from "../frontend/src/utils/batchVerification.js";

test("batch verification bounds concurrency and reports every selected account", async () => {
  const emails = Array.from({ length: 13 }, (_, index) => `account-${index}@example.test`);
  let active = 0;
  let peak = 0;
  const completed = [];
  const results = await runBatchVerification(emails, {
    verify: async (email) => {
      peak = Math.max(peak, ++active);
      await new Promise((resolve) => setTimeout(resolve, 2));
      active--;
      return { success: true, email, project_id: `project-${email}`, subscription_tier: "pro" };
    },
    onResult: (result) => completed.push(result.email),
  });
  assert.equal(peak, 4);
  assert.deepEqual(results.map((result) => result.email), emails);
  assert.equal(new Set(completed).size, emails.length);
  assert.ok(results.every((result) => result.success));
});

test("HTTP errors and unsuccessful responses do not interrupt other accounts", async () => {
  const results = await runBatchVerification(["http-error", "invalid", "good"], {
    verify: async (email) => {
      if (email === "http-error") throw { response: { status: 500, data: { detail: { reason: "upstream unavailable" } } } };
      if (email === "invalid") return { success: false, error: "project lookup failed" };
      return { success: true, project_id: "project", subscription_tier: "ultra" };
    },
  });
  assert.deepEqual(results.map((result) => result.success), [false, false, true]);
  assert.equal(results[0].status_code, 500);
  assert.equal(results[0].message, '{"reason":"upstream unavailable"}');
  assert.equal(results[1].message, "project lookup failed");
  assert.equal(results[2].subscription_tier, "ultra");
});

test("stopping prevents further checks while keeping results of active checks", async () => {
  let stopped = false;
  const started = [];
  const results = await runBatchVerification(Array.from({ length: 20 }, (_, index) => index), {
    shouldStop: () => stopped,
    verify: async (email) => {
      started.push(email);
      await new Promise((resolve) => setTimeout(resolve, 2));
      return { success: true };
    },
    onResult: () => { stopped = true; },
  });
  assert.deepEqual(started, [0, 1, 2, 3]);
  assert.equal(results.length, 4);
  assert.ok(results.every((result) => result.success));
});

test("model refresh keeps empty successes and capability metadata", async () => {
  const results = await runBatchVerification(["empty", "paid"], {
    verify: async (email) => ({
      success: true,
      supported_models: email === "empty" ? [] : ["claude-opus-5-5-high"],
      models_checked_at: 1234,
      paid: email === "paid" ? true : null,
    }),
  });
  assert.deepEqual(results[0].supported_models, []);
  assert.equal(results[0].success, true);
  assert.equal(results[0].models_checked_at, 1234);
  assert.equal(results[0].paid, null);
  assert.equal(results[1].supported_models.length, 1);
  assert.equal(results[1].paid, true);
});
