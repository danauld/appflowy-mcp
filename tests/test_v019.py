import base64
import hashlib
import unittest
from unittest.mock import AsyncMock
from pycrdt import Doc, Map, Array
from appflowy_mcp.doc_builder import build_document
from appflowy_mcp.rich_document import read_blocks, insert_blocks, update_block, move_block, delete_block
from appflowy_mcp.image_upload import decode_image, MAX_BYTES
from appflowy_mcp.rich_database import configure_view
from appflowy_mcp.server import build_server
from appflowy_mcp.config import Config
from appflowy_mcp.client import AppFlowyError, AppFlowyClient
from test_v017 import _doc_state


def merged(raw, update):
    doc = Doc(); doc.apply_update(raw); doc.apply_update(update)
    return doc.get_update()


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.raw = _doc_state(build_document([]))

    def test_nested_formatting_unknown_properties_and_targeted_edit(self):
        tree = {'type': 'simple_columns', 'data': {'future': 123}, 'children': [
            {'type': 'simple_column', 'data': {'ratio': 0.5}, 'children': [
                {'type': 'callout', 'data': {'icon': '💡', 'delta': [{'insert': '你好🙂', 'attributes': {'bold': True, 'mention': {'id': 'keep'}}}]}}]},
            {'type': 'simple_column', 'data': {}, 'children': [{'type': 'image', 'data': {'url': 'https://example.com/a.png', 'image_type': 2}}]}]}
        edit, ids = insert_blocks(self.raw, [tree]); raw = merged(self.raw, edit.update)
        b = read_blocks(raw)['blocks'][0]; leaf = b['children'][0]['children'][0]
        edited = update_block(raw, leaf['id'], {'icon': '✅'})
        after = read_blocks(merged(raw, edited.update))['blocks'][0]
        self.assertEqual(after['id'], ids[0]); self.assertEqual(after['data']['future'], 123)
        self.assertEqual(after['children'][0]['children'][0]['data']['delta'], leaf['data']['delta'])
        text_edit = update_block(raw, leaf['id'], {'delta': [{'insert': 'new', 'attributes': {'italic': True}}]})
        changed = read_blocks(merged(raw, text_edit.update))['blocks'][0]['children'][0]['children'][0]
        self.assertEqual(changed['id'], leaf['id']); self.assertEqual(changed['data']['delta'][0]['attributes'], {'italic': True})
        self.assertEqual(changed['properties']['external_id'], leaf['properties']['external_id'])

    def test_moves_deletes_and_validation(self):
        edit, ids = insert_blocks(self.raw, [{'type': 'toggle_list', 'data': {'delta': 'parent'}, 'children': [{'type': 'paragraph', 'data': {'delta': 'child'}}]}, {'type': 'paragraph', 'data': {'delta': 'second'}}])
        raw = merged(self.raw, edit.update); tree = read_blocks(raw)['blocks']; child = tree[0]['children'][0]['id']
        with self.assertRaises(ValueError): move_block(raw, ids[0], child)
        with self.assertRaises(ValueError): delete_block(raw, ids[0])
        moved = move_block(raw, ids[1], position='before', reference_block_id=ids[0])
        after = read_blocks(merged(raw, moved.update)); self.assertEqual(after['blocks'][0]['id'], ids[1])
        deleted = delete_block(raw, ids[0], True); after = read_blocks(merged(raw, deleted.update))
        self.assertEqual([b['id'] for b in after['blocks']], [ids[1]])
        with self.assertRaises(ValueError): insert_blocks(raw, [{'type': 'simple_column', 'data': {}}])
        with self.assertRaises(ValueError): delete_block(raw, read_blocks(raw)['root_id'], True)

    def test_concurrent_unrelated_edit_and_large_document(self):
        edit, ids = insert_blocks(self.raw, [{'type': 'paragraph', 'data': {'delta': 'x' * 1200000}}, {'type': 'paragraph', 'data': {'delta': 'other'}}])
        raw = merged(self.raw, edit.update)
        a = update_block(raw, ids[0], {'align': 'center'})
        b = update_block(raw, ids[1], {'delta': 'concurrent'})
        after = read_blocks(merged(merged(raw, b.update), a.update))
        self.assertEqual(after['blocks'][1]['data']['delta'][0]['insert'], 'concurrent')
        self.assertEqual(after['blocks'][0]['id'], ids[0]); self.assertLess(len(a.update), 1000)

    def test_image_validation(self):
        raw = b'\x89PNG\r\n\x1a\n' + b'payload'
        decoded, digest = decode_image('logo.png', 'image/png', base64.b64encode(raw).decode())
        self.assertEqual(decoded, raw); self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        for name, mime, data in [('bad.png', 'image/jpeg', base64.b64encode(raw).decode()), ('../logo.png', 'image/png', base64.b64encode(raw).decode()), ('logo.png', 'image/png', '!!!'), ('logo.png', 'image/png', 'A' * (MAX_BYTES * 2))]:
            with self.assertRaises(ValueError): decode_image(name, mime, data)

    def test_view_configuration_preserves_other_view_and_fields(self):
        doc = Doc(); doc['data'] = Map({'database': Map({'views': Map({'a': Map({'filters': Array([]), 'sorts': Array([]), 'field_settings': Map({'title': Map({'width': 222})}), 'future': 'keep'}), 'b': Map({'name': 'untouched'})})})})
        raw = doc.get_update(); fields = [{'id': 'title', 'name': 'Name', 'field_type': 0}, {'id': 'status', 'name': 'Status', 'field_type': 3}]
        update = configure_view(doc, 'a', fields, filters=[{'field': 'Name', 'condition': 2, 'content': 'lab'}], sorts=[{'field': 'Name', 'condition': 1}], group_field='Status', visible_fields=['Name'])
        replica = Doc(); replica.apply_update(raw); replica.apply_update(update)
        view = replica.get('data', type=Map)['database']['views']
        self.assertEqual(view['b']['name'], 'untouched'); self.assertEqual(view['a']['future'], 'keep')
        self.assertEqual(view['a']['field_settings']['title']['width'], 222)
        self.assertEqual(view['a']['field_settings']['status']['visibility'], 2)
        self.assertEqual(list(view['a']['filters'])[0]['field_id'], 'title')
        with self.assertRaises(ValueError): configure_view(doc, 'a', fields, sorts=[{'field': 'bogus', 'condition': 0}])


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mcp, self.pool = build_server(Config('https://example.com', True, 'http', '127.0.0.1', 8765))
        self.client = AsyncMock(spec=AppFlowyClient)
        self.pool.get = AsyncMock(return_value=self.client)
        self.raw = _doc_state(build_document([]))
        self.client.get_page_view.return_value = {'view': {'layout': 0}, 'encoded_collab': list(self.raw)}

    async def test_catalog_and_realtime_rejection_has_no_put(self):
        tools = await self.mcp.list_tools(); self.assertEqual(len(tools), 30)
        self.client.apply_doc_update_web.side_effect = AppFlowyError('rejected')
        with self.assertRaisesRegex(Exception, 'rejected'):
            await self.mcp.call_tool('insert_document_blocks', {'workspace_id': 'w', 'view_id': 'v', 'blocks': [{'type': 'paragraph', 'data': {'delta': 'hello'}}]})
        self.client.update_page_collab.assert_not_called()

    async def test_upload_does_not_echo_payload(self):
        raw = b'GIF89a' + b'test'; encoded = base64.b64encode(raw).decode()
        self.client.upload_image_bytes.return_value = {'file_id': 'f', 'url': 'https://example.com/image.gif'}
        result = await self.mcp.call_tool('upload_image', {'workspace_id': 'w', 'view_id': 'v', 'file_name': 'x.gif', 'mime_type': 'image/gif', 'image_base64': encoded})
        self.assertNotIn(encoded, str(result)); self.assertIn(hashlib.sha256(raw).hexdigest(), str(result))

    async def test_field_reuse_conflict_and_native_options(self):
        self.client.resolve_database_id.return_value = 'db'
        self.client.get_database_fields.return_value = [{'id': 'f', 'name': 'Status', 'field_type': 'SingleSelect', 'type_option': {'content': {'options': [{'id': 'o', 'name': 'Ready', 'color': 'Green'}]}}}]
        args = {'workspace_id': 'w', 'database_id': 'db', 'name': 'Status', 'field_type': 'SingleSelect', 'options': [{'name': 'Ready', 'color': 'Green'}]}
        result = await self.mcp.call_tool('create_database_field', args)
        self.assertIn('true', str(result).lower()); self.client.request.assert_not_called()
        with self.assertRaisesRegex(Exception, 'conflicting'):
            await self.mcp.call_tool('create_database_field', {**args, 'options': [{'name': 'Other'}]})
        self.client.request.assert_not_called()
        self.client.request.return_value = {'data': 'new-field'}
        self.client.get_database_fields.side_effect = [[], [{'id': 'new-field', 'name': 'Status', 'field_type': 'SingleSelect', 'type_option': {'content': {'options': [{'id': 'new-option', 'name': 'Ready', 'color': 'Green'}]}}}]]
        await self.mcp.call_tool('create_database_field', args)
        payload = self.client.request.call_args.kwargs['json']
        import json
        self.assertEqual(json.loads(payload['type_option_data']['content'])['options'][0]['name'], 'Ready')

    async def test_appearance_merges_existing_metadata(self):
        self.client.get_folder.side_effect = [{'view_id': 'v', 'name': 'Existing', 'extra': {'is_pinned': True, 'future': 10}}, {'view_id': 'v', 'name': 'Existing', 'extra': {'is_pinned': True, 'future': 10, 'cover': {'type': 'color', 'value': '#123456'}}}]
        await self.mcp.call_tool('set_page_appearance', {'workspace_id': 'w', 'view_id': 'v', 'cover': {'type': 'color', 'value': '#123456'}})
        payload = self.client.request.call_args.kwargs['json']
        self.assertEqual(payload['extra']['future'], 10); self.assertTrue(payload['extra']['is_pinned'])
        self.assertEqual(payload['name'], 'Existing')

    async def test_created_view_embedding_failure_reports_created_id(self):
        self.client.resolve_database_id.return_value = 'db'
        self.client.list_databases.return_value = [{'id': 'db', 'views': [{'view_id': 'source'}]}]
        self.client.request.return_value = {'data': {'view_id': 'new-view'}}
        self.client.apply_doc_update_web.side_effect = AppFlowyError('realtime rejected')
        result = await self.mcp.call_tool('create_database_view', {'workspace_id': 'w', 'database_id': 'db', 'parent_view_id': 'v', 'name': 'Linked', 'embed_in_page': True})
        self.assertIn('new-view', str(result)); self.assertIn('embedding_error', str(result))
        self.assertEqual(self.client.request.call_count, 1); self.client.update_page_collab.assert_not_called()

    async def test_native_page_link_survives_targeted_edits(self):
        tool = self.mcp._tool_manager.get_tool('insert_document_blocks')
        result = await tool.fn(None, 'w', 'v', [{'type': 'linked_page', 'data': {'view_id': 'destination'}}])
        update = self.client.apply_doc_update_web.call_args.args[2]
        after = read_blocks(merged(self.raw, update))['blocks'][0]
        self.assertEqual(after['type'], 'linked_page'); self.assertEqual(after['data']['view_id'], 'destination')
        self.assertEqual(after['id'], result['block_ids'][0])
