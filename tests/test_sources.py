"""Real SDK capability normalization; only bottom RPC is synthetic."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import asyncio
import copy
import json
import time
import unittest
import zipfile

from maibot_sdk.context import PluginContext, PluginPaths

from neko_files import archives, service as module, sources, worker
from neko_files.authorization import FileError
from neko_files.config import Config
from neko_files.service import FileService
from tests.helpers import envelope, scope


def host_message(ident="1001", **changes):
    value = {
        "message_id": ident, "session_id": "a", "platform": "qq", "account_id": "789",
        "message_info": {"group_info": {"group_id": "123"}, "user_info": {"user_id": "111"}},
        "raw_message": [{"type": "file", "data": {"file_id": "platform1", "name": "one.txt", "size": "9"}}],
    }
    value.update(changes)
    return value


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.calls = []
        self.messages = {"1001": host_message()}
        self.raw = {"message_id": 1001, "message_type": "group", "group_id": 123,
                    "message": [{"type": "file", "data": {
                        "name": "one.txt", "size": "9", "file_id": "platform1"}}]}
        self.login = "789"
        self.file_url = "https://example.invalid/resolved"
        self.allow_message = True
        self.entered = self.release = None

        async def rpc(method, plugin_id, payload, **kwargs):
            self.assertEqual((method, plugin_id), ("cap.call", "neko.files"))
            capability, args = payload["capability"], payload["args"]
            self.calls.append((capability, copy.deepcopy(args)))
            if capability == "chat.get_all_streams":
                return {"success": True, "streams": [
                    {"session_id": sid, "stream_id": sid, "platform": "qq", "account_id": account,
                     "chat_type": "group" if group else "private", "is_group_session": bool(group),
                     "group_id": group, "user_id": "111"}
                    for sid, group, account in [("a", "123", "789"), ("b", "456", "789"),
                                                ("p", "", "789"), ("wrong", "123", "987")]
                ]}
            if capability == "message.get_by_id":
                if not self.allow_message:
                    raise PermissionError("synthetic capability denied")
                self.assertEqual(args["chat_id"], "a")
                self.assertFalse(args["include_binary_data"])
                if self.entered:
                    self.entered.set()
                    await self.release.wait()
                return {"success": True, "message": copy.deepcopy(self.messages.get(args["message_id"]))}
            if capability == "api.call":
                self.assertEqual(args["api_name"], "adapter.napcat.action.call")
                action = args["args"]["action_name"]
                if action == "get_login_info":
                    result = {"user_id": self.login}
                elif action == "get_msg":
                    result = copy.deepcopy(self.raw)
                elif action in ["get_group_file_url", "get_private_file_url"]:
                    result = {"url": self.file_url}
                else:
                    raise AssertionError("No download/model/catalog action allowed")
                return {"success": True, "result": {"status": "ok", "data": result}}
            raise AssertionError("Unexpected capability: " + capability)

        self.ctx = PluginContext("neko.files", rpc_call=rpc,
                                 paths=PluginPaths(data_dir=root, runtime_dir=root / "runtime"))
        self.config = Config()
        self.config.plugin.enabled = True
        self.config.access.group_ids = ["123", "456"]
        self.config.access.account_id = "789"
        self.config.limits.reads_per_minute = 20
        self.service = FileService()
        await self.service.start(self.ctx, self.config)
        self.addAsyncCleanup(self.service.stop)

    async def lookup(self, ident="1001", **kwargs):
        return await self.service.list_files(envelope(), message_id=ident, **kwargs)

    async def test_real_sdk_raw_file_string_size_registers_without_download(self):
        result = await self.lookup()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["files"][0]["size"], 9)
        self.assertTrue(result["files"][0]["source_verified"])
        self.assertEqual(result["files"][0]["message_id"], "1001")
        self.assertEqual([cap for cap, _ in self.calls],
                         ["chat.get_all_streams", "chat.get_all_streams", "message.get_by_id"])

    async def test_notice_and_one_hop_reply_no_raw_adapter_needed(self):
        self.messages["1002"] = host_message("1002", raw_message=[], reply_to="1001")
        result = await self.lookup("1002")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["files"][0]["message_id"], "1001")
        self.assertFalse(any(cap == "api.call" for cap, _ in self.calls))
        self.messages["1001"] = host_message(raw_message=[], message_info={
            "group_info": {"group_id": "123"}, "user_info": {"user_id": "111"},
            "additional_config": {"napcat_notice_type": "group_upload", "napcat_notice_payload": {
                "group_id": 123, "file": {"file_id": "notice1", "name": "up.txt", "size": "2", "busid": 1}}}})
        self.assertTrue((await self.lookup())["success"])

    async def test_raw_reply_target_and_chain_or_forward_not_traversed(self):
        self.messages["1002"] = host_message("1002", raw_message=[{
            "type": "reply", "data": {"target_message_id": "1001"}}])
        self.assertTrue((await self.lookup("1002"))["success"])
        self.messages["1001"] = host_message(raw_message=[], reply_to="1003")
        self.calls.clear()
        self.assertFalse((await self.lookup("1002"))["success"])
        self.assertNotIn("1003", str(self.calls))
        self.messages["1001"] = host_message(raw_message=[{
            "type": "forward", "data": [{"type": "file", "data": {"name": "nested.txt"}}]}])
        self.raw["message"] = []
        self.assertFalse((await self.lookup())["success"])

    async def test_adapter_reply_is_one_hop_and_hook_raw_file_is_shared(self):
        self.messages["1002"] = host_message("1002", raw_message=[])
        self.raw = {"message_id": 1002, "message_type": "group", "group_id": 123,
                    "message": [{"type": "reply", "data": {"id": "1001"}}]}
        result = await self.lookup("1002")
        self.assertTrue(result["success"], result)
        ref = result["files"][0]["ref"]
        received = await self.service.receive(host_message(), {})
        self.assertEqual(received, {"action": "continue"})
        self.assertEqual((await self.service.db("recent", scope()))[0]["ref"], ref)
        self.assertEqual(len(await self.service.db("recent", scope())), 1)

    async def test_private_sources_require_separate_grant_and_target_proof(self):
        env = envelope("p", "")
        env["user_id"] = "111"
        self.messages["1001"] = host_message(session_id="p", message_info={
            "group_info": None, "user_info": {"user_id": "111"}})
        self.assertFalse((await self.service.list_files(env, message_id="1001"))["success"])
        # Synthetic instance only; no real deployment configuration is read or changed.
        self.config.access.user_ids = ["111"]
        original = self.ctx._rpc_call

        async def private_rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] == "message.get_by_id":
                self.assertEqual(payload["args"]["chat_id"], "p")
                return {"success": True, "message": copy.deepcopy(self.messages["1001"])}
            return await original(method, plugin_id, payload, **kwargs)

        # Replace the bottom RPC only, keep the actual SDK capability objects.
        self.ctx._rpc_call = private_rpc
        self.assertTrue((await self.service.list_files(env, message_id="1001"))["success"])
        self.messages["1001"]["message_info"]["user_info"]["user_id"] = "222"
        self.assertFalse((await self.service.list_files(env, message_id="1001"))["success"])

    async def test_sdk_missing_denied_and_host_envelope_fail_closed(self):
        for value in [None, {"success": True, "message": host_message()}, {"success": False}]:
            self.messages["1001"] = value
            self.assertFalse((await self.lookup())["success"])
        self.allow_message = False
        self.assertFalse((await self.lookup())["success"])
        self.assertFalse(any(cap == "api.call" for cap, _ in self.calls))
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_invalid_id_mutually_exclusive_modes_no_message_rpc(self):
        for ident in ["mabcdef", "https://example.invalid/a", "/tmp/file", "x " * 200]:
            self.assertFalse((await self.lookup(ident))["success"])
        for args in [{"include_group": True}, {"folder_id": "one"}, {"cursor": "one"}]:
            self.assertFalse((await self.lookup(**args))["success"])
        self.assertTrue(all(cap == "chat.get_all_streams" for cap, _ in self.calls))

    async def test_snowluma_group_uuid_is_not_a_local_path_or_private_file_id(self):
        ident='/12345678-1234-5678-9abc-1234567890ab'
        raw={'file_id':ident,'name':'one.txt','size':9}
        self.assertEqual(sources.attachment(raw,'catalog',0,'catalog')['file_id'],ident)
        self.assertEqual(sources.attachment(raw,'1001',0,group_file=True)['file_id'],ident)
        with self.assertRaises(FileError):
            sources.attachment(raw,'1001',0)
        for invalid in ['/etc/shadow',ident+'/../secret',ident+'.txt',ident+'/',
                        '/12345678-1234-5678-9abc-1234567890az','file:'+ident,'\\'+ident[1:]]:
            with self.assertRaises(FileError):
                sources.attachment({**raw,'file_id':invalid},'catalog',0,'catalog')
        self.messages['1001']['raw_message'][0]['data']['file_id']=ident
        result=await self.lookup()
        self.assertTrue(result['success'],result)
        self.assertTrue(result['files'][0]['source_verified'])
        self.assertNotIn(ident,str(result))

    async def test_snowluma_private_hash_survives_registration_and_is_not_model_visible(self):
        self.config.access.user_ids = ['111']
        original = self.ctx._rpc_call
        message = host_message(session_id='p', message_info={'group_info': None, 'user_info': {'user_id': '111'}})
        message['raw_message'][0]['data']['file_hash'] = 'OPAQUE_PRIVATE_HASH'
        actions = []

        async def private_rpc(method, plugin_id, payload, **kwargs):
            cap, args = payload['capability'], payload['args']
            if cap == 'message.get_by_id':
                self.assertEqual(args['chat_id'], 'p')
                return {'success': True, 'message': copy.deepcopy(message)}
            if cap == 'api.call':
                action = args['args']['action_name']
                actions.append(action)
                if action == 'get_private_file_url':
                    self.assertEqual(args['args']['params'], {'user_id':'111', 'file_id':'platform1',
                                                             'file_hash':'OPAQUE_PRIVATE_HASH'})
                    return {'success': True, 'result': {'status':'ok', 'data':{'url':'https://example.invalid/private'}}}
                if action == 'get_msg':
                    detail = {'message_id':1001, 'message_type':'private', 'sender':{'user_id':111},
                              'message':[{'type':'file','data':copy.deepcopy(message['raw_message'][0]['data'])}]}
                    return {'success':True, 'result':{'status':'ok','data':detail}}
            return await original(method, plugin_id, payload, **kwargs)

        self.ctx._rpc_call = private_rpc
        env = envelope('p', '')
        env['user_id'] = '111'
        listed = await self.service.list_files(env, message_id='1001')
        self.assertTrue(listed['success'], listed)
        self.assertNotIn('OPAQUE_PRIVATE_HASH', str(listed))

        async def synthetic_download(url, destination, limit, expected):
            self.assertEqual(url, 'https://example.invalid/private')
            destination.write_bytes(b'synthetic')

        with patch.object(module, 'download', side_effect=synthetic_download), patch.object(
            module, 'worker', return_value={'kind':'lines','units':[{'label':'1','text':'synthetic'}]}
        ):
            read = await self.service.read(env, listed['files'][0]['ref'])
        self.assertTrue(read['success'], read)
        self.assertNotIn('OPAQUE_PRIVATE_HASH', str(read))
        self.assertNotIn('get_msg', actions)

    async def test_file_hash_conflicts_and_malformed_values_preserve_old_registry(self):
        raw = host_message()['raw_message'][0]['data']
        for bad in [True, False, None, 'x'*1025, 'bad hash', 'bad\nhash']:
            with self.assertRaises(FileError):
                sources.attachment({**raw, 'file_hash':bad}, '1001', 0)
        source = sources.attachment({**raw, 'file_hash':'original-hash'}, '1001', 0)
        item = await self.service.db('register', scope(), source, '1001')
        self.assertTrue(await self.service.db('admit_ocr', scope(), item['ref'], 1))
        changed = {**source, 'file_hash':'changed-hash'}
        with self.assertRaises(FileError):
            await self.service.db('register', scope(), changed, '1001')
        saved = await self.service.db('get', scope(), item['ref'])
        self.assertEqual(saved['file_hash'], 'original-hash')
        self.assertFalse(await self.service.db('admit_ocr', scope(), item['ref'], 1))

    async def test_group_busid_missing_zero_and_real_value_use_platform_default_correctly(self):
        for index, busid in enumerate([None, 0, 102]):
            with self.subTest(busid=busid):
                data = self.messages["1001"]["raw_message"][0]["data"]
                data["file_id"] = f"platform-{index}"
                data.pop("busid", None)
                if busid is not None:
                    data["busid"] = busid
                listed = await self.lookup()
                self.assertTrue(listed["success"], listed)
                self.calls.clear()

                async def download(url, destination, limit, expected):
                    self.assertEqual(url, self.file_url)
                    destination.write_bytes(b"synthetic")

                with patch.object(module, "download", side_effect=download), patch.object(
                    module, "worker", return_value={"kind": "lines", "units": []}
                ):
                    result = await self.service.read(envelope(), listed["files"][0]["ref"])
                self.assertTrue(result["success"], result)
                actions = [args["args"] for cap, args in self.calls if cap == "api.call"]
                requested = next(a["params"] for a in actions if a["action_name"] == "get_group_file_url")
                self.assertEqual(requested, {"group_id": "123", "file_id": f"platform-{index}",
                                             **({"busid": busid} if busid else {})})

    async def test_private_url_message_refresh_uses_hash_and_file_action_not_stored_url(self):
        self.config.access.user_ids = ["111"]
        current = host_message(session_id="p", message_info={
            "group_info": None, "user_info": {"user_id": "111"}})
        current["raw_message"][0]["data"]["url"] = "https://example.invalid/expired"
        original = self.ctx._rpc_call
        actions = []

        async def rpc(method, plugin_id, payload, **kwargs):
            cap, args = payload["capability"], payload["args"]
            if cap == "message.get_by_id":
                self.assertEqual(args["chat_id"], "p")
                return {"success": True, "message": copy.deepcopy(current)}
            if cap == "api.call":
                action = args["args"]["action_name"]
                actions.append(action)
                if action == "get_msg":
                    data = {**current["raw_message"][0]["data"], "file_hash": "SYNTHETIC_HASH"}
                    return {"success": True, "result": {"status": "ok", "data": {
                        "message_id": 1001, "message_type": "private", "sender": {"user_id": 111},
                        "message": [{"type": "file", "data": data}]}}}
                if action == "get_private_file_url":
                    self.assertEqual(args["args"]["params"], {"user_id": "111", "file_id": "platform1",
                                                            "file_hash": "SYNTHETIC_HASH"})
                    return {"success": True, "result": {"status": "ok", "data": {"url": self.file_url}}}
            return await original(method, plugin_id, payload, **kwargs)

        self.ctx._rpc_call = rpc
        env = envelope("p", "")
        env["user_id"] = "111"
        listed = await self.service.list_files(env, message_id="1001")
        self.assertTrue(listed["success"], listed)

        async def download(url, destination, limit, expected):
            self.assertEqual(url, self.file_url)
            destination.write_bytes(b"synthetic")

        with patch.object(module, "download", side_effect=download), patch.object(
            module, "worker", return_value={"kind": "lines", "units": []}
        ):
            result = await self.service.read(env, listed["files"][0]["ref"])
        self.assertTrue(result["success"], result)
        self.assertEqual(actions.count("get_msg"), 1)
        self.assertEqual(actions[-1], "get_private_file_url")
        self.assertNotIn("SYNTHETIC_HASH", json.dumps(result))

    async def test_url_refresh_without_busid_preserves_previously_verified_real_busid(self):
        self.messages["1001"]["raw_message"][0]["data"].update(
            busid=102, url="https://example.invalid/expired")
        ref = (await self.lookup())["files"][0]["ref"]
        # SnowLuma get_msg ordinary file data has no busid even when a previous
        # scope-proven catalog/notice registration had the real value.
        self.raw["message"][0]["data"]["url"] = "https://example.invalid/expired"
        self.calls.clear()

        async def download(url, destination, limit, expected):
            self.assertEqual(url, self.file_url)
            destination.write_bytes(b"synthetic")

        with patch.object(module, "download", side_effect=download), patch.object(
            module, "worker", return_value={"kind": "lines", "units": []}
        ):
            result = await self.service.read(envelope(), ref)
        self.assertTrue(result["success"], result)
        actions = [args["args"] for cap, args in self.calls if cap == "api.call"]
        requested = next(a["params"] for a in actions if a["action_name"] == "get_group_file_url")
        self.assertEqual(requested["busid"], 102)
        self.assertEqual((await self.service.db("get", scope(), ref))["busid"], 102)

    async def test_stable_url_refresh_hash_conflict_refuses_before_url_action(self):
        data = self.messages["1001"]["raw_message"][0]["data"]
        data.update(url="https://example.invalid/expired", file_hash="original-hash")
        ref = (await self.lookup())["files"][0]["ref"]
        before = await self.service.db("get", scope(), ref)
        self.raw["message"][0]["data"].update(file_hash="changed-hash")
        self.calls.clear()
        with patch.object(module, "download") as download:
            result = await self.service.read(envelope(), ref)
        self.assertFalse(result["success"])
        self.assertIn("校验来源发生变化", result["content"])
        self.assertEqual(await self.service.db("get", scope(), ref), before)
        actions = [a["args"]["action_name"] for cap, a in self.calls if cap == "api.call"]
        self.assertNotIn("get_group_file_url", actions)
        download.assert_not_awaited()

    async def test_url_action_revoke_blocks_download_without_bypassing_check(self):
        ref = (await self.lookup())["files"][0]["ref"]
        original = self.ctx._rpc_call

        async def rpc(method, plugin_id, payload, **kwargs):
            result = await original(method, plugin_id, payload, **kwargs)
            if (payload["capability"] == "api.call"
                    and payload["args"]["args"]["action_name"] == "get_group_file_url"):
                self.config.access.group_ids = []
            return result

        self.ctx._rpc_call = rpc
        with patch.object(module, "download") as download:
            result = await self.service.read(envelope(), ref)
        self.assertFalse(result["success"])
        download.assert_not_awaited()

    async def test_message_scope_fields_and_private_account_deny(self):
        for changes in [{"message_id": "wrong"}, {"session_id": "b"}, {"platform": "other"},
                        {"account_id": "987"}, {"account_id": ""},
                        {"message_info": {"group_info": {"group_id": "456"}}},
                        {"message_info": {"user_info": {"user_id": "111"}}}]:
            self.messages["1001"] = host_message(**changes)
            self.assertFalse((await self.lookup())["success"])
        for env in [None, envelope("p", ""), envelope("wrong", "123")]:
            self.assertFalse((await self.service.list_files(env, message_id="1001"))["success"])
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_adapter_fallback_requires_id_target_and_actual_login(self):
        self.messages["1001"] = host_message(raw_message=[], processed_plain_text="flattened")
        self.assertTrue((await self.lookup())["success"])
        for changes in [{"message_id": 1002}, {"group_id": 456}, {"group_id": None},
                        {"message_type": "private"}]:
            original = self.raw
            self.raw = {**original, **changes}
            self.assertFalse((await self.lookup())["success"])
            self.raw = original
        self.login = "987"
        self.calls.clear()
        self.assertFalse((await self.lookup())["success"])
        actions = [args["args"]["action_name"] for cap, args in self.calls if cap == "api.call"]
        self.assertEqual(actions, ["get_login_info"])

    async def test_size_and_attachment_limits_fail_atomically(self):
        for size in [True, False, -1, "-1", "1.2", "9" * 1000, 40 * 1024**2 + 1]:
            bad = host_message()
            bad["raw_message"][0]["data"]["size"] = size
            self.messages["1001"] = bad
            self.assertFalse((await self.lookup())["success"], size)
        self.assertEqual(await self.service.db("recent", scope()), [])
        self.messages["1001"] = host_message(raw_message=host_message()["raw_message"] * 9)
        self.assertFalse((await self.lookup())["success"])

    async def test_cancel_and_revoke_during_source_query_do_not_register(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.lookup())
        await self.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.entered.clear()
        task = asyncio.create_task(self.lookup())
        await self.entered.wait()
        self.config.access.group_ids = []
        self.release.set()
        self.assertFalse((await task)["success"])
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_queue_revoke_blocks_before_message_lookup(self):
        await self.service.slot.acquire()
        await self.service.slot.acquire()
        task = asyncio.create_task(self.lookup())
        for _ in range(100):
            if self.calls:
                break
            await asyncio.sleep(0)
        self.config.access.group_ids = []
        self.service.slot.release()
        self.service.slot.release()
        self.assertFalse((await task)["success"])
        self.assertTrue(all(cap == "chat.get_all_streams" for cap, _ in self.calls))

    async def revoke_registry_waiter(self, operation, *, hold_after_get_msg=False):
        """Observe a real wait on the existing lock, not a simulated db helper."""
        lock = self.service.registry_lock
        acquire = lock.acquire
        waiting = asyncio.Event()
        if not hold_after_get_msg:
            await acquire()
        else:
            original_rpc = self.ctx._rpc_call

            async def rpc(method, plugin_id, payload, **kwargs):
                result = await original_rpc(method, plugin_id, payload, **kwargs)
                if (
                    payload["capability"] == "api.call"
                    and payload["args"]["args"]["action_name"] == "get_msg"
                ):
                    # Initial read-helper calls are already finished; hold only the refresh write.
                    await acquire()
                return result

            self.ctx._rpc_call = rpc

        async def observed_acquire():
            if lock.locked():
                waiting.set()
            return await acquire()

        with patch.object(lock, "acquire", side_effect=observed_acquire):
            task = asyncio.create_task(operation())
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                self.assertFalse(task.done(), "Operation must still be waiting for registry_lock")
                self.config.access.group_ids = []
                lock.release()
                return await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if lock.locked():
                    lock.release()

    async def test_list_registry_wait_revoke_prevents_source_commit(self):
        result = await self.revoke_registry_waiter(self.lookup)
        self.assertFalse(result["success"])
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_receive_registry_wait_revoke_prevents_source_commit(self):
        result = await self.revoke_registry_waiter(
            lambda: self.service.receive(host_message(), {})
        )
        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_refresh_registry_wait_revoke_preserves_original_metadata(self):
        self.messages["1001"]["raw_message"][0]["data"]["url"] = "https://example.invalid/old"
        ref = (await self.lookup())["files"][0]["ref"]
        before = await self.service.db("get", scope(), ref)
        self.raw["message"][0]["data"]["url"] = "https://example.invalid/new"
        with patch.object(module, "download") as download, patch.object(module, "worker") as parser:
            result = await self.revoke_registry_waiter(
                lambda: self.service.read(envelope(), ref), hold_after_get_msg=True
            )
        self.assertFalse(result["success"])
        self.assertEqual(await self.service.db("get", scope(), ref), before)
        download.assert_not_awaited()
        parser.assert_not_awaited()

    async def test_unload_cancels_inflight_source_without_registering(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.lookup())
        await self.entered.wait()
        await self.service.stop()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.ready)
        self.assertFalse(self.service.tasks)
        self.assertEqual(await self.service.db("recent", scope()), [])

    async def test_verified_refresh_preserves_ref_ttl_attempts_and_cache(self):
        result = await self.lookup()
        ref = result["files"][0]["ref"]
        self.assertTrue(await self.service.db("admit_ocr", scope(), ref, 1))
        cache = self.service.cache.path(ref, ".txt")
        cache.write_bytes(b"synthetic")
        await self.service.cache.commit(ref, ".txt")
        with self.service.store.connect() as con:
            created = con.execute("SELECT created FROM files WHERE id=?", (ref,)).fetchone()[0]
        self.messages["1002"] = host_message("1002")
        result = await self.lookup("1002")
        self.assertEqual(result["files"][0]["ref"], ref)
        self.assertEqual(result["files"][0]["message_id"], "1002")
        with self.service.store.connect() as con:
            self.assertEqual(con.execute("SELECT created FROM files WHERE id=?", (ref,)).fetchone()[0], created)
        self.assertFalse(await self.service.db("admit_ocr", scope(), ref, 1))
        self.assertEqual(cache.read_bytes(), b"synthetic")
        self.messages["1002"]["raw_message"][0]["data"]["size"] = "10"
        self.assertFalse((await self.lookup("1002"))["success"])
        self.assertEqual(cache.read_bytes(), b"synthetic")

    async def test_url_only_refresh_from_proven_adapter_and_legacy_url_explanation(self):
        self.messages["1001"]["raw_message"][0]["data"] = {
            "name": "one.txt", "size": "9", "url": "https://example.invalid/expired"}
        self.raw["message"][0]["data"] = {
            "name": "one.txt", "size": "9", "url": "https://example.invalid/fresh"}
        result = await self.lookup()
        ref = result["files"][0]["ref"]
        self.assertEqual((await self.service.db("get", scope(), ref))["file_id"], "")

        async def fake_download(url, destination, limit, expected):
            self.assertEqual(url, "https://example.invalid/fresh")
            destination.write_bytes(b"synthetic")

        with patch.object(module, "download", side_effect=fake_download), patch.object(
            module, "worker", return_value={"kind": "lines", "units": [{"label": "1", "text": "synthetic"}]}
        ):
            self.assertTrue((await self.service.read(envelope(), ref))["success"])
        old = await self.service.db("register", scope(), {
            "file_id": "https://example.invalid/legacy", "name": "old.txt", "size": 0})
        self.calls.clear()
        result = await self.service.read(envelope(), old["ref"])
        self.assertFalse(result["success"])
        self.assertIn("真实ID", result["content"])
        self.assertTrue(all(cap == "chat.get_all_streams" for cap, _ in self.calls))

    async def test_text_candidate_hook_expires_without_claiming_verified_source(self):
        message = host_message(raw_message=[], processed_plain_text=(
            "[文件] candidate.txt，大小: 9，链接: https://example.invalid/synthetic"))
        result = await self.service.receive(message, {})
        self.assertEqual(result, {"action": "continue"})
        item = (await self.service.db("recent", scope()))[0]
        self.assertEqual(item["file_id"], "")
        self.assertEqual(item["source"]["kind"], "text_candidate")
        listed = await self.service.list_files(envelope())
        self.assertNotIn("https://", listed["content"])
        self.assertFalse(listed["files"][0]["source_verified"])
        with self.service.store.connect() as con:
            item["url_seen"] = time.time() - 601
            con.execute("UPDATE files SET data=? WHERE id=?", (json.dumps(item), item["ref"]))
        result = await self.service.inspect(envelope(), item["ref"])
        self.assertFalse(result["success"])
        self.assertIn("过期", result["content"])

    async def test_stable_resource_and_short_url_separate_and_refresh_identity_checked(self):
        data = self.messages["1001"]["raw_message"][0]["data"]
        data["url"] = "https://example.invalid/old-signed"
        result = await self.lookup()
        ref = result["files"][0]["ref"]
        stored = await self.service.db("get", scope(), ref)
        self.assertEqual(stored["file_id"], "platform1")
        self.assertNotIn("https://", json.dumps(result))
        self.raw["message"][0]["data"]["file_id"] = "different-platform-id"
        result = await self.service.inspect(envelope(), ref)
        self.assertFalse(result["success"])
        self.assertIn("发生变化", result["content"])
        self.assertEqual((await self.service.db("get", scope(), ref))["file_id"], "platform1")
        self.raw["message"][0]["data"].update(
            file_id="platform1", url="https://example.invalid/new-signed"
        )

        async def synthetic_download(url, destination, limit, expected):
            self.assertEqual(url, self.file_url)
            destination.write_bytes(b"synthetic")

        with patch.object(module, "download", side_effect=synthetic_download), patch.object(
            module, "worker", return_value={"kind": "lines", "units": [{"label": "1", "text": "synthetic"}]}
        ):
            result = await self.service.read(envelope(), ref)
        self.assertTrue(result["success"], result)
        stored = await self.service.db("get", scope(), ref)
        self.assertEqual(stored["file_id"], "platform1")
        self.assertEqual(stored["url"], "https://example.invalid/new-signed")

    async def test_url_only_same_old_link_cannot_renew_age_via_list_or_get_msg(self):
        data = {"name": "one.txt", "size": "9", "url": "https://example.invalid/old"}
        self.messages["1001"]["raw_message"][0]["data"] = copy.deepcopy(data)
        self.raw["message"][0]["data"] = copy.deepcopy(data)
        ref = (await self.lookup())["files"][0]["ref"]
        for age in [time.time() - 601, None]:
            with self.service.store.connect() as con:
                item = json.loads(con.execute("SELECT data FROM files WHERE id=?", (ref,)).fetchone()[0])
                item["url_seen"] = age
                con.execute("UPDATE files SET data=? WHERE id=?", (json.dumps(item), ref))
            self.assertTrue((await self.lookup())["success"])
            with patch.object(module, "download") as download:
                result = await self.service.read(envelope(), ref)
            self.assertFalse(result["success"])
            self.assertIn("旧链接", result["content"])
            self.assertEqual((await self.service.db("get", scope(), ref))["url_seen"], age)
            download.assert_not_awaited()

    async def test_url_only_refresh_scope_slot_and_name_changes_fail_without_download(self):
        data = {"name": "one.txt", "size": "9", "url": "https://example.invalid/source"}
        self.messages["1001"]["raw_message"][0]["data"] = copy.deepcopy(data)
        self.raw["message"][0]["data"] = copy.deepcopy(data)
        ref = (await self.lookup())["files"][0]["ref"]
        original = copy.deepcopy(self.raw)
        for mutation in ("group", "slot", "name"):
            self.raw = copy.deepcopy(original)
            if mutation == "group":
                self.raw["group_id"] = 456
            elif mutation == "slot":
                self.raw["message"] = []
            else:
                self.raw["message"][0]["data"]["name"] = "changed.txt"
            with patch.object(module, "download") as download:
                result = await self.service.read(envelope(), ref)
            self.assertFalse(result["success"], result)
            download.assert_not_awaited()

    async def test_candidate_cannot_poison_verified_url_only_cache_identity(self):
        message = host_message(raw_message=[], processed_plain_text=(
            "[文件] one.txt，大小: 9，链接: https://example.invalid/candidate"))
        await self.service.receive(message, {})
        candidate = (await self.service.db("recent", scope()))[0]
        self.messages["1001"]["raw_message"][0]["data"] = {
            "name": "one.txt", "size": "9", "url": "https://example.invalid/proven"}
        result = await self.lookup()
        self.assertTrue(result["success"])
        self.assertNotEqual(result["files"][0]["ref"], candidate["ref"])
        self.assertTrue(result["files"][0]["source_verified"])

    async def test_url_only_archive_children_stay_parent_scoped(self):
        refs = []

        async def local_worker(path, ext, operation="parse", **options):
            if operation == "archive-list":
                return archives.catalog(path, ext, options["name"])
            if operation == "archive-read":
                archives.extract_member(path, ext, options["member"], Path(options["destination"]),
                                        options["name"], options["limit"])
                return {"extracted": True}
            return worker.parse(path, ext)

        with patch.object(module, "worker", side_effect=local_worker):
            for ident in ["1001", "1002"]:
                parent = await self.service.db("register", scope(), sources.attachment({
                    "name": "archive.zip", "url": "https://example.invalid/" + ident}, ident, 0), ident)
                path = self.service.cache.path(parent["ref"], ".zip")
                with zipfile.ZipFile(path, "w") as package:
                    package.writestr("one.txt", "synthetic " + ident)
                await self.service.cache.commit(parent["ref"], ".zip")
                result = await self.service.read_archive(envelope(), parent["ref"], "one.txt")
                self.assertTrue(result["success"], result)
                self.assertIn(ident, result["preview"]["text"])
                refs.append(result["member_file_ref"])
        self.assertNotEqual(*refs)


class ScopeHelpers(unittest.TestCase):
    def test_private_adapter_proof_and_normalization(self):
        c = {**scope(), "group": "", "user": "111"}
        sources.adapter_scope({"message_id": 1, "message_type": "private", "sender": {"user_id": 111}}, c, "1")
        with self.assertRaises(FileError):
            sources.adapter_scope({"message_id": 1, "message_type": "private", "sender": {"user_id": 222}}, c, "1")
        for raw in [{"name": "x.txt", "file_ref": "official"}, {"name": "x.txt", "base64": "xxx"},
                    {"name": "x.txt", "url": "file:///private"}]:
            with self.assertRaises(FileError):
                sources.attachment(raw, "1", 0)
