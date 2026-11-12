// 编辑时完整校验，禁止 parseInt 将 403abc 等无效输入悄悄当成有效错误码。
export function parseErrorCodes(value) {
  let codes = value;
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return { valid: true, codes: [] };
    if (text.includes("[") || text.includes("]")) {
      try {
        codes = JSON.parse(text);
      } catch {
        return { valid: false, error: "方括号必须成对，数组内用英文逗号分隔，如 [403, 429]" };
      }
      if (Array.isArray(codes) && codes.length
          && !text.slice(1, -1).split(",").every((part) => /^[0-9]{3}$/.test(part.trim()))) {
        return { valid: false, error: "错误码必须是 100–599 的整数，不能包含文字、小数或引号" };
      }
    } else {
      const parts = text.split(",").map((part) => part.trim());
      if (!parts.every((part) => /^[0-9]{3}$/.test(part))) {
        return { valid: false, error: "请用英文逗号分隔完整的三位错误码，如 403, 429；方括号可省略" };
      }
      codes = parts.map(Number);
    }
  }
  if (codes == null) codes = [];
  if (!Array.isArray(codes)) {
    return { valid: false, error: "请输入错误码数组或逗号分隔的错误码，如 [403, 429] 或 403, 429" };
  }
  if (!codes.every((code) => Number.isInteger(code) && code >= 100 && code <= 599)) {
    return { valid: false, error: "错误码必须是 100–599 的整数，不能包含文字、小数或引号" };
  }
  return { valid: true, codes: [...new Set(codes)].sort((a, b) => a - b) };
}
