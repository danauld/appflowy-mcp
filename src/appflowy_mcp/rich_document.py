"""Native document trees, preserving CRDT identities and unknown properties."""
from __future__ import annotations

import json
from typing import Any
from pycrdt import Array, Map, Text
from .doc_builder import _open_document, _finish, _new_key, _delete_block_tree, _block_data_dict

# Types supported by AppFlowy clients. Unknown existing blocks remain readable.
TYPES = {'paragraph', 'heading', 'bulleted_list', 'numbered_list', 'todo_list',
         'quote', 'code', 'divider', 'callout', 'toggle_list', 'simple_columns',
         'simple_column', 'outline', 'image', 'link_preview', 'page',
         'math_equation', 'pdf', 'file', 'video', 'google_drive', 'grid', 'board', 'calendar', 'list', 'gallery', 'linked_page',
         'simple_table', 'simple_table_row', 'simple_table_cell'}
LEAVES = {'divider', 'outline', 'image', 'link_preview', 'math_equation',
          'pdf', 'file', 'video', 'google_drive', 'grid', 'board', 'calendar', 'list', 'gallery', 'linked_page', 'code'}


def _block(o, bid):
    if bid not in o.blocks_map:
        raise ValueError(f'block {bid!r} not found; read_document_blocks first')
    return o.blocks_map[bid]


def _children(o, bid):
    return o.children_map[_block(o, bid)['children']]


def _nest(parent_type, child_type):
    if parent_type in LEAVES:
        raise ValueError(f'{parent_type} cannot contain child blocks')
    if (child_type == 'simple_column') != (parent_type == 'simple_columns'):
        raise ValueError('simple_columns must contain only simple_column blocks')
    if child_type == 'page':
        raise ValueError('page is reserved for the document root; use a page mention or link_preview')
    for parent, child in [('simple_table', 'simple_table_row'), ('simple_table_row', 'simple_table_cell')]:
        if (parent_type == parent) != (child_type == child):
            raise ValueError(f'{parent} must contain only {child} blocks')


def _delta(value):
    if isinstance(value, str):
        return [{'insert': value}]
    if not isinstance(value, list):
        raise ValueError('delta must be a string or a list of insert operations')
    for run in value:
        if not isinstance(run, dict) or not isinstance(run.get('insert'), str) or set(run) - {'insert', 'attributes'}:
            raise ValueError('delta accepts text insert operations only')
        if 'attributes' in run and not isinstance(run['attributes'], dict):
            raise ValueError('delta attributes must be an object')
    return value


def _set_text(o, block, delta):
    runs = _delta(delta)
    tid = block.get('external_id')
    if not tid:
        tid = _new_key('txt')
        block['external_id'] = tid
        block['external_type'] = 'text'
        o.text_map[tid] = Text('')
    text = o.text_map[tid]
    if len(text):
        del text[:]
    plain = ''.join(r['insert'] for r in runs)
    text.insert(0, plain)
    offset = 0
    for run in runs:
        end = offset + len(run['insert'].encode('utf-8'))
        if run.get('attributes') and end > offset:
            text.format(offset, end, run['attributes'])
        offset = end


def _validate_tree(tree, parent_type, depth=0):
    if depth > 64:
        raise ValueError('block tree exceeds 64 levels')
    if not isinstance(tree, dict) or set(tree) - {'type', 'data', 'children'}:
        raise ValueError('each block must have type, data, and optional children')
    ty = tree.get('type')
    if ty not in TYPES:
        raise ValueError(f'unsupported block type {ty!r}; supported: {sorted(TYPES - {"page"})}')
    _nest(parent_type, ty)
    data = tree.get('data', {})
    if not isinstance(data, dict):
        raise ValueError('block data must be an object')
    if 'delta' in data:
        _delta(data['delta'])
    if ty == 'linked_page' and not isinstance(data.get('view_id'), str):
        raise ValueError('linked_page requires data.view_id from list_pages')
    if ty == 'heading' and data.get('level') not in (1, 2, 3, 4, 5, 6):
        raise ValueError('heading requires level 1 through 6')
    if ty == 'image' and (not isinstance(data.get('url'), str) or data.get('image_type') not in (1, 2)):
        raise ValueError('image requires url and image_type 1 or 2; local paths are unsupported')

    children = tree.get('children', [])
    if not isinstance(children, list):
        raise ValueError('children must be an array')
    for child in children:
        _validate_tree(child, ty, depth + 1)


