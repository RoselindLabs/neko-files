from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import asyncio
import os
import subprocess
import sys
import unittest

from neko_files import jobs, transport
from neko_files.authorization import FileError


class Response:
    def __init__(self, chunks, status=200, headers=None, length=None, gate=None):
        self.chunks = chunks
        self.status = status
        self.headers = headers or {}
        self.content_length = length
        self.content = self
        self.gate = gate

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk
        if self.gate:
            await self.gate.wait()


class NetworkFixture:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []
        self.settings = {}

    def __call__(self, **kwargs):
        self.settings = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.urls.append(url)
        return next(self.responses)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.enterContext(patch.object(transport, "local_addresses", return_value=set()))
        self.enterContext(patch.object(transport.aiohttp, "TCPConnector", return_value=None))

    async def test_complete_download_is_atomic_and_existing_destination_is_not_clobbered(self):
        target = self.root / "cache.txt"
        network = NetworkFixture([Response([b"complete"], length=8)])
        with patch.object(transport.aiohttp, "ClientSession", network):
            await transport.download("https://example.invalid/file", target, 100, 8)
        self.assertEqual(target.read_bytes(), b"complete")
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse(network.settings["trust_env"])
        self.assertFalse(network.settings["auto_decompress"])
        network = NetworkFixture([Response([b"new"])])
        with (
            patch.object(transport.aiohttp, "ClientSession", network),
            self.assertRaises(FileExistsError),
        ):
            await transport.download("https://example.invalid/file", target, 100)
        self.assertEqual(target.read_bytes(), b"complete")

    async def test_short_or_oversize_responses_do_not_publish_partial_sources(self):
        for response, limit, expected in [
            (Response([b"x"], length=2), 100, 0),
            (Response([b"xx"]), 1, 0),
            (Response([b"x"]), 100, 2),
        ]:
            target = self.root / "cache.txt"
            with (
                patch.object(transport.aiohttp, "ClientSession", NetworkFixture([response])),
                self.assertRaises(FileError),
            ):
                await transport.download("https://example.invalid/file", target, limit, expected)
            self.assertFalse(target.exists())
            self.assertFalse(list(self.root.glob("*.part")))

    async def test_redirect_to_private_ip_and_nonidentity_encoding_fail_closed(self):
        response = Response([], status=302, headers={"Location": "http://127.0.0.1/private"})
        network = NetworkFixture([response])
        with (
            patch.object(transport.aiohttp, "ClientSession", network),
            self.assertRaises(FileError),
        ):
            await transport.download("https://example.invalid/file", self.root / "cache.txt", 100)
        self.assertEqual(len(network.urls), 1)
        with (
            patch.object(
                transport.aiohttp,
                "ClientSession",
                NetworkFixture([Response([b"x"], headers={"Content-Encoding": "gzip"})]),
            ),
            self.assertRaises(FileError),
        ):
            await transport.download("https://example.invalid/file", self.root / "cache.txt", 100)

    async def test_cancelled_stream_leaves_no_final_or_partial_file(self):
        gate = asyncio.Event()
        network = NetworkFixture([Response([b"x" * 65536], gate=gate)])
        with patch.object(transport.aiohttp, "ClientSession", network):
            task = asyncio.create_task(
                transport.download("https://example.invalid/file", self.root / "cache.txt", 100000)
            )
            for _ in range(100):
                if list(self.root.glob("*.part")):
                    break
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(list(self.root.iterdir()))

    async def test_spawn_cancel_waits_for_owned_handle_then_kills_and_reaps(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class Process:
            returncode = None
            killed = False
            reaped = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                self.reaped = True
                return self.returncode

        process = Process()

        async def spawn(*args, **kwargs):
            started.set()
            await release.wait()
            return process

        with patch.object(jobs.asyncio, "create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(jobs.worker(self.root / "input.txt", ".txt"))
            await started.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed and process.reaped)

    @unittest.skipUnless(os.name == "posix", "actual resource-limited child requires Linux")
    async def test_real_resource_limited_parser_child(self):
        path = self.root / "text.txt"
        path.write_text("synthetic child", encoding="utf-8")
        result = await jobs.worker(path, ".txt")
        self.assertEqual(result["units"][0]["text"], "synthetic child")
        import pymupdf

        document = pymupdf.open()
        document.new_page().insert_text((50, 50), "Synthetic limited PDF")
        path = self.root / "page.pdf"
        document.save(path)
        document.close()
        parsed = await jobs.worker(path, ".pdf")
        self.assertIn("Synthetic limited PDF", parsed["units"][0]["text"])
        self.assertTrue((await jobs.worker(path, ".pdf", "render", page=1))["image_base64"])


class URLTests(unittest.TestCase):
    def test_hard_exit_never_publishes_the_final_cache_name(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "cache-f_000000000000000000000001.txt"
            program = r"""
from pathlib import Path
import asyncio,os,sys
sys.path.insert(0,sys.argv[1])
from neko_files import transport
transport.local_addresses=lambda:set()
class Content:
 async def iter_chunked(self,size):
  yield b'x'*65536
  os._exit(0)
class Response:
 status=200;headers={};content_length=131072;content=Content()
 async def __aenter__(self):return self
 async def __aexit__(self,*args):pass
class Session:
 def __init__(self,*args,**kwargs):pass
 async def __aenter__(self):return self
 async def __aexit__(self,*args):pass
 def get(self,*args,**kwargs):return Response()
transport.aiohttp.ClientSession=Session
transport.aiohttp.TCPConnector=lambda **kwargs:None
asyncio.run(transport.download('https://example.invalid/file',Path(sys.argv[2]),1024*1024))
"""
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", program, str(root), str(target)],
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertFalse(target.exists())
            self.assertEqual(len(list(target.parent.glob("*.part"))), 1)

    def test_private_mapped_metadata_and_nonstandard_ports_are_rejected(self):
        for url in [
            "file:///etc/passwd",
            "http://localhost/x",
            "http://169.254.169.254/x",
            "http://[::ffff:127.0.0.1]/x",
            "https://example.invalid:8443/x",
            "http://user:pass@example.invalid/x",
        ]:
            with self.assertRaises(FileError):
                transport.validate_url(url)
        self.assertEqual(
            transport.validate_url("https://example.invalid/file").hostname, "example.invalid"
        )
