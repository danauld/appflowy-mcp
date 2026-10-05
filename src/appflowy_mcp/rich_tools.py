"""Rich page tools. Existing document writes require the realtime channel."""
from __future__ import annotations
import json
import secrets
import uuid
from typing import Any
from mcp.server.fastmcp import Context
from .client import AppFlowyError
from .database_collab import decode_collab_doc, get_field_type_int, get_field_select_options, SELECT_COLORS
from .image_upload import decode_image
from .rich_database import configure_view
from . import rich_document as native

FIELD_TYPES = {'Text': 0, 'Number': 1, 'Date': 2, 'SingleSelect': 3,
               'MultiSelect': 4, 'Checkbox': 5, 'URL': 6, 'Relation': 10}
LAYOUTS = {'Grid': 1, 'Board': 2, 'Calendar': 3, 'List': 6, 'Gallery': 7}
BLOCK_TYPES = {'Grid': 'grid', 'Board': 'board', 'Calendar': 'calendar', 'List': 'list', 'Gallery': 'gallery'}


def _node(folder, view_id):
    if folder.get('view_id') == view_id:
        return folder
    for child in folder.get('children', []):
        found = _node(child, view_id)
        if found:
            return found
    return None


def register_rich_tools(mcp, pool, resolve_document, load_state):
    async def document(ctx, ws, view):
        client = await pool.get(ctx)
        target = await resolve_document(client, ws, view)
        raw = await load_state(client, ws, target)
        if not raw:
            raise ValueError('document has no state; initialise it with append_to_page first')
        return client, target, raw

    async def write(client, ws, target, edit, **result):
        await client.apply_doc_update_web(ws, target, edit.update, collab_type=0)
        return {'view_id': target, 'write_path': 'web-update', **result}

    @mcp.tool()
    async def read_document_blocks(ctx: Context, workspace_id: str, view_id: str) -> dict[str, Any]:
        """Read a document or row body as a native block tree with IDs, data.delta,
        inline attributes, children and unknown properties. Use before targeted edits;
        Markdown reads cannot represent columns, images or linked views faithfully.
        """
        _, target, raw = await document(ctx, workspace_id, view_id)
        return {'view_id': target, **native.read_blocks(raw)}

    @mcp.tool()
    async def insert_document_blocks(ctx: Context, workspace_id: str, view_id: str,
                                     blocks: list[dict[str, Any]], parent_id: str | None = None,
                                     position: str = 'bottom', reference_block_id: str | None = None) -> dict[str, Any]:
        """Insert native trees {type,data,children}; return generated block IDs.
        parent_id defaults to root; position top/bottom/before/after (last two need
        reference_block_id). Text: data.delta=[{insert:"text",attributes:{bold:true}}].
        Types: paragraph, heading (level), callout (icon,icon_type), toggle_list
        (collapsed), simple_columns with simple_column children, outline, image
        (url,image_type:2,width,align), code (language:mermaid), math_equation (formula),
        link_preview (url,preview_type:bookmark|embed), linked_page (view_id), file,
        pdf, video, google_drive, grid/board/calendar/list/gallery, lists, tables.
        Embed previews are client-supported previews, not arbitrary HTML/iframes.
        Existing content is preserved; writes require realtime web-update.
        """
        client, target, raw = await document(ctx, workspace_id, view_id)
        edit, ids = native.insert_blocks(raw, blocks, parent_id, position, reference_block_id)
        return await write(client, workspace_id, target, edit, block_ids=ids)

    @mcp.tool()
    async def update_document_block(ctx: Context, workspace_id: str, view_id: str,
                                    block_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Patch a native block's data properties, preserving its ID, children and
        unknown data. data.delta replaces that block's complete text and formatting;
        omit delta to preserve text. Read IDs with read_document_blocks. Realtime only.
        """
        client, target, raw = await document(ctx, workspace_id, view_id)
        return await write(client, workspace_id, target, native.update_block(raw, block_id, data), block_id=block_id)

    @mcp.tool()
    async def move_document_block(ctx: Context, workspace_id: str, view_id: str,
                                  block_id: str, parent_id: str | None = None,
                                  position: str = 'bottom', reference_block_id: str | None = None) -> dict[str, Any]:
        """Move a block and its subtree without recreating their IDs. parent_id
        defaults to root; position top/bottom/before/after. Rejects cycles, root moves
        and invalid column/table nesting. Read IDs first. Realtime only.
        """
        client, target, raw = await document(ctx, workspace_id, view_id)
        edit = native.move_block(raw, block_id, parent_id, position, reference_block_id)
        return await write(client, workspace_id, target, edit, block_id=block_id)

    @mcp.tool()
    async def delete_document_block(ctx: Context, workspace_id: str, view_id: str,
                                    block_id: str, recursive: bool = False) -> dict[str, Any]:
        """Delete a specified block. Nonempty containers require recursive=true,
        which deletes all descendants. Root deletion is forbidden. Read IDs and
        inspect children before deleting. Realtime only; no full-page replacement.
        """
        client, target, raw = await document(ctx, workspace_id, view_id)
        return await write(client, workspace_id, target, native.delete_block(raw, block_id, recursive), deleted_block_id=block_id)

    @mcp.tool()
    async def upload_image(ctx: Context, workspace_id: str, view_id: str, file_name: str,
                           mime_type: str, image_base64: str) -> dict[str, Any]:
        """Upload PNG/JPEG/WebP/GIF bytes to the caller's AppFlowy storage (max 5MiB).
        image_base64 is strict base64, not a local path or data URL. Returns file_id,
        URL, byte count, SHA256 and native image data; it does not insert a block.
        Insert the returned block with insert_document_blocks. External URLs can
        be inserted directly as image data with image_type:2, without uploading.
        """
        raw, digest = decode_image(file_name, mime_type, image_base64)
        client = await pool.get(ctx)
        # Confirm this is an accessible page before creating storage objects.
        await client.get_page_view(workspace_id, view_id)
        result = await client.upload_image_bytes(workspace_id, view_id, raw, mime_type)
        return {**result, 'file_name': file_name, 'mime_type': mime_type,
                'size_bytes': len(raw), 'sha256': digest,
                'block': {'type': 'image', 'data': {'url': result['url'], 'image_type': 2}}}

    @mcp.tool()
    async def set_page_appearance(ctx: Context, workspace_id: str, view_id: str,
                                  icon: dict[str, Any] | None = None,
                                  cover: dict[str, Any] | None = None) -> dict[str, Any]:
        """Set page icon {ty:0 emoji|1 image URL|2 native icon,value:string} and/or
        cover {type:color|custom|gradient|built_in|unsplash|none,value:string,offset?:number}.
        Omitted values are preserved. Merges cover into existing folder metadata.
        Uploaded image URLs work as custom covers. Reports readback; API is folder
        metadata, not document replacement.
        """
        if icon is None and cover is None:
            raise ValueError('supply icon and/or cover')
        if icon is not None and (icon.get('ty') not in (0, 1, 2) or not isinstance(icon.get('value'), str)):
            raise ValueError('icon requires ty 0/1/2 and string value')
        if cover is not None and (cover.get('type') not in {'color', 'custom', 'gradient', 'built_in', 'unsplash', 'none'} or not isinstance(cover.get('value'), str)):
            raise ValueError('cover requires a supported type and string value')
        client = await pool.get(ctx)
        folder = await client.get_folder(workspace_id, depth=50)
        node = _node(folder, view_id)
        if node is None:
            raise ValueError('page not found in folder; cannot safely merge appearance')
        extra = node.get('extra') or {}
        if isinstance(extra, str):
            extra = json.loads(extra)
        payload = {'name': node['name']}
        if icon is not None:
            payload['icon'] = icon
        if cover is not None:
            payload['extra'] = {**extra, 'cover': cover}
        await client.request('PATCH', f'/api/workspace/{workspace_id}/page-view/{view_id}', json=payload)
        after = _node(await client.get_folder(workspace_id, depth=50), view_id)
        return {'view_id': view_id, 'icon': after.get('icon'), 'extra': after.get('extra'), 'write_path': 'page-view'}

    @mcp.tool()
    async def create_database_field(ctx: Context, workspace_id: str, database_id: str,
                                    name: str, field_type: str,
                                    options: list[dict[str, Any]] | None = None,
                                    target_database_id: str | None = None) -> dict[str, Any]:
        """Create a field: Text,Number,Checkbox,URL,Date,SingleSelect,MultiSelect,Relation.
        Select options are [{name,color?}] with AppFlowy named colors (Purple default).
        Relations require target_database_id. Reuses an exactly matching named field;
        conflicting definitions fail. Adds schema only, without copying rows.
        """
        if not name.strip() or field_type not in FIELD_TYPES:
            raise ValueError(f'name is required; field_type must be one of {list(FIELD_TYPES)}')
        ty = FIELD_TYPES[field_type]
        if options is not None and ty not in (3, 4):
            raise ValueError('options are only valid for select fields')
        if (ty == 10) != (target_database_id is not None):
            raise ValueError('Relation requires target_database_id; other types must omit it')
        opts = []
        for opt in options or []:
            if not isinstance(opt.get('name'), str) or not opt['name'].strip() or opt.get('color', 'Purple') not in SELECT_COLORS:
                raise ValueError(f'options require nonempty names and colors from {SELECT_COLORS}')
            opts.append({'id': secrets.token_hex(4), 'name': opt['name'], 'color': opt.get('color', 'Purple')})
        if len({o['name'] for o in opts}) != len(opts):
            raise ValueError('duplicate option names')
        client = await pool.get(ctx)
        db = await client.resolve_database_id(workspace_id, database_id)
        target = await client.resolve_database_id(workspace_id, target_database_id) if target_database_id else None
        fields = await client.get_database_fields(workspace_id, db)
        matches = [f for f in fields if f.get('name') == name]
        option_data = {'content': json.dumps({'options': opts, 'disable_color': False})} if ty in (3, 4) else ({'database_id': target} if ty == 10 else None)
        if matches:
            if len(matches) > 1:
                raise ValueError('multiple fields with this name; resolve ambiguity first')
            f = matches[0]
            equal = get_field_type_int(f) == ty
            if ty in (3, 4):
                equal = equal and [(o['name'], o.get('color', 'Purple')) for o in get_field_select_options(f)] == [(o['name'], o['color']) for o in opts]
            if ty == 10:
                to = f.get('type_option') or {}
                content = to.get('content', to)
                if isinstance(content, str):
                    content = json.loads(content or '{}')
                equal = equal and content.get('database_id') == target
            if not equal:
                raise ValueError(f'field {name!r} exists with a conflicting definition')
            return {'database_id': db, 'field_id': f['id'], 'existing': True}
        payload = {'name': name, 'field_type': ty}
        if option_data is not None:
            payload['type_option_data'] = option_data
        response = await client.request('POST', f'/api/workspace/{workspace_id}/database/{db}/fields', json=payload)
        fid = response.get('data')
        output = {'database_id': db, 'field_id': fid, 'existing': False}
        # Verify the created definition before reporting completion.
        actual = next((f for f in await client.get_database_fields(workspace_id, db) if f.get('id') == fid), None)
        if actual is None or (ty in (3, 4) and [(o['name'], o.get('color')) for o in get_field_select_options(actual)] != [(o['name'], o['color']) for o in opts]):
            output['verification_error'] = 'field created but its definition did not read back; inspect field_id before retrying'
        return output

    @mcp.tool()
    async def create_database_view(ctx: Context, workspace_id: str, database_id: str,
                                   parent_view_id: str, name: str, layout: str = 'Grid',
                                   embed_in_page: bool = False) -> dict[str, Any]:
        """Create a named Grid/Board/Calendar/List/Gallery view of an existing database.
        Rows remain shared. parent_view_id is the destination page/container; when
        embed_in_page=true insert a linked native database block there too. On partial
        success returns created IDs and embedding_error: do not retry creation blindly.
        """
        if layout not in LAYOUTS or not name.strip():
            raise ValueError(f'name required; layout must be one of {list(LAYOUTS)}')
        client = await pool.get(ctx)
        db = await client.resolve_database_id(workspace_id, database_id)
        if embed_in_page:
            _, target, raw = await document(ctx, workspace_id, parent_view_id)
        databases = await client.list_databases(workspace_id)
        source = next((d for d in databases if (d.get('database_id') or d.get('id')) == db), None)
        source_views = (source or {}).get('views') or []
        source_id = next((v.get('view_id') for v in source_views if v.get('view_id')), None)
        if not source_id:
            raise ValueError('database has no accessible source view')
        response = await client.request('POST', f'/api/workspace/{workspace_id}/page-view/{source_id}/database-view',
                                         json={'parent_view_id': parent_view_id, 'database_id': db,
                                               'name': name, 'layout': LAYOUTS[layout], 'embedded': embed_in_page})
        result = response.get('data') or {}
        vid = result.get('view_id')
        if not vid:
            raise AppFlowyError('create view response has no view_id')
        output = {'view_id': vid, 'database_id': db, 'layout': layout, 'embedded': False}
        if embed_in_page:
            try:
                # Reload after the API change to avoid stale server-side document state.
                _, target, raw = await document(ctx, workspace_id, parent_view_id)
                block = {'type': BLOCK_TYPES[layout], 'data': {'database_id': db, 'parent_id': parent_view_id,
                         'view_id': vid, 'view_ids': [vid]}}
                edit, ids = native.insert_blocks(raw, [block])
                output.update(await write(client, workspace_id, target, edit, block_ids=ids))
                output['view_id'] = vid
                output['embedded'] = True
            except (AppFlowyError, ValueError) as exc:
                output['embedding_error'] = str(exc)
        return output

    @mcp.tool()
    async def configure_database_view(ctx: Context, workspace_id: str, database_id: str,
                                      view_id: str, filters: list[dict[str, Any]] | None = None,
                                      sorts: list[dict[str, Any]] | None = None,
                                      group_field: str | None = None,
                                      visible_fields: list[str] | None = None) -> dict[str, Any]:
        """Configure one existing database view via realtime CRDT update. Omitted
        settings stay intact; [] clears filters/sorts, group_field="" clears grouping.
        Filters: [{field:name_or_id,condition:native_integer,content:native_string}].
        Text conditions 0:is,1:is_not,2:contains,3:does_not_contain,4:starts_with,
        5:ends_with,6:empty,7:not_empty. Other types use their native condition enums.
        Sorts: [{field:name_or_id,condition:0 ascending|1 descending}]. group_field
        accepts a select/checkbox field. visible_fields is the complete visible set.
        Unknown fields fail before writing. Does not change rows or other views.
        """
        client = await pool.get(ctx)
        db = await client.resolve_database_id(workspace_id, database_id)
        fields = await client.get_database_fields(workspace_id, db)
        doc = decode_collab_doc(await client.get_collab(workspace_id, db, 1))
        update = configure_view(doc, view_id, fields, filters, sorts, group_field, visible_fields)
        await client.apply_doc_update_web(workspace_id, db, update, collab_type=1)
        return {'database_id': db, 'view_id': view_id, 'write_path': 'web-update'}
