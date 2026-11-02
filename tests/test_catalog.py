"""Real SDK bottom-RPC regression: metadata listing is not download admission."""
import copy
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch

from maibot_sdk.context import PluginContext, PluginPaths
from neko_files.config import Config
from neko_files.service import FileService
from neko_files import service as module
from tests.helpers import envelope, scope


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.catalog = {'files': [
            {'file_id': 'wanted', 'file_name': 'target.txt', 'file_size': '20'},
            {'file_id': 'large', 'file_name': 'unrelated.zip', 'file_size': 100 * 1024**2}]}
        self.failure = None

        async def rpc(method, plugin_id, payload, **kwargs):
            self.assertEqual((method, plugin_id), ('cap.call', 'neko.files'))
            cap, args = payload['capability'], payload['args']
            if cap == 'chat.get_all_streams':
                return {'success': True, 'streams': [{'session_id': 'a', 'stream_id': 'a',
                    'platform': 'qq', 'chat_type': 'group', 'is_group_session': True,
                    'group_id': '123', 'account_id': '789', 'user_id': '111'}]}
            self.assertEqual(cap, 'api.call')
            action = args['args']['action_name']
            self.calls.append((action, args['args']['params']))
            if action == 'get_login_info':
                response = {'status': 'ok', 'retcode': 0, 'data': {'user_id': '789'}}
            elif action == 'get_group_file_url':
                response = self.failure or {'status': 'failed', 'retcode': 1400, 'message': 'packetBackend unavailable'}
            else:
                self.assertIn(action, ('get_group_root_files', 'get_group_files_by_folder'))
                response = {'status': 'ok', 'retcode': 0, 'data': copy.deepcopy(self.catalog)}
            return {'success': True, 'result': response}

        root = Path(self.temp.name)
        self.ctx = PluginContext('neko.files', rpc_call=rpc, paths=PluginPaths(data_dir=root, runtime_dir=root/'run'))
        self.config = Config()
        self.config.plugin.enabled = True
        self.config.access.group_ids = ['123']
        self.config.access.group_catalog = True
        self.service = FileService()
        await self.service.start(self.ctx, self.config)
        self.addAsyncCleanup(self.service.stop)

    async def test_large_unrelated_file_does_not_hide_small_document(self):
        result = await self.service.list_files(envelope(), include_group=True)
        self.assertTrue(result['success'], result)
        self.assertEqual([f['name'] for f in result['files']], ['target.txt', 'unrelated.zip'])
        self.assertTrue(result['directory_complete'])
        self.assertEqual(result['skipped_files'], 0)
        self.assertFalse(any('file_url' in action for action, _ in self.calls))
        self.assertEqual(self.calls[-1], ('get_group_root_files', {'group_id': '123'}))

    async def test_metadata_size_does_not_raise_download_limit(self):
        result = await self.service.list_files(envelope(), include_group=True)
        large = result['files'][1]
        self.calls.clear()
        with patch.object(module, 'download', side_effect=AssertionError('must not download')):
            response = await self.service.read(envelope(), large['ref'])
        self.assertFalse(response['success'])
        self.assertIn('超过大小限制', response['content'])
        self.assertEqual(self.calls, [])

    async def test_malformed_item_reports_partial_instead_of_losing_valid_entries(self):
        self.catalog['files'].append({'file_id': 'broken', 'file_name': 'bad.txt', 'file_size': None})
        result = await self.service.list_files(envelope(), include_group=True)
        self.assertTrue(result['success'])
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(result['skipped_files'], 1)
        self.assertTrue(result['source_truncated'])
        self.assertFalse(result['directory_complete'])

    async def test_all_invalid_is_not_reported_as_empty_directory(self):
        self.catalog = {'files': [{'file_name': 'bad.txt', 'file_size': -1}]}
        result = await self.service.list_files(envelope(), include_group=True)
        self.assertFalse(result['success'])
        self.assertIn('不能判断目录为空', result['content'])

    async def test_root_alias_and_invented_placeholders(self):
        result = await self.service.list_files(envelope(), include_group=True, folder_id='/')
        self.assertTrue(result['success'])
        self.assertEqual(self.calls[-1][0], 'get_group_root_files')
        for folder in ('0', 'root'):
            self.calls.clear()
            result = await self.service.list_files(envelope(), include_group=True, folder_id=folder)
            self.assertFalse(result['success'])
            self.assertIn('省略folder_id', result['content'])
            self.assertFalse(self.calls)

    async def test_partial_marker_survives_cursor_without_another_query(self):
        self.catalog = {'files': [{'file_id': str(n), 'name': str(n)+'.txt', 'size': 1} for n in range(25)] + [None]}
        result = await self.service.list_files(envelope(), include_group=True)
        self.calls.clear()
        next_page = await self.service.list_files(envelope(), cursor=result['next_cursor'])
        self.assertTrue(next_page['success'])
        self.assertEqual(next_page['skipped_files'], 1)
        self.assertFalse(next_page['directory_complete'])
        self.assertFalse(self.calls)

    async def test_fetch_failure_identifies_action_without_leaking_original_detail(self):
        listed = await self.service.list_files(envelope(), include_group=True)
        self.failure = {'status': 'failed', 'retcode': 1400, 'message': 'packetBackend failed https://secret.invalid/?token=TOPSECRET'}
        response = await self.service.inspect(envelope(), listed['files'][0]['ref'])
        self.assertFalse(response['success'])
        self.assertIn('get_group_file_url', response['content'])
        self.assertIn('1400', response['content'])
        self.assertIn('后端不可用', response['content'])
        self.assertNotIn('TOPSECRET', str(response))
        self.assertNotIn('secret.invalid', str(response))

    async def test_catalog_string_large_integer_and_not_supported_download(self):
        self.catalog['files'][1]['file_size'] = str(10 * 1024**3)
        result = await self.service.list_files(envelope(), include_group=True)
        self.assertTrue(result['success'])
        self.assertEqual(result['files'][1]['size'], 10 * 1024**3)

    async def test_nonzero_retcode_cannot_be_mistaken_for_success(self):
        item = await self.service.db('register', scope(), {'file_id':'small','name':'one.txt','size':1})
        self.failure = {'status':'ok','retcode':1400,'data':{}}
        result = await self.service.inspect(envelope(), item['ref'])
        self.assertFalse(result['success'])
        self.assertIn('get_group_file_url', result['content'])
