"""Bounded source normalization; no RPC, downloads or independent registry."""

import re

from .authorization import FileError
from .transport import validate_url


def message_id(value):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 256
        or any(ord(ch) < 33 for ch in value)
        or "/" in value or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or re.fullmatch(r"m[a-z2-7]{6}", value)
        or value.lower().startswith(("http:", "https:", "file:"))
    ):
        raise FileError("需要真实消息ID；未解析的短编号、URL或路径不可取源")
    return value


def bounded_int(value, maximum=40 * 1024**2):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= maximum:
        raise FileError("文件大小或资源类型无效/超限")
    return value


def host_scope(message, c, ident):
    if not isinstance(message, dict):
        raise FileError("宿主没有该消息，或消息能力未授权")
    info = message.get("message_info")
    if not isinstance(info, dict):
        raise FileError("消息归属信息缺失")
    group, user = info.get("group_info") or {}, info.get("user_info") or {}
    if not isinstance(group, dict) or not isinstance(user, dict):
        raise FileError("消息目标信息无效")
    if (
        message.get("message_id") != ident
        or message.get("session_id") != c["session"]
        or message.get("platform") != "qq"
        or str(message.get("account_id") or "") != c["account"]
        or str(group.get("group_id") or "") != c["group"]
        or (not c["group"] and str(user.get("user_id") or "") != c["user"])
    ):
        raise FileError("消息ID、会话、群私或机器人账号不属于当前授权目标")


def adapter_scope(message, c, ident):
    if not isinstance(message, dict) or str(message.get("message_id") or "") != ident:
        raise FileError("QQ原始消息ID无法核实")
    if c["group"]:
        valid = message.get("message_type") == "group" and str(
            message.get("group_id") or ""
        ) == c["group"]
    else:
        sender = message.get("sender") or {}
        valid = (
            isinstance(sender, dict)
            and message.get("message_type") == "private"
            and message.get("group_id") in [None, "", 0]
            and str(sender.get("user_id") or "") == c["user"]
        )
    if not valid:
        raise FileError("QQ原始消息缺少当前群/私聊目标证明")


def attachment(raw, ident, slot, kind="message"):
    if not isinstance(raw, dict):
        raise FileError("附件结构无效")
    name = raw.get("name") or raw.get("file_name") or raw.get("file")
    if not isinstance(name, str) or not 0 < len(name) <= 240:
        raise FileError("附件文件名无效")
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or any(ord(ch) < 32 for ch in name):
        raise FileError("附件文件名无效")
    file_id = raw.get("file_id") or raw.get("id") or ""
    if not isinstance(file_id, str) or len(file_id) > 1024 or any(ord(ch) < 33 for ch in file_id):
        raise FileError("附件资源ID无效")
    # A signed URL is never a stable platform resource ID.
    url = raw.get("url") or ""
    if file_id.startswith(("http://", "https://")):
        url, file_id = url or file_id, ""
    if url:
        validate_url(url)
        if len(url) > 4096:
            raise FileError("附件链接过长")
    if not file_id and not url:
        raise FileError("附件没有可核实资源；不接受本地路径/base64/官方file_ref")
    if file_id.startswith(("file:", "/", "\\")):
        raise FileError("附件资源不能为本地路径")
    return {
        "file_id": file_id,
        "name": name,
        # Catalog metadata is not download admission. An unrelated large file
        # must not hide every small document in the same directory. document()
        # still enforces the configured download/parser byte limit.
        "size": bounded_int(raw.get("size", raw.get("file_size", 0)),
                            2**63 - 1 if kind == 'catalog' else 40 * 1024**2),
        "busid": bounded_int(raw.get("busid", 0), 2**31 - 1),
        "url": url,
        "source": {"kind": kind, "slot": slot},
        "source_key": ("candidate:" if kind == "text_candidate" else "message:") + ident + ":" + str(slot),
    }


def extract(message, c, ident, adapter=False):
    """Only top-level file/reply segments, never forward contents or quote text."""
    segments = message.get("message" if adapter else "raw_message") or []
    if not isinstance(segments, list) or len(segments) > 128:
        raise FileError("消息段格式无效或超限")
    records, replies = [], []
    if not adapter:
        info = message.get("message_info") or {}
        extra = info.get("additional_config") or {}
        if not isinstance(extra, dict):
            raise FileError("消息附加信息无效")
        notice = extra.get("napcat_notice_payload") or {}
        if extra.get("napcat_notice_type") == "group_upload":
            if (
                not isinstance(notice, dict)
                or not c["group"]
                or str(notice.get("group_id") or "") != c["group"]
            ):
                raise FileError("上传通知群归属无法核实")
            records.append(attachment(notice.get("file"), ident, 0, "notice"))
        if message.get("reply_to"):
            replies.append(message_id(message["reply_to"]))
    for segment in segments:
        if not isinstance(segment, dict):
            raise FileError("消息段格式无效")
        data = segment.get("data")
        if segment.get("type") == "file":
            if len(records) >= 8:
                raise FileError("单条消息附件超过8个，请单独提供")
            records.append(attachment(data, ident, len(records)))
        elif segment.get("type") == "reply":
            if not isinstance(data, dict):
                raise FileError("引用结构无效")
            replies.append(message_id(str(data.get("id" if adapter else "target_message_id") or "")))
    if len(set(replies)) > 1:
        raise FileError("引用目标不唯一")
    return records, replies[0] if replies else ""
