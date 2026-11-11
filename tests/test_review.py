"""Offline regressions for cost ownership, paging and model-visible contracts."""

import json
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext

from neko_files import archives, ocr, service as module, worker
from pathlib import Path
from neko_files.authorization import FileError
from neko_files.plugin import NekoFiles
from neko_files.reading import window
from tests.helpers import ServiceFixture, envelope, scope


class ReviewTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_unknown_survives_time_reload_and_cache_loss_and_full_ledger(self):
        self.config.access.ocr = True
        item, path = await self.cached("page.pdf", b"synthetic")
        self.ctx.llm.generate.side_effect = TimeoutError("unknown")
        document = {"kind": "pages", "units": [{"label": "1", "text": ""}]}
        with patch.object(module, "worker", AsyncMock(return_value=document)), patch.object(
            ocr, "worker", AsyncMock(return_value={"image_base64": "synthetic"})
        ):
            first = await self.service.read(envelope(), item["ref"], 1, 1, use_ocr=True)
            self.assertFalse(first["success"])
            with self.service.store.connect() as con:
                con.execute("UPDATE ocr_attempts SET created=created-86400")
            await self.service.stop()
            await self.service.start(self.ctx, self.config)
            # Simulate eviction of derived results, preserving the source fixture.
            for cached in path.parent.glob("*.parsed.v2.json"):
                cached.unlink()
            again = await self.service.read(envelope(), item["ref"], 1, 1, use_ocr=True)
            self.assertFalse(again["success"])
            self.ctx.llm.generate.assert_awaited_once()
            for page in range(2, 257):
                self.assertTrue(await self.service.db("admit_ocr", scope(), item["ref"], page))
            self.assertFalse(await self.service.db("admit_ocr", scope(), item["ref"], 257))
            with self.service.store.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM ocr_attempts").fetchone()[0], 256)

    async def test_full_index_consumes_cursor_without_extending_ttl(self):
        await self.service.db("register_many", scope(), [
            {"file_id": str(n), "name": f"{n}.txt", "size": 1} for n in range(700)
        ])
        result = await self.service.list_files(envelope())
        seen = set()
        expiry = self.service.list_pages[result["next_cursor"]]["expires"]
        while True:
            self.assertTrue(result["success"])
            visible = json.loads(result["content"])
            self.assertEqual([f["ref"] for f in visible["files"]], [f["ref"] for f in result["files"]])
            self.assertNotIn("directory_complete", visible)
            self.assertIn("近7天", visible["search_instruction"])
            seen.update(item["ref"] for item in result["files"])
            cursor = result["next_cursor"]
            if not cursor:
                break
            self.assertEqual(len(self.service.list_pages), 1)
            self.assertEqual(self.service.list_pages[cursor]["expires"], expiry)
            self.service.rates.clear()
            result = await self.service.list_files(envelope(), cursor=cursor)
            self.assertNotIn(cursor, self.service.list_pages)
        self.assertEqual(len(seen), 700)
        self.assertEqual(len(self.service.list_pages), 0)

    async def test_archive_pages_preserve_long_complete_member_names(self):
        item = await self.register("long.zip", b"")
        path = self.service.cache.path(item["ref"], ".zip")
        names = [f"{n}/" + "x" * 990 + ".txt" for n in range(100)]
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, "synthetic")
        await self.service.cache.commit(item["ref"], ".zip")
        listing = archives.catalog(path, ".zip")
        seen, offset = [], 0
        with patch.object(module, "worker", AsyncMock(return_value=listing)):
            while True:
                self.service.rates.clear()
                result = await self.service.inspect(envelope(), item["ref"], offset)
                self.assertTrue(result["success"])
                self.assertLess(len(result["content"]), 8000)
                seen.extend(entry["name"] for entry in result["entries"])
                offset = result["next_offset"]
                if offset is None:
                    break
        self.assertEqual(seen, names)
        async def parser(source, ext, operation="parse", **options):
            if operation == "archive-read":
                archives.extract_member(source, ext, options["member"],
                                        Path(options["destination"]), options["name"], options["limit"])
                return {"extracted": True}
            return worker.parse(source, ext)
        self.service.rates.clear()
        with patch.object(module, "worker", side_effect=parser):
            member = await self.service.read_archive(envelope(), item["ref"], seen[-1])
            self.assertTrue(member["success"], member)
            self.assertEqual(member["file"]["member"], seen[-1])
            self.assertIn("synthetic", member["preview"]["text"])
            result = await self.service.read(envelope(), member["member_file_ref"])
            self.assertTrue(result["success"], result)
            self.assertIn("synthetic", result["text"])
        destination = path.parent / "selected.txt"
        archives.extract_member(path, ".zip", seen[-1], destination)
        self.assertEqual(destination.read_text(), "synthetic")

    async def test_real_sdk_normalizes_backend_error_without_exposing_original(self):
        original = self.service.ctx
        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["args"]["args"]["action_name"] == "get_login_info":
                return {"success": True, "result": {"status": "ok", "data": {"user_id": "789"}}}
            return {"success": False, "error": "packetBackend https://example.invalid/secret"}
        self.service.ctx = PluginContext("neko.files", rpc_call=rpc)
        try:
            with self.assertRaises(FileError) as failure:
                await self.service.api(scope(), "get_group_file_url", {})
            self.assertIn("后端不可用", str(failure.exception))
            self.assertNotIn("secret", str(failure.exception))
        finally:
            self.service.ctx = original

    async def test_keyword_range_and_two_level_next_call(self):
        item, _ = await self.cached()
        document = {"kind": "lines", "units": [
            {"label": str(n), "text": "hit" * 1000} for n in range(30)
        ]}
        selected = window(document, start=2, end=3, keyword="hit")
        self.assertEqual(selected["match_count"], 2)
        self.assertNotIn("[0]", selected["text"])
        self.assertNotIn("[3]", selected["text"])
        with patch.object(module, "worker", AsyncMock(return_value=document)):
            result = await self.service.read(envelope(), item["ref"], keyword="hit")
            self.assertEqual(result["next_call"]["offset"], result["next_offset"])
            self.assertEqual(result["next_call"]["match_offset"], 0)

    async def test_explicit_range_stops_but_default_range_pages_after_character_tail(self):
        item, _ = await self.cached()
        self.config.limits.output_chars = 500
        document = {"kind": "lines", "units": [
            {"label": str(n), "text": "x" * 10} for n in range(1, 131)
        ]}
        with patch.object(module, "worker", AsyncMock(return_value=document)):
            for end in [2, 100, 0]:
                arguments = {"ref": item["ref"], "start": 1, "end": end}
                parts, starts = [], []
                while True:
                    self.service.rates.clear()
                    result = await self.service.read(envelope(), **arguments)
                    self.assertTrue(result["success"], result)
                    parts.append(result["text"])
                    starts.append(arguments["start"])
                    visible = json.loads(result["content"])
                    self.assertIn("next_call", visible)
                    self.assertIn("parse_truncated", visible)
                    self.assertIn("output_truncated", visible)
                    self.assertNotIn("match_count", visible)
                    self.assertIn("match_count", result)  # structured compatibility
                    if result["next_call"] is None:
                        self.assertIsNone(result["next_start"])
                        if end:
                            self.assertIn("指定范围已读完", visible["instruction"])
                        break
                    following = dict(result["next_call"])
                    self.assertEqual(following.pop("tool"), "neko_files_read")
                    self.assertEqual(following.pop("file_ref"), item["ref"])
                    if result["output_truncated"]:
                        self.assertEqual((following["start"], following["end"]),
                                         (arguments["start"], arguments["end"]))
                    arguments = {"ref": item["ref"], **following}
                expected = window(document, end=end or 130, limit=100000)["text"]
                self.assertEqual("".join(parts), expected)
                self.assertEqual(max(starts), 101 if end == 0 else 1)

    async def test_archive_member_keyword_continuation_preserves_range_and_batch(self):
        item = await self.register("search.zip", b"")
        path = self.service.cache.path(item["ref"], ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("notes.txt", "\n".join("hit " + "x" * 90 for _ in range(35)))
        await self.service.cache.commit(item["ref"], ".zip")

        async def parser(source, ext, operation="parse", **options):
            if operation == "archive-read":
                archives.extract_member(source, ext, options["member"], Path(options["destination"]))
                return {"extracted": True}
            return archives.catalog(source, ext) if operation == "archive-list" else worker.parse(source, ext)

        self.config.limits.output_chars = 500
        with patch.object(module, "worker", side_effect=parser):
            member = await self.service.read_archive(envelope(), item["ref"], "notes.txt")
            self.assertTrue(member["success"], member)
            arguments = {"ref": member["member_file_ref"], "start": 2, "end": 31, "keyword": "hit"}
            offsets = set()
            for _ in range(30):
                self.service.rates.clear()
                result = await self.service.read(envelope(), **arguments)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["match_count"], 30)
                self.assertEqual(result["file"]["parent"], item["ref"])
                offsets.add(result["match_offset"])
                following = result["next_call"]
                if following is None:
                    self.assertIn("指定范围已读完", result["instruction"])
                    break
                self.assertEqual((following["start"], following["end"], following["keyword"]), (2, 31, "hit"))
                self.assertEqual(following["file_ref"], member["member_file_ref"])
                if result["output_truncated"]:
                    self.assertEqual(following["match_offset"], result["match_offset"])
                else:
                    self.assertEqual(following["offset"], 0)
                    self.assertEqual(following["match_offset"], 20)
                arguments = {"ref": following["file_ref"], **{k: v for k, v in following.items() if k not in {"tool", "file_ref"}}}
            else:
                self.fail("member keyword continuation did not finish")
            self.assertEqual(offsets, {0, 20})

    async def test_invalid_ocr_options_rejected_before_render_admission_or_model(self):
        self.config.access.ocr = True
        item, _ = await self.cached("page.pdf", b"synthetic")
        document = {"kind": "pages", "units": [{"label": "1", "text": ""}]}
        with patch.object(module, "worker", AsyncMock(return_value=document)), patch.object(ocr, "worker", AsyncMock()) as render:
            for overrides in [{"end": 0}, {"end": 2}, {"offset": -1}, {"sheet": "Sheet1"},
                              {"keyword": "hit"}, {"match_offset": 1}]:
                result = await self.service.read(envelope(), item["ref"], **{
                    "start": 1, "end": 1, "use_ocr": True, **overrides,
                })
                self.assertFalse(result["success"], result)
            render.assert_not_awaited()
        self.ctx.llm.generate.assert_not_awaited()
        with self.service.store.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ocr_attempts").fetchone()[0], 0)

    def test_real_sdk_schema_contains_operational_hints(self):
        components = collect_components(NekoFiles())
        metadata = {c["name"]: c["metadata"] for c in components}
        # Only fields actually consumed by Host; deliberately exclude details.
        params = {p["name"]: p["description"]
                  for p in metadata["neko_files_read"]["parameters"]}
        self.assertIn("start=end", params["ocr"])
        self.assertIn("配置模型", params["ocr"])
        self.assertIn("sheet/keyword/match_offset", params["ocr"])
        self.assertIn("不是搜索起点", params["offset"])
        self.assertIn("清零", params["match_offset"])
        self.assertIn("member_file_ref", metadata["neko_files_archive_read"]["description"])
