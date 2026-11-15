// 与后端一致：规则是非空字符串二元组；名称按字面量匹配，不解释正则。
// Python str.strip 的空白集合（JS trim 会额外删除 BOM，但不删除 NEL 等字符）。
const RULE_WHITESPACE = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu;

export function validatePromptRewriteRules(value) {
  if (!Array.isArray(value)) {
    return { valid: false, error: "身份改写规则必须是数组（每条 [客户端名称, 替换文本]）", rowErrors: [] };
  }
  const rules = [], rowErrors = [];
  value.forEach((rule, index) => {
    if (!Array.isArray(rule) || rule.length !== 2 || rule.some((item) => typeof item !== "string")) {
      rowErrors[index] = `第 ${index + 1} 条身份改写规则必须是字符串二元组`;
      return;
    }
    const cleaned = rule.map((item) => item.replace(RULE_WHITESPACE, ""));
    if (cleaned.some((item) => !item)) {
      rowErrors[index] = `第 ${index + 1} 条规则的客户端名称和替换文本均不能为空`;
      return;
    }
    rules.push(cleaned);
  });
  const error = rowErrors.find(Boolean);
  return { valid: !error, error, rowErrors, rules };
}