def _add(o, parent, tree):
    bid, ck = _new_key('block'), _new_key('ch')
    data = dict(tree.get('data', {}))
    delta = data.pop('delta', None)
    o.blocks_map[bid] = Map({'id': bid, 'ty': tree['type'], 'parent': parent,
                            'children': ck, 'data': json.dumps(data)})
    o.children_map[ck] = Array([])
    if delta is not None:
        _set_text(o, o.blocks_map[bid], delta)
    for child in tree.get('children', []):
        o.children_map[ck].append(_add(o, bid, child))
    return bid


def read_blocks(raw: bytes) -> dict[str, Any]:
    o = _open_document(raw)
    def visit(bid, ancestors):
        if bid in ancestors:
            raise ValueError('document contains a block cycle')
        b = _block(o, bid)
        data = _block_data_dict(b)
        tid = b.get('external_id')
        if tid and tid in o.text_map:
            data['delta'] = [{'insert': chunk, **({'attributes': attrs} if attrs else {})}
                             for chunk, attrs in o.text_map[tid].diff()]
        return {'id': bid, 'type': b['ty'], 'parent_id': b['parent'], 'data': data,
                'properties': {k: b[k] for k in b.keys() if k not in {'data', 'id', 'ty', 'parent'}},
                'children': [visit(cid, ancestors | {bid}) for cid in _children(o, bid)]}
    return {'root_id': o.page_id, 'blocks': [visit(bid, {o.page_id}) for bid in o.root_children]}


def _at(children, position, reference):
    ids = list(children)
    if position in {'top', 'bottom'}:
        if reference is not None:
            raise ValueError('reference_block_id is only valid for before/after')
        return 0 if position == 'top' else len(ids)
    if position not in {'before', 'after'} or reference not in ids:
        raise ValueError('position must be top/bottom or before/after an existing sibling reference_block_id')
    return ids.index(reference) + (position == 'after')


def insert_blocks(raw, trees, parent_id=None, position='bottom', reference_block_id=None):
    o = _open_document(raw)
    parent = parent_id or o.page_id
    if not isinstance(trees, list) or not trees:
        raise ValueError('blocks must be a nonempty array')
    for tree in trees:
        _validate_tree(tree, _block(o, parent)['ty'])
    children = _children(o, parent)
    at = _at(children, position, reference_block_id)
    with o.doc.transaction():
        ids = [_add(o, parent, tree) for tree in trees]
        for offset, bid in enumerate(ids):
            children.insert(at + offset, bid)
    return _finish(o, len(ids)), ids


def update_block(raw, block_id, data):
    o = _open_document(raw)
    b = _block(o, block_id)
    if block_id == o.page_id:
        raise ValueError('cannot update root; use set_page_appearance')
    if not isinstance(data, dict):
        raise ValueError('data must be an object patch')
    patch = dict(data)
    delta = patch.pop('delta', None)
    if delta is not None:
        _delta(delta)
    merged = {**_block_data_dict(b), **patch}
    with o.doc.transaction():
        b['data'] = json.dumps(merged)
        if delta is not None:
            _set_text(o, b, delta)
    return _finish(o, 1)


def move_block(raw, block_id, parent_id=None, position='bottom', reference_block_id=None):
    o = _open_document(raw)
    b = _block(o, block_id)
    parent = parent_id or o.page_id
    if block_id == o.page_id:
        raise ValueError('cannot move root')
    ancestor = parent
    while ancestor:
        if ancestor == block_id:
            raise ValueError('cannot move a block into itself or its descendants')
        ancestor = _block(o, ancestor)['parent']
    _nest(_block(o, parent)['ty'], b['ty'])
    if reference_block_id == block_id:
        raise ValueError('cannot position a block relative to itself')
    target = _children(o, parent)
    # Validate before removing; same-parent destination computed after removal.
    _at(target, position, reference_block_id)
    source = _children(o, b['parent'])
    with o.doc.transaction():
        del source[list(source).index(block_id)]
        target.insert(_at(target, position, reference_block_id), block_id)
        b['parent'] = parent
    return _finish(o, 1)


def delete_block(raw, block_id, recursive=False):
    o = _open_document(raw)
    b = _block(o, block_id)
    if block_id == o.page_id:
        raise ValueError('cannot delete root')
    if len(_children(o, block_id)) and not recursive:
        raise ValueError('block contains children; pass recursive=true to delete its subtree')
    with o.doc.transaction():
        siblings = _children(o, b['parent'])
        del siblings[list(siblings).index(block_id)]
        _delete_block_tree(block_id, o.blocks_map, o.children_map, o.text_map)
    return _finish(o, 1)
