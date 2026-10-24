"""Only a host-overwritten context plus current QQ metadata can authorize IO."""

import asyncio
import re


class FileError(ValueError):
    pass


def numeric(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def policy(config):
    a = config.access
    limits = config.limits
    return (
        config.plugin.enabled,
        tuple(a.group_ids),
        tuple(a.user_ids),
        a.account_id,
        a.group_catalog,
        a.ocr,
        limits.file_mib,
        limits.cache_mib,
        limits.timeout_seconds,
        limits.output_chars,
        limits.reads_per_minute,
    )


async def authorize(ctx, config, envelope):
    if not config.plugin.enabled:
        raise FileError("文件插件未启用")
    if not isinstance(envelope, dict) or envelope.get("contract") != "neko.tools.context.v1":
        raise FileError("宿主缺少可信工具上下文补丁，已拒绝文件操作")
    session = envelope.get("session_id")
    if (
        not isinstance(session, str)
        or not 0 < len(session) <= 256
        or envelope.get("stream_id") != session
        or envelope.get("platform") != "qq"
    ):
        raise FileError("可信QQ会话信息不完整")
    a = config.access
    if (
        len(a.group_ids) > 16
        or len(a.user_ids) > 16
        or not all(numeric(v) for v in [*a.group_ids, *a.user_ids])
        or (a.account_id and not numeric(a.account_id))
    ):
        raise FileError("QQ授权名单或机器人账号格式无效")
    async with asyncio.timeout(3):
        rows = await ctx.chat.get_all_streams(platform="qq")
    if not isinstance(rows, list) or len(rows) > 512:
        raise FileError("宿主会话查询不可用或超限")
    matches = [r for r in rows if isinstance(r, dict) and r.get("session_id") == session]
    if len(matches) != 1:
        raise FileError("当前会话归属无法唯一核实")
    row = matches[0]
    group = row.get("chat_type") == "group"
    target = str(row.get("group_id" if group else "user_id") or "")
    account = str(row.get("account_id") or "")
    if (
        row.get("platform") != "qq"
        or row.get("stream_id", session) != session
        or row.get("chat_type") not in ["group", "private"]
        or row.get("is_group_session") is not group
        or not numeric(account)
        or not numeric(target)
        or (not group and row.get("group_id") not in [None, ""])
        or target not in (a.group_ids if group else a.user_ids)
        or (a.account_id and account != a.account_id)
        or (envelope.get("account_id") and envelope["account_id"] != account)
        or (group and str(envelope.get("group_id") or "") != target)
        or (not group and envelope.get("group_id") not in [None, ""])
        or (not group and str(envelope.get("user_id") or "") != target)
    ):
        raise FileError("当前QQ目标或机器人账号未授权")
    return {
        "session": session,
        "account": account,
        "group": target if group else "",
        "user": target if not group else str(envelope.get("user_id") or ""),
    }
