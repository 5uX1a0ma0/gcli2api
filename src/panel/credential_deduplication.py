"""按 Google 返回的实际邮箱扫描并清理重复凭证。"""

import asyncio
import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from log import log
from src.credential_manager import invalidate_model_cache
from src.google_oauth_api import Credentials, get_user_email
from src.models import CredDeduplicateRequest
from src.storage.database import get_database
from src.utils import verify_panel_token


router = APIRouter()
CREDENTIAL_LOOKUP_TIMEOUT = 30


@router.post("/deduplicate")
async def deduplicate_credentials(
    request: CredDeduplicateRequest, http_request: Request,
    token: str = Depends(verify_panel_token)
):
    """默认仅扫描；清理必须提供扫描预览的标识，且重新核实实际邮箱。"""
    if request.emails is not None and not request.emails:
        raise HTTPException(status_code=400, detail="所选凭证不能为空；扫描全部请省略 emails")
    if not request.dry_run and not request.preview:
        raise HTTPException(status_code=400, detail="请先扫描重复凭证，再确认去重")

    database = await get_database()
    emails = list(dict.fromkeys(request.emails)) if request.emails is not None else None
    snapshots = await database.get_credential_deduplication_snapshots(emails)
    semaphore = asyncio.Semaphore(4)

    async def resolve(snapshot: dict[str, Any]) -> dict[str, Any]:
        try:
            credentials = Credentials.from_dict(snapshot["credential_data"])
            # 从 refresh_token 换取新令牌，核实长期可用性及其真实归属，避免导入的
            # access_token 与 refresh_token 属于不同账户时按错误的身份清理。
            await credentials.refresh()
            actual_email = await get_user_email(credentials)
            if not isinstance(actual_email, str):
                raise ValueError("无法获取实际邮箱")
            actual_email = actual_email.strip().lower()
            if len(actual_email) > 320 or not re.fullmatch(r"[^@\s]+@[^@\s]+", actual_email):
                raise ValueError("无法获取有效的实际邮箱")
            return {
                "snapshot": snapshot,
                "actual_email": actual_email,
                "credential_data": credentials.to_dict(),
            }
        except Exception:
            # 无法验证的凭证绝不参与删除，且不向前端返回含令牌的异常信息。
            return {"snapshot": snapshot, "error": "查询实际邮箱失败，已保留凭证"}

    async def bounded_resolve(snapshot: dict[str, Any]) -> dict[str, Any]:
        # 超时从获取并发槽之后开始，排队时间不计入单项查询期限。
        async with semaphore:
            if await http_request.is_disconnected():
                return {"snapshot": snapshot, "error": "扫描已停止，已保留凭证"}
            try:
                return await asyncio.wait_for(resolve(snapshot), timeout=CREDENTIAL_LOOKUP_TIMEOUT)
            except TimeoutError:
                return {"snapshot": snapshot, "error": "查询实际邮箱超时，已保留凭证"}

    resolved = await asyncio.gather(*(bounded_resolve(snapshot) for snapshot in snapshots))
    if await http_request.is_disconnected():
        raise HTTPException(status_code=499, detail="扫描已停止")
    verified = {
        item["snapshot"]["email"]: item for item in resolved if "actual_email" in item
    }
    errors = [
        {"email": item["snapshot"]["email"], "message": item["error"]}
        for item in resolved if "error" in item
    ]
    missing = sorted(set(emails or ()) - {snapshot["email"] for snapshot in snapshots})
    errors.extend({"email": email, "message": "凭证不存在，已跳过"} for email in missing)

    email_groups: dict[str, list[dict[str, Any]]] = {}
    for item in verified.values():
        email_groups.setdefault(item["actual_email"], []).append(item)
    groups = []
    for actual_email, members in sorted(email_groups.items()):
        if len(members) < 2:
            continue
        members.sort(key=lambda item: (
            item["snapshot"]["disabled"],
            item["snapshot"]["email"] != actual_email,
            item["snapshot"]["id"],
        ))
        groups.append({
            "actual_email": actual_email,
            "keep_email": members[0]["snapshot"]["email"],
            "duplicate_emails": [item["snapshot"]["email"] for item in members[1:]],
        })

    preview = hashlib.sha256(json.dumps({
        "entries": sorted([
            [item["snapshot"]["email"], item["snapshot"]["fingerprint"], item.get("actual_email")]
            for item in resolved
        ]),
        "missing": missing,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    duplicate_count = sum(len(group["duplicate_emails"]) for group in groups)
    removed = []
    if not request.dry_run:
        if request.preview != preview:
            raise HTTPException(status_code=409, detail="凭证或实际邮箱查询结果已变化，请重新扫描后再去重")
        try:
            removed = await database.apply_credential_deduplication(groups, verified)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            log.error(f"实际邮箱去重失败: {type(exc).__name__}")
            raise HTTPException(status_code=500, detail="邮箱去重失败，请重新扫描后重试") from exc
        for group in groups:
            for email in [group["keep_email"], *group["duplicate_emails"]]:
                invalidate_model_cache(email)
        log.info(f"实际邮箱去重完成: 清理 {len(removed)} 个重复凭证，保留 {len(groups)} 组")

    return {
        "dry_run": request.dry_run,
        "total_count": len(emails) if emails is not None else len(snapshots),
        "verified_count": len(verified),
        "duplicate_count": duplicate_count,
        "removed_count": len(removed),
        "groups": groups,
        "errors": errors,
        "preview": preview,
    }
