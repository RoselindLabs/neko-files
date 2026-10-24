"""Short accounting lock, per-file leases and verified complete-source records."""

from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid

from .authorization import FileError
from .jobs import io_call, settle


REF = re.compile(r"f_[0-9a-f]{24}")
OWNED = re.compile(r"cache-(f_[0-9a-f]{24})\.[A-Za-z0-9_.-]+")


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def atomic(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        with temporary.open("xb") as file:
            temporary.chmod(0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Cache:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise FileError("缓存目录不可为链接")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = asyncio.Lock()
        self._files = {}
        self._pins = Counter()
        self._reserved = 0

    def path(self, ref, suffix):
        if not REF.fullmatch(ref) or not re.fullmatch(r"\.[A-Za-z0-9_.-]+", suffix):
            raise FileError("内部缓存编号无效")
        path = self.root / ("cache-" + ref + suffix)
        if path.is_symlink():
            raise FileError("缓存文件不可为链接")
        return path

    def _admit(self, required, capacity):
        groups = {}
        for path in self.root.iterdir():
            match = OWNED.fullmatch(path.name)
            if path.is_symlink() or not path.is_file() or not match:
                if not path.exists() and not path.is_symlink():
                    continue  # Another pinned operation may have just committed a temporary file.
                raise FileError("缓存目录存在未知条目或链接，保留现场并拒绝写入")
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            groups.setdefault(match[1], []).append((path, info.st_size, info.st_mtime))
        total = sum(size for files in groups.values() for _, size, _ in files)
        if required > capacity:
            raise FileError("当前缓存额度不足以处理该文件")
        for ref, files in sorted(
            groups.items(), key=lambda pair: min(entry[2] for entry in pair[1])
        ):
            if self._pins[ref]:
                continue
            if (
                time.time() - min(entry[2] for entry in files) > 3600
                or total + self._reserved + required > capacity
            ):
                for path, size, _ in files:
                    total -= size
                    path.unlink()
        if (
            total + self._reserved + required > capacity
            or shutil.disk_usage(self.root).free < self._reserved + required + 128 * 1024**2
        ):
            raise FileError("缓存或磁盘可用空间不足")

    @asynccontextmanager
    async def _file_lock(self, ref):
        if not REF.fullmatch(ref):
            raise FileError("内部缓存编号无效")
        async with self.lock:
            if ref not in self._files and len(self._files) >= 64:
                raise FileError("文件等待队列已满")
            entry = self._files.setdefault(ref, [asyncio.Lock(), 0])
            entry[1] += 1
        acquired = False
        try:
            await entry[0].acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry[0].release()

            async def release():
                async with self.lock:
                    entry[1] -= 1
                    if not entry[1]:
                        del self._files[ref]

            await settle(asyncio.create_task(release()))

    @asynccontextmanager
    async def reserve(self, ref, required, capacity):
        async with self._file_lock(ref):
            async with self.lock:
                await io_call(self._admit, required, capacity)
                self._reserved += required
                self._pins[ref] += 1
            try:
                yield
            finally:

                async def release():
                    async with self.lock:
                        self._reserved -= required
                        self._pins[ref] -= 1
                        if not self._pins[ref]:
                            del self._pins[ref]

                await settle(asyncio.create_task(release()))

    @asynccontextmanager
    async def pin_member(self, ref):
        """Archive job's existing worst-case reservation includes its one child."""
        async with self._file_lock(ref):
            async with self.lock:
                self._pins[ref] += 1
            try:
                yield
            finally:

                async def release():
                    async with self.lock:
                        self._pins[ref] -= 1
                        if not self._pins[ref]:
                            del self._pins[ref]

                await settle(asyncio.create_task(release()))

    async def source(self, ref, ext, limit):
        path = self.path(ref, ext)
        marker = self.path(ref, ".source.json")

        def validate():
            if not path.exists() or not marker.exists():
                path.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                return None
            if marker.stat().st_size > 8192:
                raise FileError("缓存提交记录异常")
            try:
                record = json.loads(marker.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise FileError("缓存提交记录损坏，保留现场") from None
            size = path.stat().st_size
            if (
                not isinstance(record, dict)
                or record.get("format") != 2
                or type(record.get("bytes")) is not int
                or not 0 < size <= limit
                or record["bytes"] != size
                or record.get("sha256") != digest(path)
            ):
                raise FileError("原文件缓存发生变化或未完整提交，保留现场")
            return record

        return await io_call(validate)

    async def commit(self, ref, ext):
        path = self.path(ref, ext)

        def commit():
            size = path.stat().st_size
            if not size:
                raise FileError("缓存文件为空")
            record = {"format": 2, "bytes": size, "sha256": digest(path)}
            atomic(self.path(ref, ".source.json"), json.dumps(record).encode("utf-8"))
            return record

        return await io_call(commit)

    async def read_json(self, path):
        def read():
            if path.stat().st_size > 8 * 1024**2:
                raise FileError("解析缓存超限")
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise FileError("解析缓存损坏，保留现场") from None

        return await io_call(read)

    async def write_json(self, path, value):
        def write():
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            if len(data) > 8 * 1024**2:
                raise FileError("解析缓存超限")
            atomic(path, data)

        await io_call(write)
