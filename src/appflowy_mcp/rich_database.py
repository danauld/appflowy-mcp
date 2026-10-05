"""Validated view settings updates on existing database CRDT maps."""
from __future__ import annotations
import uuid
from pycrdt import Array, Map
from .database_collab import get_field_type_int


def configure_view(doc, view_id, fields, filters=None, sorts=None, group_field=None, visible_fields=None):
    database = doc.get('data', type=Map)['database']
    views = database['views']
    if view_id not in views:
        raise ValueError(f'view {view_id!r} not found in this database')
    by_ref = {str(f[k]): f for f in fields for k in ('id', 'name') if f.get(k)}
    def field(ref):
        if ref not in by_ref:
            raise ValueError(f'unknown field {ref!r}; valid names: {[f.get("name") for f in fields]}')
        return by_ref[ref]
    parsed = {}
    for key, specs in [('filters', filters), ('sorts', sorts)]:
        if specs is None:
            continue
        items = []
        for spec in specs:
            f = field(spec.get('field', ''))
            ty = get_field_type_int(f)
            condition = spec.get('condition')
            if not isinstance(condition, int) or isinstance(condition, bool) or condition < 0:
                raise ValueError('condition must be a native AppFlowy nonnegative integer')
            if key == 'sorts' and condition not in (0, 1):
                raise ValueError('sort condition: 0 ascending, 1 descending')
            item = {'id': str(uuid.uuid4()), 'field_id': f['id'], 'ty': ty, 'condition': condition}
            if key == 'filters':
                if not isinstance(spec.get('content', ''), str):
                    raise ValueError('filter content must be a native AppFlowy string')
                item['content'] = spec.get('content', '')
                item['type'] = ty
                item['filter_type'] = 2
            items.append(item)
        parsed[key] = items
    if group_field is not None:
        if group_field == '':
            parsed['groups'] = []
        else:
            f = field(group_field)
            ty = get_field_type_int(f)
            if ty not in (3, 4, 5):
                raise ValueError('group_field must be SingleSelect, MultiSelect or Checkbox')
            parsed['groups'] = [{'id': str(uuid.uuid4()), 'field_id': f['id'], 'ty': ty, 'groups': [], 'content': ''}]
    visible = None if visible_fields is None else {field(ref)['id'] for ref in visible_fields}
    view = views[view_id]
    sv = doc.get_state()
    with doc.transaction():
        for key, items in parsed.items():
            if key not in view:
                view[key] = Array([])
            arr = view[key]
            while len(arr):
                del arr[0]
            for item in items:
                arr.append(Map({k: Array(v) if isinstance(v, list) else v for k, v in item.items()}))
        if visible is not None:
            if 'field_settings' not in view:
                view['field_settings'] = Map({})
            settings = view['field_settings']
            for f in fields:
                fid = f['id']
                if fid not in settings:
                    settings[fid] = Map({})
                settings[fid]['visibility'] = 0 if fid in visible else 2
    return doc.get_update(sv)
