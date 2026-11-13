const messageText = (value, fallback) => {
  if (value == null || value === "") return fallback;
  return typeof value === "string" ? value : JSON.stringify(value);
};

/** 检验选中凭证；限制并发，保留每项结果，停止后让已发出的检验完成。 */
export async function runBatchVerification(emails, {
  verify,
  onResult,
  shouldStop = () => false,
  concurrency = 4,
}) {
  const results = new Array(emails.length);
  let next = 0;
  const worker = async () => {
    while (!shouldStop()) {
      const index = next++;
      if (index >= emails.length) return;
      const email = emails[index];
      let result;
      try {
        const data = await verify(email);
        result = {
          email,
          success: data.success === true,
          project_id: data.project_id || "",
          subscription_tier: data.subscription_tier || "",
          supported_models: data.supported_models,
          models_checked_at: data.models_checked_at,
          paid: data.paid,
          message: messageText(data.message || data.error, data.success ? "检验成功" : "检验失败"),
        };
      } catch (error) {
        const data = error.response?.data || {};
        result = {
          email,
          success: false,
          status_code: error.response?.status,
          message: messageText(data.detail || data.error || data.message || error.message, "检验失败"),
        };
      }
      results[index] = result;
      onResult?.(result);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, emails.length) }, worker));
  return results.filter(Boolean);
}
