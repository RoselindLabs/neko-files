"""Scoped metadata in one registry. Short-lived URLs are private, not platform IDs.

Optional JSON source fields preserve existing schema/refs and OCR attempt records.
"""

from pathlib import Path
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

from .authorization import FileError


class Store:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise FileError("插件数据目录不可为链接")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "files.db"
        if self.db.is_symlink():
            raise FileError("索引不可为链接")
        with self.connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, scope TEXT, created REAL, data TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS ocr_attempts (id TEXT PRIMARY KEY, created REAL)"
            )
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=3)
        try:
            with con:
                yield con
        finally:
            con.close()

    def scope(self, c):
        return "\0".join([c["session"], c["account"], c["group"]])

    def _item(self, c, raw, message_id="", parent="", member=""):
        if not isinstance(raw, dict):
            raise FileError("文件元数据格式无效")
        file_id = raw.get("file_id") or raw.get("id") or ""
        ident = file_id or raw.get("source_key")
        name = raw.get("file_name") or raw.get("name")
        if not isinstance(ident, str) or not 0 < len(ident) <= 1024 or not isinstance(name, str):
            raise FileError("文件元数据缺少有效编号或文件名")
        if type(raw.get("file_size", raw.get("size", 0))) is not int:
            raise FileError("文件大小格式无效")
        size = raw.get("file_size", raw.get("size", 0))
        if size < 0:
            raise FileError("文件大小无效")
        name = name.replace("\\", "/").rsplit("/", 1)[-1][:240]
        if not name or any(ord(ch) < 32 for ch in name):
            raise FileError("文件名无效")
        busid = raw.get("busid", 0)
        if type(busid) is not int or busid < 0:
            raise FileError("文件资源类型无效")
        file_hash = raw.get('file_hash', '')
        if not isinstance(file_hash, str) or len(file_hash) > 1024 or any(ord(ch) < 33 for ch in file_hash):
            raise FileError('文件校验元数据无效')
        ref = "f_" + hashlib.sha256((self.scope(c) + "\0" + ident).encode()).hexdigest()[:24]
        item = {
            "ref": ref,
            "file_id": file_id,
            "name": name,
            "size": size,
            "busid": busid,
            "file_hash": file_hash,
            "message_id": str(message_id)[:256],
            "parent": parent,
            "member": member,
        }
        if "source" in raw:
            source = raw["source"]
            if (
                not isinstance(source, dict)
                or source.get("kind") not in ["message", "notice", "catalog", "text_candidate"]
                or type(source.get("slot")) is not int
                or not 0 <= source["slot"] < 500
            ):
                raise FileError("来源元数据无效")
            item.update(source=dict(source), url=raw.get("url", ""), url_seen=time.time())
        return ref, item

    def _save(self, con, c, ref, item):
        old = con.execute("SELECT data,scope FROM files WHERE id=?", (ref,)).fetchone()
        if old and old[1] != self.scope(c):
            raise FileError("索引作用域冲突，保留现场")
        if old and item.get("source", {}).get("kind") in ["message", "notice", "catalog"]:
            previous = json.loads(old[0])
            if previous["name"] != item["name"] or (
                previous["size"] and item["size"] and previous["size"] != item["size"]
            ):
                raise FileError("同一资源元数据发生变化，保留缓存；请单独重新提供文件")
            if not item["size"]:
                item["size"] = previous["size"]
            # Missing/zero means unknown, not a change to the real business ID.
            item['busid'] = item['busid'] or previous.get('busid', 0)
            if previous.get('file_hash') and item['file_hash'] and previous['file_hash'] != item['file_hash']:
                raise FileError('同一文件校验来源发生变化，保留缓存和账本')
            item['file_hash'] = item['file_hash'] or previous.get('file_hash', '')
            if not item['file_id'] and item.get('url') == previous.get('url'):
                # A repeated get_msg/list observation is not URL renewal.
                # Keep unknown legacy age unknown instead of blessing it now.
                item['url_seen'] = previous.get('url_seen')
            # Fresh scope-proven registration may update URL/provenance, never extend 7-day TTL.
            con.execute("UPDATE files SET data=? WHERE id=?", (json.dumps(item), ref))
        elif not old:
            con.execute(
                "INSERT INTO files VALUES (?,?,?,?)",
                (ref, self.scope(c), time.time(), json.dumps(item, ensure_ascii=False)),
            )

    def register(self, c, raw, message_id="", parent="", member=""):
        ref, item = self._item(c, raw, message_id, parent, member)
        with self.connect() as con:
            con.execute("DELETE FROM files WHERE created < ?", (time.time() - 7 * 86400,))
            self._save(con, c, ref, item)
            con.execute(
                "DELETE FROM files WHERE id IN (SELECT id FROM files ORDER BY created DESC LIMIT -1 OFFSET 2048)"
            )
        return self.get(c, ref)

    def register_many(self, c, records, message_id=""):
        prepared = [self._item(c, raw, message_id) for raw in records]
        with self.connect() as con:
            con.execute("DELETE FROM files WHERE created < ?", (time.time() - 7 * 86400,))
            for ref, item in prepared:
                self._save(con, c, ref, item)
            con.execute(
                "DELETE FROM files WHERE id IN (SELECT id FROM files ORDER BY created DESC LIMIT -1 OFFSET 2048)"
            )
            rows = dict(con.execute("SELECT id,data FROM files WHERE scope=?", (self.scope(c),)))
        return [json.loads(rows[ref]) for ref, _ in prepared]

    def get(self, c, ref):
        if not isinstance(ref, str):
            raise FileError("文件编号格式无效")
        with self.connect() as con:
            row = con.execute(
                "SELECT data FROM files WHERE id=? AND scope=? AND created>?",
                (ref, self.scope(c), time.time() - 7 * 86400),
            ).fetchone()
        if not row:
            raise FileError("当前聊天/账号无此文件或索引已过期")
        return json.loads(row[0])

    def recent(self, c, limit=20):
        with self.connect() as con:
            return [
                json.loads(r[0])
                for r in con.execute(
                    "SELECT data FROM files WHERE scope=? AND created>? ORDER BY created DESC, id LIMIT ?",
                    (self.scope(c), time.time() - 7 * 86400, min(limit, 2048)),
                )
            ]

    def admit_ocr(self, c, ref, page):
        key = hashlib.sha256((self.scope(c) + "\0" + ref + "\0" + str(page)).encode()).hexdigest()
        with self.connect() as con:
            con.execute("DELETE FROM ocr_attempts WHERE created < ?", (time.time() - 3600,))
            if con.execute("SELECT COUNT(*) FROM ocr_attempts").fetchone()[0] >= 256:
                return False
            return (
                con.execute(
                    "INSERT OR IGNORE INTO ocr_attempts VALUES (?,?)", (key, time.time())
                ).rowcount
                == 1
            )


def public(item):
    result = {k: item[k] for k in ["ref", "name", "size", "message_id", "parent", "member"]}
    kind = item.get("source", {}).get("kind", "legacy")
    if kind == "legacy" and str(item["file_id"]).startswith(("http://", "https://")):
        kind = "text_candidate"
    result["source_kind"] = kind
    result["source_verified"] = kind in ["message", "notice", "catalog"]
    return result
