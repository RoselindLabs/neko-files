from unittest.mock import AsyncMock, patch

import asyncio
import unittest

from maibot_sdk.components import collect_components
from neko_files import service as module, ocr
from neko_files.authorization import FileError
from neko_files.config import Config
from neko_files.jobs import io_call
from neko_files.plugin import NekoFiles, CONTRACT
from neko_files.worker import parse
from tests.helpers import ServiceFixture, envelope, scope


class ServiceTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_missing_trusted_context_cross_chat_and_wrong_account_fail(self):
        self.assertFalse((await self.service.list_files(None))["success"])
        item = await self.register()
        self.assertFalse((await self.service.inspect(envelope("b", "456"), item["ref"]))["success"])
        self.ctx.api.call.return_value = {"status": "ok", "data": {"user_id": "different"}}
        self.assertFalse((await self.service.inspect(envelope(), item["ref"]))["success"])
        self.assertEqual(self.ctx.api.call.await_count, 1)
        self.ctx.llm.generate.assert_not_awaited()

    async def test_cached_read_uses_verified_source_and_no_network(self):
        item, path = await self.cached(data=b"complete text")

        async def parser(path, ext, *args, **kwargs):
            return await io_call(parse, path, ext)

        with patch.object(module, "worker", side_effect=parser):
            result = await self.service.read(envelope(), item["ref"])
        self.assertTrue(result["success"])
        self.assertIn("complete text", result["text"])
        self.ctx.api.call.assert_not_awaited()
        self.ctx.llm.generate.assert_not_awaited()

    async def test_revoke_during_parse_does_not_return_content(self):
        item, path = await self.cached()
        started = asyncio.Event()
        release = asyncio.Event()

        async def parser(*args, **kwargs):
            started.set()
            await release.wait()
            return {"kind": "lines", "units": [{"label": "1", "text": "secret"}]}

        with patch.object(module, "worker", side_effect=parser):
            task = asyncio.create_task(self.service.read(envelope(), item["ref"]))
            await started.wait()
            self.config.access.group_ids = []
            release.set()
            result = await task
        self.assertFalse(result["success"])
        self.assertNotIn("secret", str(result))

    async def test_group_catalog_pages_scope_bound_snapshot_without_requery(self):
        self.config.access.group_catalog = True

        async def api(name, **kwargs):
            if kwargs["action_name"] == "get_login_info":
                return {"status": "ok", "data": {"user_id": "789"}}
            return {
                "status": "ok",
                "data": {
                    "files": [
                        {"file_id": str(n), "name": str(n) + ".txt", "size": 1} for n in range(50)
                    ]
                },
            }

        self.ctx.api.call.side_effect = api
        first = await self.service.list_files(envelope(), True)
        second = await self.service.list_files(envelope(), cursor=first["next_cursor"])
        third = await self.service.list_files(envelope(), cursor=second["next_cursor"])
        self.assertEqual([len(page["files"]) for page in [first, second, third]], [20, 20, 10])
        self.assertEqual(self.ctx.api.call.await_count, 2)
        self.assertFalse(
            (await self.service.list_files(envelope("b", "456"), cursor=first["next_cursor"]))[
                "success"
            ]
        )

    async def test_ocr_cache_continuation_and_reload_do_not_repeat_model(self):
        self.config.access.ocr = True
        item, path = await self.cached("page.pdf", b"synthetic pdf")
        self.ctx.llm.generate.side_effect = None
        self.ctx.llm.generate.return_value = {"content": "x" * 9000, "finish_reason": "stop"}
        document = {"kind": "pages", "units": [{"label": "1", "text": ""}], "needs_ocr": True}
        with (
            patch.object(module, "worker", AsyncMock(return_value=document)),
            patch.object(ocr, "worker", AsyncMock(return_value={"image_base64": "synthetic"})),
        ):
            first = await self.service.read(envelope(), item["ref"], 1, 1, use_ocr=True)
            await self.service.stop()
            await self.service.start(self.ctx, self.config)
            second = await self.service.read(
                envelope(), item["ref"], 1, 1, offset=first["next_offset"], use_ocr=True
            )
        self.assertTrue(first["success"] and second["success"])
        self.assertEqual(len(second["text"]), 1000)
        self.assertTrue(second["cache_hit"])
        self.ctx.llm.generate.assert_awaited_once()

    async def test_unknown_ocr_admission_survives_reload_and_no_result_is_invented(self):
        self.config.access.ocr = True
        item, path = await self.cached("page.pdf", b"synthetic pdf")
        self.ctx.llm.generate.side_effect = TimeoutError("synthetic unknown")
        document = {"kind": "pages", "units": [{"label": "1", "text": ""}]}
        with (
            patch.object(module, "worker", AsyncMock(return_value=document)),
            patch.object(ocr, "worker", AsyncMock(return_value={"image_base64": "synthetic"})),
        ):
            first = await self.service.read(envelope(), item["ref"], 1, 1, use_ocr=True)
            await self.service.stop()
            await self.service.start(self.ctx, self.config)
            second = await self.service.read(envelope(), item["ref"], 1, 1, use_ocr=True)
        self.assertFalse(first["success"] or second["success"])
        self.ctx.llm.generate.assert_awaited_once()

    async def test_bulk_registration_is_atomic_on_bad_metadata_and_scoped(self):
        with self.assertRaises(FileError):
            await self.service.db(
                "register_many", scope(), [{"file_id": "ok", "name": "ok.txt", "size": 1}, None]
            )
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_receive_registers_message_file_links_from_flattened_text(self):
        message = {
            "session_id": "a",
            "platform": "qq",
            "account_id": "789",
            "message_id": "m1",
            "processed_plain_text": (
                "给你文件\n[文件] report.pdf，大小: 10，链接: https://ftn.qq.com/dl/abc123\n后面"
            ),
            "message_info": {
                "group_info": {"group_id": "123"},
                "user_info": {"user_id": "111"},
                "additional_config": {},
            },
        }
        self.ctx.api.call.return_value = {"status": "ok", "data": {"user_id": "789"}}
        result = await self.service.receive(message, {})
        self.assertEqual(result, {"action": "continue"})
        self.assertNotIn("Neko Files", message["processed_plain_text"])
        files = await self.service.db("recent", scope())
        self.assertEqual([f["name"] for f in files], ["report.pdf"])
        self.assertEqual(files[0]["file_id"], "")
        self.assertTrue(files[0]["url"].startswith("https://ftn.qq.com/"))
        self.assertEqual(files[0]["source"]["kind"], "text_candidate")

    async def test_file_links_extracts_only_file_markers_with_urls(self):
        from neko_files.service import file_links

        self.assertEqual(
            file_links("[文件] a.zip，大小: 5，链接: https://x.test/f.zip"),
            [("a.zip", "https://x.test/f.zip")],
        )
        self.assertEqual(file_links("[视频] 文件: v.mp4，大小: 1"), [])
        self.assertEqual(file_links("[文件] no-link.pdf"), [])
        self.assertEqual(file_links("普通消息"), [])

    async def test_receive_still_registers_group_upload_notice_alongside_attachments(self):
        message = {
            "session_id": "a",
            "platform": "qq",
            "account_id": "789",
            "message_id": "m2",
            "message_info": {
                "group_info": {"group_id": "123"},
                "user_info": {"user_id": "111"},
                "additional_config": {
                    "napcat_notice_type": "group_upload",
                    "napcat_notice_payload": {
                        "group_id": 123,
                        "file": {"file_id": "notice1", "name": "up.zip", "size": 5, "busid": 2},
                    },
                },
            },
        }
        self.ctx.api.call.return_value = {"status": "ok", "data": {"user_id": "789"}}
        result = await self.service.receive(message, {})
        self.assertEqual(result, {"action": "continue"})
        files = await self.service.db("recent", scope())
        self.assertEqual([f["name"] for f in files], ["up.zip"])

    async def test_receive_without_attachments_or_unauthorized_scope_is_passthrough(self):
        plain = {
            "session_id": "a",
            "message_info": {"additional_config": {}, "user_info": {}},
        }
        self.assertEqual((await self.service.receive(plain, {}))["action"], "continue")
        outside = {
            "session_id": "zzz",
            "platform": "qq",
            "account_id": "789",
            "processed_plain_text": "[文件] x.txt，大小: 1，链接: https://x.test/x.txt",
            "message_info": {
                "group_info": {"group_id": "999"},
                "user_info": {"user_id": "111"},
                "additional_config": {},
            },
        }
        self.assertEqual((await self.service.receive(outside, {}))["action"], "continue")
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_disabled_plugin_starts_no_state_and_declares_only_opted_in_tools(self):
        config = Config()
        self.assertFalse(config.plugin.enabled)
        self.assertEqual(config.plugin.config_version, "1.0.0")
        tools = [item for item in collect_components(NekoFiles()) if item["type"] == "TOOL"]
        self.assertEqual(len(tools), 4)
        self.assertTrue(all(item["metadata"]["trusted_context"] == CONTRACT for item in tools))
