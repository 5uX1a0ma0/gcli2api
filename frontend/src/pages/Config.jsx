import { Loader2, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import Modal from "../components/Modal";
import Select from "../components/Select";
import ErrorCodesField from "../components/ErrorCodesField";
import api from "../api";
import { parseErrorCodes } from "../utils/errorCodes.mjs";
import { validatePromptRewriteRules } from "../utils/promptRewrite.mjs";

// 多行文本（IP 白名单等，每行一条）：编辑期间保留原始文本（允许末尾空行与连续回车），
// 仅在按键时把清洗后的数组回写配置（保证保存时取到最新值），失焦时再归一化显示。
// 原实现在每次按键即时 split + 过滤空行，末尾按回车产生的新空行被立即移除，
// 导致"最后一行按回车没反应"、无法换行新增词条。
function LinesField({ value, onChange, placeholder }) {
  const toText = (v) => (Array.isArray(v) ? v.join("\n") : v ?? "");
  const [text, setText] = useState(() => toText(value));
  const focusedRef = useRef(false);

  // 仅在非编辑态（失焦）时同步外部配置变化，避免编辑过程中被即时覆盖
  useEffect(() => {
    if (!focusedRef.current) setText(toText(value));
  }, [value]);

  const commitText = (raw) => {
    const lines = raw
      .split(/\r?\n/)
      .map((w) => w.replace(/\s+$/, ""))
      .filter((w) => w.trim() !== "");
    onChange(lines);
  };

  return (
    <textarea
      value={text}
      onFocus={() => {
        focusedRef.current = true;
      }}
      onChange={(e) => {
        const raw = e.target.value;
        setText(raw);
        commitText(raw);
      }}
      onBlur={() => {
        focusedRef.current = false;
        commitText(text);
      }}
      placeholder={placeholder}
      rows={6}
      className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500 font-mono"
    />
  );
}

const CONFIG_GROUPS = [
  {
    title: "服务器配置",
    fields: [
      { key: "host", label: "服务器主机地址", type: "text", note: "服务器监听的主机地址，0.0.0.0表示监听所有接口（重启生效）" },
      { key: "port", label: "服务器端口", type: "number", note: "服务器监听的端口号（重启生效）" },
      { key: "api_password", label: "API 访问密码", type: "text", note: "聊天API访问密码，用于 OpenAI / Gemini / Claude 端点认证" },
      { key: "panel_password", label: "控制面板密码", type: "text", note: "控制面板访问密码" },
    ],
  },
  {
    title: "IP 白名单",
    fields: [
      { key: "ip_whitelist_enabled", label: "启用 IP 白名单", type: "bool", note: "开启后仅白名单内的 IP 可访问面板和 API，其余 IP 直接 403。留空列表或关闭开关则不限制。保存后立即生效（热更新）" },
      { key: "ip_whitelist", label: "IP 白名单列表", type: "textarea", showIf: (cfg) => !!cfg.ip_whitelist_enabled, note: "每行一条，支持单个 IP、CIDR 网段、起止范围（- 或 ~ 分隔），IPv4 / IPv6 均可", placeholder: "例如：\n127.0.0.1\n192.168.1.0/24\n10.0.0.1-10.0.0.100\n::1\n2001:db8::/32" },
    ],
  },
  {
    title: "基础配置",
    fields: [
      { key: "credentials_dir", label: "凭证目录路径", type: "text", note: "存储认证文件的目录路径" },
      { key: "proxy", label: "代理设置", type: "text", note: "支持 http/https/socks5/socks5h，可带账号密码（user:pass@host:port），留空不使用（重启生效）", placeholder: "http://user:pass@proxy:port 或 socks5h://proxy:port" },
    ],
  },
  {
    title: "端点配置",
    fields: [
      { key: "oauth_proxy_url", label: "OAuth Endpoint", type: "text", note: "Google OAuth2 端点，用于 token 获取和刷新" },
      { key: "googleapis_proxy_url", label: "Google APIs Endpoint", type: "text", note: "Google APIs 端点" },
      { key: "antigravity_api_url", label: "Antigravity API Endpoint", type: "text", note: "Antigravity API 端点" },
    ],
  },
  {
    title: "自动封禁配置",
    fields: [
      { key: "auto_ban_enabled", label: "启用自动封禁", type: "bool", note: "遇到指定错误码时自动禁用凭证" },
      { key: "auto_ban_error_codes", label: "自动封禁错误码", type: "error_codes", placeholder: "403, 400 或 [403, 400]", emptyHint: "格式有效：空列表，不按状态码自动禁用凭证", note: "支持 403, 400 或 [403, 400]，方括号可省略；使用英文逗号，范围 100–599。留空不按状态码自动禁用。" },
    ],
  },
  {
    title: "重试配置",
    fields: [
      { key: "retry_429_enabled", label: "启用错误重试", type: "bool", note: "遇到下方指定错误码时自动重试，与自动封禁开关独立" },
      { key: "retry_error_codes", label: "自动重试错误码", type: "error_codes", placeholder: "400, 403, 429, 500, 503", emptyHint: "格式有效：空列表，不按状态码自动重试", note: "支持 429, 503 或 [429, 503]，方括号可省略；使用英文逗号，范围 100–599。默认 400, 403, 429, 500, 503；留空不按状态码重试。" },
      { key: "retry_429_max_retries", label: "最大重试次数", type: "number", note: "遇到错误时的最大重试次数" },
      { key: "retry_429_interval", label: "重试间隔（秒）", type: "number", note: "两次重试间的等待秒数，允许 0（立即重试），范围 0-3600" },
    ],
  },
  {
    title: "凭证选择模式",
    fields: [
      {
        key: "free_account_priority_enabled",
        label: "免费账户优先",
        type: "bool",
        note: "默认开启。Non-paid 账户支持的模型优先使用这些账户；没有可用免费账户时回退其他账户。按模型能力识别，与 Tier 无关；会话缓存和粘性在同一优先级内保持。",
      },
      {
        key: "credential_select_mode",
        label: "选择模式",
        type: "select",
        note: "决定每次 API 请求如何挑选凭证",
        options: [
          { value: "random", label: "完全随机" },
          { value: "cache", label: "缓存匹配（推荐）" },
          { value: "sticky", label: "凭证不变直到 CD" },
        ],
        descriptions: {
          random: "完全随机负载均衡：每次请求从可用凭证中随机挑选一个，最均匀地分摊负载，但相同会话可能命中不同凭证，无法利用上游 prompt cache。",
          cache: "缓存匹配（默认推荐）：以\"模型 + system 指令（或首条 user 消息）\"哈希出会话 key，相同会话始终复用同一凭证——命中 Google 上游的 prompt cache，长对话更快、更省 token。凭证进入 CD 后自动切换并重新绑定。建议搭配 Redis（REDIS_URL）实现多实例共享。",
          sticky: "凭证粘性：所有请求锁定同一个凭证，直到该凭证进入冷却（CD）或被禁用才切换到下一个。适合最大化单凭证的缓存命中，但负载不均——单凭证扛满全量请求，其余凭证闲置。",
        },
      },
    ],
  },
  {
    title: "响应配置",
    fields: [
      { key: "return_thoughts_to_frontend", label: "返回思维链到前端", type: "bool", note: "启用后思维链会在响应中返回；禁用后过滤掉" },
    ],
  },
  {
    title: "抗截断配置",
    fields: [
      { key: "anti_truncation_max_attempts", label: "抗截断最大重试次数", type: "number", note: "检测到输出截断时的最大续传尝试次数（1-10）。使用'抗截断-'前缀的模型生效" },
    ],
  },
  {
    title: "提示词身份改写",
    id: "prompt-rewrite-settings",
    noteClassName: "text-gray-400",
    fields: [
      {
        key: "prompt_rewrite_enabled", label: "启用提示词身份改写", type: "bool",
        note: "关键词替换覆盖系统、开发者、用户、助手和工具结果的全部正文，以及工具描述。开启默认词表时，还会清理系统指令中的计费头、归一化明确身份声明并折叠文首重复身份。旧词条已迁移为名称映射，旧开关状态继续沿用。",
      },
      {
        key: "prompt_rewrite_use_default_keywords", label: "使用内置默认词表", type: "bool",
        note: "包含 Codex、Claude Code、Claude Agent SDK、Cursor、DeepSeek Harness 等客户端名称，默认替换为 Antigravity。内置规则保护代码、路径、URL 和标识；自定义规则优先，空自定义列表仍可使用内置词表。",
      },
      {
        key: "prompt_rewrite_rules", label: "自定义名称映射", type: "rules",
        columnLabels: ["客户端名称", "替换文本"],
        columnPlaceholders: ["例如 Codex", "留空表示删除"],
        emptyHint: "暂无自定义规则；默认词表开启时仍会生效。",
        note: "按字面量查找替换全部消息正文，不区分大小写，含代码、路径和名称子串。较长名称优先，同名规则以最后一条为准。名称不能为空，替换文本留空即删除命中内容；保存后立即生效。",
      },
      {
        key: "prompt_rewrite_cloak_enabled", label: "暴力零宽打散", type: "bool",
        note: "独立开关，无需关键词词表。在系统 / 开发者指令和工具描述中，将长度至少 5 个字母的英文词在第 3 个字母后插入零宽空格（U+200B），已打散的词不会重复处理。此模式也会打散这些文本中的代码、路径和 URL。",
      },
    ],
  },
  {
    title: "模型名复写",
    fields: [
      { key: "model_rewrite_enabled", label: "启用模型名复写", type: "bool", note: "开启后按规则匹配请求中的模型名（支持正则），命中则替换为规则指定的模型名。常用于把客户端固定模型名映射到实际可用的上游模型。保存后立即生效（热更新）" },
      {
        key: "model_rewrite_rules",
        label: "复写规则",
        type: "rules",
        showIf: (cfg) => !!cfg.model_rewrite_enabled,
        note: "每行一条规则：匹配正则 + 替换模型名。自上而下首条命中即生效，替换支持正则反向引用（如 \\1）",
      },
    ],
  },
  {
    title: "请求日志",
    fields: [
      { key: "usage_log_enabled", label: "启用请求日志", type: "bool", note: "记录每次 API 请求（模型、凭证、token 用量、耗时、状态）到数据库，在\"请求日志\"页面查看。日志不自动清理，可在该页面手动清理或清空" },
      { key: "request_log_enabled", label: "启用请求体详细日志", type: "bool", note: "开启后，当请求命中下方错误码时，把发往上游的完整请求头（已脱敏）与请求体保存为 JSON 文件（data/request_logs/）。主要用于请求体错误时排查 debug；清理/清空日志时会同步删除这些文件" },
      { key: "request_log_error_codes", label: "详细日志错误码", type: "error_codes", note: "仅当最终响应状态码命中此列表时记录详细日志。支持 400, 429 或 [400, 429]，方括号可省略；使用英文逗号，范围 100–599。留空默认只记录 400。", emptyHint: "格式有效：留空使用默认错误码 400", placeholder: "400, 429 或 [400, 429]" },
    ],
  },
];

const ERROR_CODE_FIELDS = CONFIG_GROUPS.flatMap((group) => group.fields)
  .filter((field) => field.type === "error_codes");

export default function Config() {
  const [config, setConfig] = useState({});
  const [envLocked, setEnvLocked] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);
  const invalidCodeFields = ERROR_CODE_FIELDS.filter((field) =>
    !envLocked.includes(field.key) && !parseErrorCodes(config[field.key]).valid
  );
  const promptRuleValidation = validatePromptRewriteRules(
    config.prompt_rewrite_rules === undefined ? [] : config.prompt_rewrite_rules
  );
  const invalidPromptRules = !envLocked.includes("prompt_rewrite_rules") && !promptRuleValidation.valid;

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const res = await api.get("/config/get");
      setConfig(res.data.config || {});
      setEnvLocked(res.data.env_locked || []);
      if (!silent) setMessage(null);
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "加载配置失败" });
    } finally {
      if (!silent) setLoading(false);
    }
  };

  const saveConfig = async () => {
    if (invalidCodeFields.length) {
      setMessage({ type: "error", text: `请修正无效的错误码：${invalidCodeFields.map((field) => field.label).join("、")}` });
      return;
    }
    if (invalidPromptRules) {
      setMessage({ type: "error", text: promptRuleValidation.error });
      return;
    }
    const payload = { ...config };
    // 旧别名只供兼容读取，面板始终保存新字段。
    delete payload.sensitive_words_enabled;
    delete payload.sensitive_words;
    if ("prompt_rewrite_rules" in payload && !envLocked.includes("prompt_rewrite_rules")) {
      payload.prompt_rewrite_rules = promptRuleValidation.rules;
    }
    for (const field of ERROR_CODE_FIELDS) {
      if (field.key in payload && !envLocked.includes(field.key)) {
        payload[field.key] = parseErrorCodes(payload[field.key]).codes;
      }
    }
    setSaving(true);
    try {
      const res = await api.post("/config/save", { config: payload });
      setMessage({ type: "success", text: res.data.message || "配置保存成功" });
      // 静默刷新保留表单与滚动位置，便于继续编辑刚保存的规则。
      await loadConfig({ silent: true });
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "保存配置失败" });
    } finally {
      setSaving(false);
    }
  };

  const setField = (key, value) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  // 一键镜像/官方端点（墙内访问 Google 官方端点的反代方案）
  const MIRROR_BASE = "https://gcli-api.sukaka.top";
  const OFFICIAL_ENDPOINTS = {
    oauth_proxy_url: "https://oauth2.googleapis.com",
    googleapis_proxy_url: "https://www.googleapis.com",
    antigravity_api_url: "https://daily-cloudcode-pa.googleapis.com",
  };
  const MIRROR_PATHS = {
    oauth_proxy_url: "/oauth2",
    googleapis_proxy_url: "/googleapis",
    antigravity_api_url: "/daily-cloudcode-pa",
  };

  const useMirrorUrls = () => {
    setConfig((prev) => {
      const next = { ...prev };
      for (const [key, path] of Object.entries(MIRROR_PATHS)) {
        if (!envLocked.includes(key)) next[key] = MIRROR_BASE + path;
      }
      return next;
    });
  };

  const restoreOfficialUrls = () => {
    setConfig((prev) => {
      const next = { ...prev };
      for (const [key, url] of Object.entries(OFFICIAL_ENDPOINTS)) {
        if (!envLocked.includes(key)) next[key] = url;
      }
      return next;
    });
  };

  const renderField = (field) => {
    const isLocked = envLocked.includes(field.key);
    const value = config[field.key] ?? "";

    if (isLocked) {
      return (
        <div className="text-xs text-amber-400/80 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          该配置已被环境变量锁定，修改环境变量后重启生效
        </div>
      );
    }

    if (field.type === "bool") {
      return (
        <label className="flex items-center gap-3 cursor-pointer select-none">
          <button
            id={field.key}
            type="button"
            role="switch"
            aria-label={field.label}
            aria-checked={!!value}
            onClick={() => setField(field.key, !value)}
            className="relative w-11 h-10 rounded-lg shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-purple-400"
          >
            <span
              aria-hidden="true"
              className={`absolute inset-x-0 top-2 h-6 rounded-full transition-colors motion-reduce:transition-none ${value ? "bg-purple-600" : "bg-dark-600"}`}
            />
            <span
              aria-hidden="true"
              className={`absolute top-2.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform motion-reduce:transition-none ${
                value ? "translate-x-5" : ""
              }`}
            />
          </button>
          <span className="text-sm text-gray-300">{value ? "已启用" : "已禁用"}</span>
        </label>
      );
    }

    if (field.type === "select") {
      const current = field.options.find((o) => o.value === value) || field.options[0];
      return (
        <div className="space-y-2">
          <Select
            value={current?.value ?? ""}
            onChange={(v) => setField(field.key, v)}
            options={field.options}
            className="w-full sm:w-64"
          />
          {field.descriptions && current && (
            <div className="bg-dark-800/60 border border-dark-600 rounded-lg p-3 text-xs sm:text-sm text-gray-300 leading-relaxed">
              {field.descriptions[current.value]}
            </div>
          )}
        </div>
      );
    }

    if (field.type === "error_codes") {
      return (
        <ErrorCodesField
          id={field.key}
          value={value}
          onChange={(v) => setField(field.key, v)}
          placeholder={field.placeholder}
          emptyHint={field.emptyHint}
        />
      );
    }

    if (field.type === "rules") {
      const columnLabels = field.columnLabels || ["匹配正则", "替换模型名"];
      const columnPlaceholders = field.columnPlaceholders || ["匹配正则，如 ^gpt-4$", "替换模型名，如 gemini-2.5-pro"];
      const validation = field.key === "prompt_rewrite_rules" ? promptRuleValidation : null;
      const rules = Array.isArray(value)
        ? value.map((r) => (Array.isArray(r) ? [String(r[0] ?? ""), String(r[1] ?? "")] : ["", ""]))
        : [];
      const updateRule = (idx, col, val) => {
        const next = rules.map((r) => [...r]);
        next[idx][col] = val;
        setField(field.key, next);
      };
      const removeRule = (idx) => {
        setField(field.key, rules.filter((_, i) => i !== idx));
      };
      const addRule = () => {
        setField(field.key, [...rules, ["", ""]]);
      };
      return (
        <div id={field.key} role="group" aria-label={field.label} className="space-y-2">
          {rules.length === 0 && (
            <div className={`text-xs ${field.key === "prompt_rewrite_rules" ? "text-gray-400" : "text-gray-500"} bg-dark-800/60 border border-dark-700 rounded-lg px-3 py-2`}>
              {field.emptyHint || "暂无规则，点击下方按钮新增"}
            </div>
          )}
          {rules.length > 0 && (
            <div className="flex items-center gap-2 pr-10 text-xs text-gray-400">
              <span className="flex-1 min-w-0">{columnLabels[0]}</span>
              <span aria-hidden="true" className="shrink-0">→</span>
              <span className="flex-1 min-w-0">{columnLabels[1]}</span>
            </div>
          )}
          {rules.map((rule, idx) => (
            <div key={idx} className="space-y-1.5">
            <div className="flex items-center gap-2">
              <input
                id={`${field.key}-${idx}-name`}
                type="text"
                value={rule[0]}
                onChange={(e) => updateRule(idx, 0, e.target.value)}
                placeholder={columnPlaceholders[0]}
                aria-label={`${columnLabels[0]}，第 ${idx + 1} 条`}
                aria-invalid={!!validation?.rowErrors[idx]}
                aria-describedby={`${validation?.rowErrors[idx] ? `${field.key}-${idx}-error ` : ""}${field.key}-note`}
                autoComplete="off"
                spellCheck={false}
                className={`flex-1 min-w-0 px-3 py-2 bg-dark-800 border ${validation?.rowErrors[idx] ? "border-red-500" : "border-dark-600"} rounded-lg text-sm text-white placeholder-gray-500 font-mono`}
              />
              <span aria-hidden="true" className="text-gray-500 text-sm shrink-0">→</span>
              <input
                id={`${field.key}-${idx}-replacement`}
                type="text"
                value={rule[1]}
                onChange={(e) => updateRule(idx, 1, e.target.value)}
                placeholder={columnPlaceholders[1]}
                aria-label={`${columnLabels[1]}，第 ${idx + 1} 条`}
                aria-invalid={!!validation?.rowErrors[idx]}
                aria-describedby={`${validation?.rowErrors[idx] ? `${field.key}-${idx}-error ` : ""}${field.key}-note`}
                autoComplete="off"
                spellCheck={false}
                className={`flex-1 min-w-0 px-3 py-2 bg-dark-800 border ${validation?.rowErrors[idx] ? "border-red-500" : "border-dark-600"} rounded-lg text-sm text-white placeholder-gray-500 font-mono`}
              />
              <button
                type="button"
                onClick={() => removeRule(idx)}
                className="shrink-0 p-2 text-gray-500 hover:text-red-400 transition-colors"
                title="删除该规则"
                aria-label={`删除${field.label}第 ${idx + 1} 条`}
              >
                <Trash2 size={16} />
              </button>
            </div>
            {validation?.rowErrors[idx] && (
              <p id={`${field.key}-${idx}-error`} className="text-xs text-red-300" aria-live="polite">
                {validation.rowErrors[idx]}
              </p>
            )}
            </div>
          ))}
          <button
            type="button"
            onClick={addRule}
            className="btn btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5"
          >
            <Plus size={14} />
            新增规则
          </button>
        </div>
      );
    }

    if (field.type === "textarea") {
      return (
        <LinesField
          value={value}
          onChange={(v) => setField(field.key, v)}
          placeholder={field.placeholder}
        />
      );
    }

    return (
      <input
        type={field.type}
        value={value}
        onChange={(e) =>
          setField(
            field.key,
            field.type === "number" ? Number(e.target.value) : e.target.value
          )
        }
        placeholder={field.placeholder}
        className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500"
      />
    );
  };

  return (
    <div className="space-y-6">
      {/* 操作按钮（sticky 常驻视口顶部） */}
      <div className="sticky top-0 z-30 -mx-1 px-1 py-2 bg-dark-950/95 backdrop-blur border-b border-dark-700 rounded-lg">
        <div className="flex items-center gap-2">
        <button
          onClick={loadConfig}
          disabled={loading}
          className="btn btn-secondary text-sm flex items-center gap-1.5"
        >
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          刷新配置
        </button>
        <button
          onClick={saveConfig}
          disabled={saving || loading || invalidCodeFields.length > 0 || invalidPromptRules}
          className="btn btn-primary text-sm flex items-center gap-1.5 disabled:opacity-50"
        >
          {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
          {saving ? "保存中..." : "保存配置"}
        </button>
        </div>
        {!loading && invalidCodeFields.length > 0 && (
          <p className="mt-2 text-xs text-red-300" role="status">
            {invalidCodeFields.map((field) => field.label).join("、")}格式无效，修正后才能保存。
          </p>
        )}
        {!loading && invalidPromptRules && (
          <p className="mt-2 text-xs text-red-300" role="status">
            自定义名称映射格式无效，修正后才能保存。
          </p>
        )}
      </div>

      {/* 消息弹窗 */}
      {message && (
        <Modal type={message.type} title={message.type === "success" ? "操作成功" : "操作失败"} onClose={() => setMessage(null)}>
          <span className="whitespace-pre-wrap break-all">{message.text}</span>
        </Modal>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin text-purple-500" size={28} />
        </div>
      ) : (
        <div className="space-y-6">
          {CONFIG_GROUPS.map((group) => (
            <div key={group.title} id={group.id} className="card">
              <h3 className="text-base font-semibold text-purple-400 mb-4 border-b border-dark-700 pb-3">
                {group.title}
              </h3>

              {group.title === "端点配置" && (
                <div className="flex flex-wrap gap-2 mb-4">
                  <button
                    type="button"
                    onClick={useMirrorUrls}
                    className="btn btn-secondary text-xs px-3 py-1.5"
                  >
                    🚀 一键使用镜像网址
                  </button>
                  <button
                    type="button"
                    onClick={restoreOfficialUrls}
                    className="btn btn-secondary text-xs px-3 py-1.5"
                  >
                    🔄 还原官方端点
                  </button>
                </div>
              )}

              <div className="space-y-5">
                {group.fields
                  .filter((field) => !field.showIf || field.showIf(config))
                  .map((field) => (
                    <div key={field.key}>
                      <label htmlFor={field.key} className="block text-sm font-medium text-gray-300 mb-1.5">
                        {field.label}
                      </label>
                      {renderField(field)}
                      {field.note && (
                        <p id={`${field.key}-note`} className={`text-xs ${group.noteClassName || "text-gray-500"} mt-1.5`}>{field.note}</p>
                      )}
                    </div>
                  ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
