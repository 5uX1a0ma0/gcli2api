"""
配置路由模块 - 处理 /config/* 相关的HTTP请求
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

import config
from log import log
from src.converter.prompt_rewrite import validate_prompt_rewrite_rules
from src.models import ConfigSaveRequest
from src.storage.database import get_database
from src.utils import verify_panel_token
from .utils import get_env_locked_keys


# 创建路由器
router = APIRouter(prefix="/config", tags=["config"])


@router.get("/get")
async def get_config(token: str = Depends(verify_panel_token)):
    """获取当前配置"""
    try:


        # 读取当前配置（包括环境变量和TOML文件中的配置）
        current_config = {}

        # 基础配置
        current_config["credentials_dir"] = await config.get_credentials_dir()
        current_config["proxy"] = await config.get_proxy_config() or ""

        # 代理端点配置
        current_config["oauth_proxy_url"] = await config.get_oauth_proxy_url()
        current_config["googleapis_proxy_url"] = await config.get_googleapis_proxy_url()
        current_config["antigravity_api_url"] = await config.get_antigravity_api_url()

        # 自动封禁配置
        current_config["auto_ban_enabled"] = await config.get_auto_ban_enabled()
        current_config["auto_ban_error_codes"] = await config.get_auto_ban_error_codes()

        # 自动重试配置（保留原配置键名）
        current_config["retry_error_codes"] = await config.get_retry_error_codes()
        current_config["retry_429_max_retries"] = await config.get_retry_429_max_retries()
        current_config["retry_429_enabled"] = await config.get_retry_429_enabled()
        current_config["retry_429_interval"] = await config.get_retry_429_interval()
        # 抗截断配置
        current_config["anti_truncation_max_attempts"] = await config.get_anti_truncation_max_attempts()

        # 兼容性配置

        # 思维链返回配置
        current_config["return_thoughts_to_frontend"] = await config.get_return_thoughts_to_frontend()

        # IP 白名单配置
        current_config["ip_whitelist_enabled"] = await config.get_ip_whitelist_enabled()
        current_config["ip_whitelist"] = await config.get_ip_whitelist()

        # 模型名复写配置
        current_config["model_rewrite_enabled"] = await config.get_model_rewrite_enabled()
        current_config["model_rewrite_rules"] = await config.get_model_rewrite_rules_config()

        # 请求日志配置
        current_config["usage_log_enabled"] = await config.get_usage_log_enabled()

        # 请求体详细日志配置
        current_config["request_log_enabled"] = await config.get_request_log_enabled()
        current_config["request_log_error_codes"] = await config.get_request_log_error_codes()

        # 凭证选择模式
        current_config["credential_select_mode"] = await config.get_credential_select_mode()
        current_config["free_account_priority_enabled"] = await config.get_free_account_priority_enabled()

        # 服务器配置
        current_config["host"] = await config.get_server_host()
        current_config["port"] = await config.get_server_port()
        current_config["api_password"] = await config.get_api_password()
        current_config["panel_password"] = await config.get_panel_password()

        # 从数据库读取配置
        database = await get_database()
        storage_config = await database.get_all_config()

        # 获取环境变量锁定的配置键
        env_locked_keys = get_env_locked_keys()

        # 合并存储系统配置（不覆盖环境变量）
        for key, value in storage_config.items():
            if key not in env_locked_keys and key not in (
                config.PROMPT_REWRITE_KEYS | config.LEGACY_PROMPT_REWRITE_KEYS
            ):
                current_config[key] = value

        # 返回同一份有效配置及旧别名，避免旧数据库字段覆盖迁移/环境变量结果。
        rewrite = await config.get_prompt_rewrite_config()
        current_config.update({
            "prompt_rewrite_enabled": rewrite.enabled,
            "prompt_rewrite_use_default_keywords": rewrite.use_default_keywords,
            "prompt_rewrite_rules": [list(rule) for rule in rewrite.rules],
            "sensitive_words_enabled": rewrite.enabled,
            "sensitive_words": [name for name, _ in rewrite.rules],
        })

        return JSONResponse(content={"config": current_config, "env_locked": list(env_locked_keys)})

    except Exception as e:
        log.error(f"获取配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save")
async def save_config(request: ConfigSaveRequest, token: str = Depends(verify_panel_token)):
    """保存配置"""
    try:

        new_config = dict(request.config)

        # 新字段在同一请求中优先；旧入口仍可保存，但只写入新字段。
        if "prompt_rewrite_enabled" not in new_config and "sensitive_words_enabled" in new_config:
            new_config["prompt_rewrite_enabled"] = new_config["sensitive_words_enabled"]
        if "prompt_rewrite_rules" not in new_config and "sensitive_words" in new_config:
            if not isinstance(new_config["sensitive_words"], list):
                raise HTTPException(status_code=400, detail="旧词表必须是字符串数组")
            try:
                new_config["prompt_rewrite_rules"] = config.migrate_sensitive_words(new_config["sensitive_words"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        for key in config.LEGACY_PROMPT_REWRITE_KEYS:
            new_config.pop(key, None)

        log.debug(f"收到的配置数据: {list(new_config.keys())}")

        # 验证配置项
        for key, label in (
            ("auto_ban_error_codes", "自动封禁错误码"),
            ("retry_error_codes", "自动重试错误码"),
            ("request_log_error_codes", "详细日志错误码"),
        ):
            if key in new_config:
                try:
                    new_config[key] = config.parse_error_codes(new_config[key])
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"{label}无效：{exc}") from exc

        if "retry_429_max_retries" in new_config:
            if (
                not isinstance(new_config["retry_429_max_retries"], int)
                or new_config["retry_429_max_retries"] < 0
            ):
                raise HTTPException(status_code=400, detail="最大重试次数必须是大于等于0的整数")

        if "retry_429_enabled" in new_config:
            if not isinstance(new_config["retry_429_enabled"], bool):
                raise HTTPException(status_code=400, detail="错误重试开关必须是布尔值")

        # 验证新的配置项
        if "retry_429_interval" in new_config:
            try:
                interval = float(new_config["retry_429_interval"])
                if interval < 0 or interval > 3600:
                    raise HTTPException(status_code=400, detail="重试间隔必须在0-3600秒之间")
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="重试间隔必须是有效的数字")

        if "anti_truncation_max_attempts" in new_config:
            if (
                not isinstance(new_config["anti_truncation_max_attempts"], int)
                or new_config["anti_truncation_max_attempts"] < 1
                or new_config["anti_truncation_max_attempts"] > 10
            ):
                raise HTTPException(
                    status_code=400, detail="抗截断最大重试次数必须是1-10之间的整数"
                )

        if "return_thoughts_to_frontend" in new_config:
            if not isinstance(new_config["return_thoughts_to_frontend"], bool):
                raise HTTPException(status_code=400, detail="思维链返回开关必须是布尔值")

        # 验证整份身份改写配置；禁止空名称、空替换文本和部分接受规则。
        for key in ("prompt_rewrite_enabled", "prompt_rewrite_use_default_keywords"):
            if key in new_config and not isinstance(new_config[key], bool):
                raise HTTPException(status_code=400, detail="身份改写开关必须是布尔值")
        if "prompt_rewrite_rules" in new_config:
            try:
                new_config["prompt_rewrite_rules"] = validate_prompt_rewrite_rules(new_config["prompt_rewrite_rules"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 验证 IP 白名单配置（每行一条，支持单 IP / CIDR / 起止范围，IPv4 / IPv6）
        if "ip_whitelist_enabled" in new_config:
            if not isinstance(new_config["ip_whitelist_enabled"], bool):
                raise HTTPException(status_code=400, detail="IP 白名单开关必须是布尔值")

        if "ip_whitelist" in new_config:
            entries = new_config["ip_whitelist"]
            if not isinstance(entries, list):
                raise HTTPException(status_code=400, detail="IP 白名单必须是数组（每行一条）")
            cleaned_entries = []
            for entry in entries:
                if not isinstance(entry, str):
                    raise HTTPException(status_code=400, detail=f"IP 白名单条目必须是字符串: {entry!r}")
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    config.parse_ip_whitelist_line(entry)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"无效的 IP 白名单条目: {entry!r}")
                cleaned_entries.append(entry)
            new_config["ip_whitelist"] = cleaned_entries

        # 验证模型名复写配置（每条规则 [pattern, replacement]，pattern 支持正则）
        if "model_rewrite_enabled" in new_config:
            if not isinstance(new_config["model_rewrite_enabled"], bool):
                raise HTTPException(status_code=400, detail="模型名复写开关必须是布尔值")

        if "model_rewrite_rules" in new_config:
            rules = new_config["model_rewrite_rules"]
            if not isinstance(rules, list):
                raise HTTPException(status_code=400, detail="模型名复写规则必须是数组（每条 [匹配正则, 替换模型名]）")
            cleaned_rules = []
            for rule in rules:
                if not (isinstance(rule, (list, tuple)) and len(rule) == 2 and all(isinstance(x, str) for x in rule)):
                    raise HTTPException(
                        status_code=400,
                        detail=f"每条规则必须是 [匹配正则, 替换模型名] 的字符串二元组: {rule!r}",
                    )
                pattern, replacement = rule[0].strip(), rule[1].strip()
                if not pattern or not replacement:
                    raise HTTPException(status_code=400, detail=f"规则的正则和替换模型名均不能为空: {rule!r}")
                try:
                    re.compile(pattern)
                except re.error:
                    raise HTTPException(status_code=400, detail=f"无效的正则表达式: {pattern!r}")
                cleaned_rules.append([pattern, replacement])
            new_config["model_rewrite_rules"] = cleaned_rules

        # 验证请求日志配置
        if "usage_log_enabled" in new_config:
            if not isinstance(new_config["usage_log_enabled"], bool):
                raise HTTPException(status_code=400, detail="请求日志开关必须是布尔值")

        # 验证请求体详细日志配置
        if "request_log_enabled" in new_config:
            if not isinstance(new_config["request_log_enabled"], bool):
                raise HTTPException(status_code=400, detail="请求体详细日志开关必须是布尔值")

        # 验证凭证选择模式
        if "credential_select_mode" in new_config:
            mode = new_config["credential_select_mode"]
            if mode not in ("random", "cache", "sticky"):
                raise HTTPException(status_code=400, detail="凭证选择模式必须是 random / cache / sticky")

        if "free_account_priority_enabled" in new_config:
            if not isinstance(new_config["free_account_priority_enabled"], bool):
                raise HTTPException(status_code=400, detail="免费账户优先开关必须是布尔值")

        # 验证服务器配置
        if "host" in new_config:
            if not isinstance(new_config["host"], str) or not new_config["host"].strip():
                raise HTTPException(status_code=400, detail="服务器主机地址不能为空")

        if "port" in new_config:
            if (
                not isinstance(new_config["port"], int)
                or new_config["port"] < 1
                or new_config["port"] > 65535
            ):
                raise HTTPException(status_code=400, detail="端口号必须是1-65535之间的整数")

        if "api_password" in new_config:
            if not isinstance(new_config["api_password"], str):
                raise HTTPException(status_code=400, detail="API访问密码必须是字符串")

        if "panel_password" in new_config:
            if not isinstance(new_config["panel_password"], str):
                raise HTTPException(status_code=400, detail="控制面板密码必须是字符串")

        # 获取环境变量锁定的配置键
        env_locked_keys = get_env_locked_keys()

        database = await get_database()
        storage_config = await database.get_all_config()
        # 惰性迁移：首次保存其他配置时也写入未被环境变量锁定的新字段。
        rewrite = await config.get_prompt_rewrite_config()
        for key, value in (
            ("prompt_rewrite_enabled", rewrite.enabled),
            ("prompt_rewrite_use_default_keywords", rewrite.use_default_keywords),
            ("prompt_rewrite_rules", [list(rule) for rule in rewrite.rules]),
        ):
            if key not in storage_config and key not in env_locked_keys:
                new_config.setdefault(key, value)

        # 直接写入数据库
        for key, value in new_config.items():
            if key not in env_locked_keys:
                await database.set_config(key, value)
                if key in ("api_password", "panel_password"):
                    log.debug(f"设置{key}字段为: {value}")

        # 重新加载配置缓存（关键！）
        await config.reload_config()

        # 验证保存后的结果
        test_api_password = await config.get_api_password()
        test_panel_password = await config.get_panel_password()
        log.debug(f"保存后立即读取的API密码: {test_api_password}")
        log.debug(f"保存后立即读取的面板密码: {test_panel_password}")

        # 构建响应消息
        response_data = {
            "message": "配置保存成功",
            "saved_config": {k: v for k, v in new_config.items() if k not in env_locked_keys},
        }

        return JSONResponse(content=response_data)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"保存配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
