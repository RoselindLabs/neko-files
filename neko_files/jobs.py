"""Bounded parser children and IO completion before releasing their ownership."""

from pathlib import Path
from typing import Any, Callable

import asyncio
import json
import os
import sys

from .authorization import FileError


async def settle(task: asyncio.Task) -> Any:
    """Drain owned work despite repeated cancellation; do not lose its handle."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def io_call(function: Callable, *args, **kwargs) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await settle(task)
        except Exception:
            pass  # Caller cancellation wins; ownership still ends only after IO finishes.
        raise


async def worker(path: Path, ext: str, operation: str = "parse", **options) -> dict:
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("worker.py")),
            str(path),
            ext,
            operation,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Do not pass provider tokens, proxy credentials or arbitrary app
            # configuration to document parsers. This is not an OS sandbox.
            env={key: value for key, value in os.environ.items()
                 if key in {"SYSTEMROOT", "SystemRoot", "WINDIR", "LANG", "LC_ALL", "TZ"}},
        )
    )
    process = None
    try:
        async with asyncio.timeout(35):
            process = await asyncio.shield(creation)
            process.stdin.write(json.dumps(options).encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            output = bytearray()
            while block := await process.stdout.read(65536):
                output.extend(block)
                if len(output) > 8 * 1024 * 1024:
                    raise FileError("解析输出超过8MiB")
            await process.wait()
            if process.returncode:
                raise FileError("解析进程超时或超过资源限制")
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError):
                raise FileError("解析进程未返回有效结果") from None
            if not isinstance(result, dict) or result.get("success") is not True:
                raise FileError("文件损坏、格式不符或超过解析上限")
            return result
    finally:
        if process is None:
            try:
                process = await settle(creation)
            except Exception:
                process = None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await settle(asyncio.create_task(process.wait()))
