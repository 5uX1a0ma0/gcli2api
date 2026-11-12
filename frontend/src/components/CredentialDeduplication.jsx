import { AlertCircle, CheckCircle, Loader2, Mail, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import api from "../api";

export default function CredentialDeduplication({ selectedEmails, totalCount, onBusyChange, onRefresh, onClose }) {
  const [scope, setScope] = useState(selectedEmails.length ? "selected" : "all");
  const [phase, setPhase] = useState("idle");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const controllerRef = useRef(null);
  const panelRef = useRef(null);
  const busy = phase !== "idle";
  const selectionKey = JSON.stringify([...selectedEmails].sort());

  useEffect(() => {
    panelRef.current?.scrollIntoView({ block: "nearest" });
    panelRef.current?.focus({ preventScroll: true });
    return () => controllerRef.current?.abort();
  }, []);

  useEffect(() => {
    if (result?.dry_run && result.scopeEmails
        && JSON.stringify([...result.scopeEmails].sort()) !== selectionKey) {
      setResult(null);
      setNotice("所选凭证已变化，请重新扫描。");
    }
  }, [selectionKey, result]);

  const scan = async () => {
    if (controllerRef.current) return;
    const emails = scope === "selected" ? [...selectedEmails] : null;
    if (emails && !emails.length) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setPhase("scanning");
    onBusyChange(true);
    setError("");
    setNotice("");
    setResult(null);
    try {
      const response = await api.post("/creds/deduplicate", { emails, dry_run: true }, {
        signal: controller.signal, timeout: 0,
      });
      setResult({ ...response.data, scopeEmails: emails });
    } catch (err) {
      if (controller.signal.aborted) setNotice("扫描已停止，没有清理任何凭证。");
      else setError(err.response?.data?.detail || "扫描失败，请重试");
    } finally {
      controllerRef.current = null;
      setPhase("idle");
      onBusyChange(false);
    }
  };

  const apply = async () => {
    if (controllerRef.current || !result?.dry_run || !result.duplicate_count) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setPhase("applying");
    onBusyChange(true);
    setError("");
    try {
      const response = await api.post("/creds/deduplicate", {
        emails: result.scopeEmails, dry_run: false, preview: result.preview,
      }, { signal: controller.signal, timeout: 0 });
      setResult({ ...response.data, scopeEmails: result.scopeEmails });
      await onRefresh();
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err.response?.data?.detail || "去重失败，请重新扫描后重试");
        if (err.response?.status === 409) setResult(null);
      }
    } finally {
      controllerRef.current = null;
      setPhase("idle");
      onBusyChange(false);
    }
  };

  const changeScope = (value) => {
    setScope(value);
    setResult(null);
    setError("");
    setNotice("");
  };

  return (
    <section
      id="credential-deduplication"
      ref={panelRef}
      tabIndex={-1}
      aria-labelledby="credential-deduplication-title"
      aria-busy={busy}
      className="mt-3 pt-3 border-t border-dark-700 space-y-3 focus:outline-none"
    >
      <div className="flex items-center justify-between gap-2">
        <h4 id="credential-deduplication-title" className="flex items-center gap-2 text-sm font-medium text-gray-200">
          <Mail size={16} className="text-purple-400" />按实际邮箱去重
        </h4>
        <button
          type="button"
          aria-label="关闭邮箱去重"
          onClick={onClose}
          disabled={busy}
          className="p-2 rounded-lg text-gray-400 hover:text-white disabled:opacity-40 focus-visible:ring-2 focus-visible:ring-purple-400"
        ><X size={16} /></button>
      </div>
      <p className="text-xs text-gray-400 leading-relaxed">
        查询 Google 返回的真实邮箱，先展示重复组，确认后清理。优先保留已启用的凭证，再优先保留邮箱标签与实际邮箱一致的一份；同等条件保留最早创建的一份。查询失败项会保留。
      </p>
      <fieldset disabled={busy} className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-gray-300">
        <legend className="sr-only">邮箱去重范围</legend>
        <label className="flex items-center gap-2 min-h-8 cursor-pointer">
          <input type="radio" name="deduplication-scope" value="selected" checked={scope === "selected"}
            disabled={!selectedEmails.length} onChange={() => changeScope("selected")} className="accent-purple-600" />
          所选凭证（{selectedEmails.length} 项）
        </label>
        <label className="flex items-center gap-2 min-h-8 cursor-pointer">
          <input type="radio" name="deduplication-scope" value="all" checked={scope === "all"}
            onChange={() => changeScope("all")} className="accent-purple-600" />
          全部凭证（{totalCount} 项，包含其他分页和筛选之外的凭证）
        </label>
      </fieldset>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button" onClick={scan}
          disabled={busy || (scope === "selected" ? !selectedEmails.length : !totalCount)}
          className="btn btn-secondary text-xs px-3 py-1.5 min-h-10 flex items-center gap-1.5 disabled:opacity-40 focus-visible:ring-2 focus-visible:ring-purple-400"
        >
          {phase === "scanning" && <Loader2 size={14} className="animate-spin motion-reduce:animate-none" />}
          {phase === "scanning" ? "正在查询实际邮箱…" : "扫描重复凭证"}
        </button>
        {phase === "scanning" && (
          <button type="button" onClick={() => controllerRef.current?.abort()}
            className="btn btn-secondary text-xs px-3 py-1.5 min-h-10">停止扫描</button>
        )}
        {result?.dry_run && result.duplicate_count > 0 && (
          <button
            type="button" onClick={apply} disabled={busy}
            className="btn btn-danger text-xs px-3 py-1.5 min-h-10 flex items-center gap-1.5 disabled:opacity-40 focus-visible:ring-2 focus-visible:ring-purple-400"
          >
            {phase === "applying" && <Loader2 size={14} className="animate-spin motion-reduce:animate-none" />}
            {phase === "applying" ? "正在核实并清理…" : `确认去重（${result.duplicate_count} 项）`}
          </button>
        )}
      </div>
      {error && <p role="alert" className="flex items-start gap-1.5 text-xs text-red-300 leading-relaxed">
        <AlertCircle size={14} className="shrink-0 mt-0.5" /><span>{error}</span>
      </p>}
      {notice && <p role="status" className="text-xs text-gray-400">{notice}</p>}
      {result && (
        <div className="space-y-3 text-xs">
          <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-gray-300">
            {!result.dry_run && <span className="flex items-center gap-1.5 text-emerald-400 font-medium">
              <CheckCircle size={14} />邮箱去重完成，已清理 {result.removed_count} 项
            </span>}
            <span>已核实 {result.verified_count} / {result.total_count} 项</span>
            <span>重复邮箱 {result.groups.length} 组</span>
            {result.dry_run && <span className={result.duplicate_count ? "text-amber-300" : "text-emerald-400"}>
              {result.duplicate_count ? `可清理 ${result.duplicate_count} 项` : "未发现可清理的重复凭证"}
            </span>}
          </div>
          {result.groups.length > 0 && (
            <ul aria-label="实际邮箱重复组" className="space-y-2 max-h-72 overflow-y-auto">
              {result.groups.map((group) => (
                <li key={group.actual_email} className="rounded-lg bg-dark-800 px-3 py-2 space-y-1 leading-relaxed">
                  <div className="text-gray-200 break-all">实际邮箱：{group.actual_email}</div>
                  <div className="text-emerald-400 break-all">保留：{group.keep_email}</div>
                  <div className="text-red-300 break-all">{result.dry_run ? "待清理" : "已清理"}：{group.duplicate_emails.join("、")}</div>
                </li>
              ))}
            </ul>
          )}
          {result.errors.length > 0 && (
            <details className="text-amber-300">
              <summary className="cursor-pointer min-h-8">{result.errors.length} 项未核实，已保留（查看原因）</summary>
              <ul className="space-y-1 max-h-48 overflow-y-auto text-gray-400">
                {result.errors.map((item) => <li key={item.email} className="break-all">{item.email}：{item.message}</li>)}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
