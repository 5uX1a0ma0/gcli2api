import { AlertCircle, CheckCircle } from "lucide-react";
import { parseErrorCodes } from "../utils/errorCodes.mjs";

export default function ErrorCodesField({ id, value, onChange, placeholder, emptyHint }) {
  const text = Array.isArray(value) ? value.join(", ") : String(value ?? "");
  const result = parseErrorCodes(value);
  return (
    <div className="w-full sm:max-w-md space-y-2">
      <input
        id={id}
        type="text"
        value={text}
        onChange={(event) => onChange(event.target.value)}
        onBlur={() => { if (result.valid) onChange(result.codes); }}
        placeholder={placeholder}
        aria-invalid={!result.valid}
        aria-describedby={`${id}-validation ${id}-note`}
        autoComplete="off"
        spellCheck={false}
        className={`w-full px-3 py-2 bg-dark-800 border rounded-lg text-sm text-white placeholder-gray-500 font-mono ${result.valid ? "border-dark-600" : "border-red-500"}`}
      />
      <p
        id={`${id}-validation`}
        aria-live="polite"
        className={`flex items-start gap-1.5 text-xs leading-relaxed ${result.valid ? "text-emerald-400" : "text-red-300"}`}
      >
        {result.valid ? <CheckCircle size={14} className="shrink-0 mt-0.5" />
          : <AlertCircle size={14} className="shrink-0 mt-0.5" />}
        <span>{!result.valid ? result.error : result.codes.length
          ? `格式有效：${result.codes.join(", ")}（${result.codes.length} 个错误码）`
          : emptyHint}</span>
      </p>
    </div>
  );
}
