from pathlib import Path
from tempfile import TemporaryDirectory

import asyncio
import unittest

from neko_files.authorization import FileError
from neko_files.cache import Cache


A = "f_" + "a" * 24
B = "f_" + "b" * 24


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Cache(Path(self.directory.name) / "cache")

    async def test_unmarked_legacy_or_crashed_final_file_is_not_a_source(self):
        path = self.cache.path(A, ".txt")
        path.write_bytes(b"partial")
        self.assertIsNone(await self.cache.source(A, ".txt", 100))
        self.assertFalse(path.exists())

    async def test_commit_verifies_size_and_digest_and_preserves_corrupt_evidence(self):
        path = self.cache.path(A, ".txt")
        path.write_bytes(b"complete")
        record = await self.cache.commit(A, ".txt")
        self.assertEqual(await self.cache.source(A, ".txt", 100), record)
        path.write_bytes(b"changed!")
        with self.assertRaises(FileError):
            await self.cache.source(A, ".txt", 100)
        self.assertTrue(path.exists())

    async def test_different_files_proceed_concurrently_same_file_waits(self):
        entered = asyncio.Event()

        async def other(ref):
            async with self.cache.reserve(ref, 1, 1000):
                entered.set()

        async with self.cache.reserve(A, 1, 1000):
            task = asyncio.create_task(other(B))
            await asyncio.wait_for(entered.wait(), 1)
            await task
            entered.clear()
            task = asyncio.create_task(other(A))
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
        await task
        self.assertTrue(entered.is_set())
        self.assertFalse(self.cache._files)

    async def test_pinned_files_survive_pressure_and_reservations_release(self):
        async with self.cache.reserve(A, 100, 1000):
            path = self.cache.path(A, ".txt")
            path.write_bytes(b"x" * 600)
            with self.assertRaises(FileError):
                async with self.cache.reserve(B, 500, 1000):
                    pass
            self.assertTrue(path.exists())
        self.assertEqual(self.cache._reserved, 0)
        self.assertFalse(self.cache._pins)
        async with self.cache.reserve(B, 500, 1000):
            self.assertFalse(path.exists())

    async def test_cancelled_waiter_releases_its_lock_registration(self):
        async def waiter():
            async with self.cache.reserve(A, 1, 1000):
                pass

        async with self.cache.reserve(A, 1, 1000):
            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.cache._files)
        self.assertEqual(self.cache._reserved, 0)

    async def test_unknown_files_are_preserved_not_evicted(self):
        sentinel = self.cache.root / "unowned.txt"
        sentinel.write_text("keep")
        with self.assertRaises(FileError):
            async with self.cache.reserve(A, 1, 1000):
                pass
        self.assertTrue(sentinel.exists())
