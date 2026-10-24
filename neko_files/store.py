"""Private, bounded, chat/account-scoped file metadata. No resource URLs persisted."""

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
        ident = raw.get("file_id") or raw.get("id")
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
        ref = "f_" + hashlib.sha256((self.scope(c) + "\0" + ident).encode()).hexdigest()[:24]
        item = {
            "ref": ref,
            "file_id": ident,
            "name": name,
            "size": size,
            "busid": busid,
            "message_id": str(message_id)[:256],
            "parent": parent,
            "member": member,
        }
        return ref, item

    def register(self, c, raw, message_id="", parent="", member=""):
        ref, item = self._item(c, raw, message_id, parent, member)
        with self.connect() as con:
            con.execute("DELETE FROM files WHERE created < ?", (time.time() - 7 * 86400,))
            con.execute(
                "INSERT OR IGNORE INTO files VALUES (?,?,?,?)",
                (ref, self.scope(c), time.time(), json.dumps(item, ensure_ascii=False)),
            )
            con.execute(
                "DELETE FROM files WHERE id IN (SELECT id FROM files ORDER BY created DESC LIMIT -1 OFFSET 2048)"
            )
        return self.get(c, ref)

    def register_many(self, c, records):
        prepared = [self._item(c, raw) for raw in records]
        with self.connect() as con:
            con.execute("DELETE FROM files WHERE created < ?", (time.time() - 7 * 86400,))
            con.executemany(
                "INSERT OR IGNORE INTO files VALUES (?,?,?,?)",
                [
                    (ref, self.scope(c), time.time(), json.dumps(item, ensure_ascii=False))
                    for ref, item in prepared
                ],
            )
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
    return {k: item[k] for k in ["ref", "name", "size", "message_id", "parent", "member"]}
