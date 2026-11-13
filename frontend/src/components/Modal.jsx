import { useEffect, useId, useRef } from "react";
import { AlertCircle, CheckCircle, Info, X } from "lucide-react";

/**
 * 通用消息弹窗
 * type: success / error / info
 */
export default function Modal({ type = "info", title, children, onClose }) {
  const titleId = useId();
  const dialogRef = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    dialogRef.current?.focus();
    return () => previous?.isConnected && previous.focus();
  }, []);
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
      if (e.key === "Tab") {
        const controls = [...dialogRef.current.querySelectorAll('button:not(:disabled), input:not(:disabled), [href], [tabindex="0"]')];
        const target = e.shiftKey ? controls.at(-1) : controls[0];
        if (document.activeElement === dialogRef.current ||
            document.activeElement === (e.shiftKey ? controls[0] : controls.at(-1))) {
          e.preventDefault();
          target?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const styles = {
    success: { icon: <CheckCircle size={20} className="text-green-400" />, border: "border-green-500/40", iconBg: "bg-green-500/15" },
    error: { icon: <AlertCircle size={20} className="text-red-400" />, border: "border-red-500/40", iconBg: "bg-red-500/15" },
    info: { icon: <Info size={20} className="text-blue-400" />, border: "border-blue-500/40", iconBg: "bg-blue-500/15" },
  }[type] || {};

  return (
    <div className="fixed inset-0 !m-0 z-50 flex items-center justify-center p-4 bg-black/60" onClick={onClose}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`card max-w-md w-full max-h-[calc(100dvh-2rem)] flex flex-col border ${styles.border} p-0 overflow-hidden`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-dark-700 shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className={`p-1.5 rounded-lg shrink-0 ${styles.iconBg}`}>{styles.icon}</span>
            <h3 id={titleId} className="text-sm font-semibold text-gray-200 break-all">{title}</h3>
          </div>
          <button onClick={onClose} aria-label="关闭弹窗" className="text-gray-400 hover:text-white shrink-0 ml-2 min-h-8 min-w-8 flex items-center justify-center">
            <X size={16} />
          </button>
        </div>
        <div className="px-4 py-3 text-sm text-gray-300 max-h-[60vh] min-h-0 overflow-y-auto break-all">
          {children}
        </div>
        <div className="px-4 py-3 border-t border-dark-700 text-right shrink-0">
          <button onClick={onClose} className="btn btn-secondary text-xs px-4 py-1.5">
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
