import asyncio
import json as _json
import time
from typing import Any
import uuid

import httpx
from pycrdt import Doc, Map


class AppFlowyError(RuntimeError):
    pass


class AppFlowyClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        verify: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._http = httpx.AsyncClient(verify=verify, timeout=timeout)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        r = await self._http.post(
            f"{self.base_url}/gotrue/token",
            params={"grant_type": "password"},
            json={"email": self._email, "password": self._password},
        )
        if r.status_code != 200:
            raise AppFlowyError(
                f"Login failed ({r.status_code}): {r.text[:300]}"
            )
        self._store_token(r.json())
        await self._verify()

    async def _verify(self) -> None:
        # Bootstraps the AppFlowy-side user record + default workspace on first
        # login (real AppFlowy clients call this after every gotrue auth).
        # Idempotent: returns {is_new: false} if the user already exists.
        token = self._access_token
        r = await self._http.get(
            f"{self.base_url}/api/user/verify/{token}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code >= 400:
            raise AppFlowyError(
                f"User verify failed ({r.status_code}): {r.text[:300]}"
            )

    async def _refresh(self) -> None:
        if not self._refresh_token:
            await self._login()
            return
        r = await self._http.post(
            f"{self.base_url}/gotrue/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": self._refresh_token},
        )
        if r.status_code != 200:
            # Refresh token may be revoked — fall back to password grant.
            await self._login()
            return
        self._store_token(r.json())

    def _store_token(self, payload: dict[str, Any]) -> None:
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        # 60s safety margin before expiry to avoid races.
        self._expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60

    async def _ensure_token(self) -> None:
        async with self._auth_lock:
            if not self._access_token:
                await self._login()
            elif time.time() >= self._expires_at:
                await self._refresh()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        await self._ensure_token()
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        r = await self._http.request(
            method, url, headers=headers, params=params, json=json
        )
        if r.status_code == 401:
            async with self._auth_lock:
                await self._login()
            headers["Authorization"] = f"Bearer {self._access_token}"
            r = await self._http.request(
                method, url, headers=headers, params=params, json=json
            )
        if r.status_code >= 400:
            raise AppFlowyError(
                f"{method} {path} failed ({r.status_code}): {r.text[:500]}"
            )
        if not r.content:
            return None
        return r.json()

    async def list_workspaces(
        self, include_role: bool = True, include_member_count: bool = False
    ) -> list[dict[str, Any]]:
        resp = await self.request(
            "GET",
            "/api/workspace",
            params={
                "include_role": str(include_role).lower(),
                "include_member_count": str(include_member_count).lower(),
            },
        )
        return resp.get("data") or []

    async def get_folder(
        self, workspace_id: str, depth: int = 10, root_view_id: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"depth": str(depth)}
        if root_view_id:
            params["root_view_id"] = root_view_id
        resp = await self.request(
            "GET", f"/api/workspace/{workspace_id}/folder", params=params
        )
        return resp.get("data") or {}

    async def get_page_view(
        self, workspace_id: str, view_id: str
    ) -> dict[str, Any]:
        resp = await self.request(
            "GET", f"/api/workspace/{workspace_id}/page-view/{view_id}"
        )
        return resp.get("data") or {}

    async def create_page(
        self,
        workspace_id: str,
        parent_view_id: str,
        name: str,
        layout: int = 0,
    ) -> dict[str, Any]:
        resp = await self.request(
            "POST",
            f"/api/workspace/{workspace_id}/page-view",
            json={
                "parent_view_id": parent_view_id,
                "layout": layout,
                "name": name,
            },
        )
        return resp.get("data") or {}

    async def rename_page(
        self, workspace_id: str, view_id: str, name: str
    ) -> None:
        await self.request(
            "POST",
            f"/api/workspace/{workspace_id}/page-view/{view_id}/update-name",
            json={"name": name},
        )

    async def move_page(
        self,
        workspace_id: str,
        view_id: str,
        new_parent_view_id: str,
        prev_view_id: str | None = None,
    ) -> None:
        """Move/reorder a page in the folder tree.

        `new_parent_view_id` is the destination parent (use the current parent
        to reorder in place). `prev_view_id` is the sibling that should come
        immediately before the moved page after the move; `None` places it
        first under the new parent.
        """
        await self.request(
            "POST",
            f"/api/workspace/{workspace_id}/page-view/{view_id}/move",
            json={
                "new_parent_view_id": new_parent_view_id,
                "prev_view_id": prev_view_id,
            },
        )

    async def update_page_collab(
        self,
        workspace_id: str,
        object_id: str,
        encoded_collab_v1: bytes,
        collab_type: int = 0,
    ) -> None:
        """Replace document content via background DB upsert. encoded_collab_v1
        must be bincode-serialized EncodedCollab (state_vector + doc_state +
        version) — see doc_builder.build_document.

        Caveat: this writes only to the storage backend; active realtime
        WebSocket sessions (browser/desktop with the page open) may overwrite
        with their local state. Prefer `apply_doc_update_web` for live updates.
        """
        await self.request(
            "PUT",
            f"/api/workspace/{workspace_id}/collab/{object_id}",
            json={
                "workspace_id": workspace_id,
                "object_id": object_id,
                "encoded_collab_v1": list(encoded_collab_v1),
                "collab_type": collab_type,
            },
        )

    async def apply_doc_update_web(
        self,
        workspace_id: str,
        object_id: str,
        doc_state: bytes,
        collab_type: int = 0,
    ) -> None:
        """Apply a Yrs v1 incremental update through the realtime sync server.

        Routes via `/api/workspace/v1/{ws}/collab/{obj}/web-update` — the same
        channel AppFlowy Web uses. Changes are broadcast to all connected
        WebSocket sessions in real time.
        """
        await self.request(
            "POST",
            f"/api/workspace/v1/{workspace_id}/collab/{object_id}/web-update",
            json={
                "doc_state": list(doc_state),
                "collab_type": collab_type,
            },
        )

    @staticmethod
    def row_to_document_id(row_id: str) -> str:
        """A database row's body document is a separate collab; AppFlowy derives
        its object id as uuid5(row_uuid, 'document_id')."""
        return str(uuid.uuid5(uuid.UUID(str(row_id)), "document_id"))

    async def get_collab(
        self, workspace_id: str, object_id: str, collab_type: int
    ) -> dict[str, Any]:
        """Read any collab's encoded state (0 Document, 1 Database, 4 DatabaseRow)."""
        resp = await self.request(
            "GET",
            f"/api/workspace/v1/{workspace_id}/collab/{object_id}",
            params={"collab_type": str(collab_type)},
        )
        return resp.get("data") or {}

    async def get_document_decoded(
        self, workspace_id: str, view_id: str
    ) -> dict[str, Any]:
        """Fetch a Document's raw Y.Doc bytes and decode via pycrdt.

        Returns the same shape as `/collab/json` but with text_map values that
        preserve inline formatting (as JSON-serialized Yjs deltas where present;
        plain strings where the text has no formatting).
        """
        page: dict[str, Any] = {}
        try:
            page = await self.get_page_view(workspace_id, view_id)
        except AppFlowyError:
            try:
                doc_id = self.row_to_document_id(view_id)
                page = await self.get_page_view(workspace_id, doc_id)
            except Exception:
                pass
        raw = bytes(
            page.get("data", {}).get("encoded_collab")
            or page.get("encoded_collab")
            or b""
        )
        if not raw:
            # Fallback: check raw collab endpoint
            for target_id in (view_id, self.row_to_document_id(view_id)):
                try:
                    cdata = await self.get_collab(
                        workspace_id, target_id, collab_type=0
                    )
                    ds = cdata.get("doc_state")
                    if ds:
                        raw = bytes(ds)
                        break
                except Exception:
                    pass
        if not raw:
            return {}

        doc = Doc()
        doc["data"] = Map({})
        doc.apply_update(raw)
        root = doc.get("data", type=Map)
        if "document" not in root:
            return {}
        document = root["document"]

        blocks_out: dict[str, dict[str, Any]] = {}
        blocks_map = document["blocks"]
        for bid in list(blocks_map.keys()):
            b = blocks_map[bid]
            blocks_out[bid] = {k: b[k] for k in b.keys()}

        children_out: dict[str, list[str]] = {}
        cm = document["meta"]["children_map"]
        for ck in list(cm.keys()):
            arr = cm[ck]
            children_out[ck] = list(arr)

        text_out: dict[str, str] = {}
        tm = document["meta"]["text_map"]
        for tk in list(tm.keys()):
            runs = tm[tk].diff()
            if not runs:
                text_out[tk] = ""
            elif len(runs) == 1 and not runs[0][1]:
                text_out[tk] = runs[0][0]
            else:
                ops = []
                for chunk, attrs in runs:
                    op = {"insert": chunk}
                    if attrs:
                        op["attributes"] = attrs
                    ops.append(op)
                text_out[tk] = _json.dumps(ops, ensure_ascii=False)

        return {
            "collab": {
                "document": {
                    "page_id": document["page_id"],
                    "blocks": blocks_out,
                    "meta": {
                        "children_map": children_out,
                        "text_map": text_out,
                    },
                }
            }
        }

    async def get_collab_json(
        self, workspace_id: str, object_id: str, collab_type: int
    ) -> Any:
        # collab_type: 0=Document, 1=Database, 2=WorkspaceDatabase, 3=Folder,
        #              4=DatabaseRow, 5=UserAwareness, 6=Unknown
        resp = await self.request(
            "GET",
            f"/api/workspace/v1/{workspace_id}/collab/{object_id}/json",
            params={"collab_type": str(collab_type)},
        )
        return resp.get("data")

    # ------------------------------------------------------------------ #
    # Database (Grid / Board / Calendar) — rows & fields.                 #
    # AppFlowy-Cloud exposes row CRUD + field *reads*; there is no API to #
    # create or define fields/columns/options, so these operate on a      #
    # database's existing schema (e.g. a fresh Grid's default columns).    #
    # ------------------------------------------------------------------ #

    async def list_databases(self, workspace_id: str) -> list[dict[str, Any]]:
        resp = await self.request(
            "GET", f"/api/workspace/{workspace_id}/database"
        )
        return resp.get("data") or []

    async def resolve_database_id(self, workspace_id: str, ref: str) -> str:
        """Resolve a database_id from any of: a database_id, a database
        view_id, or the folder/page view_id that create_page returned for a
        Grid/Board/Calendar (whose child is the database view)."""
        dbs = await self.list_databases(workspace_id)
        for d in dbs:
            if d.get("id") == ref:
                return ref
        for d in dbs:
            for v in d.get("views") or []:
                if v.get("view_id") == ref:
                    return d["id"]
        # ref may be the folder page create_page returned; its child is the
        # actual database view.
        try:
            sub = await self.get_folder(workspace_id, depth=2, root_view_id=ref)
        except AppFlowyError:
            sub = {}
        child_ids = {c.get("view_id") for c in (sub.get("children") or [])}
        if child_ids:
            for d in dbs:
                for v in d.get("views") or []:
                    if v.get("view_id") in child_ids:
                        return d["id"]
        raise AppFlowyError(
            f"could not resolve a database from {ref!r}; pass a database_id or "
            "a Grid/Board/Calendar view_id (see list_databases)"
        )

    async def get_database_fields(
        self, workspace_id: str, database_id: str
    ) -> list[dict[str, Any]]:
        resp = await self.request(
            "GET",
            f"/api/workspace/{workspace_id}/database/{database_id}/fields",
        )
        return resp.get("data") or []

    async def get_database_row_ids(
        self, workspace_id: str, database_id: str
    ) -> list[str]:
        resp = await self.request(
            "GET", f"/api/workspace/{workspace_id}/database/{database_id}/row"
        )
        out: list[str] = []
        for r in resp.get("data") or []:
            rid = r if isinstance(r, str) else r.get("id")
            if rid:
                out.append(rid)
        return out

    async def get_database_rows(
        self,
        workspace_id: str,
        database_id: str,
        row_ids: list[str],
        with_doc: bool = False,
    ) -> list[dict[str, Any]]:
        if not row_ids:
            return []
        params: dict[str, Any] = {"ids": ",".join(row_ids)}
        if with_doc:
            params["with_doc"] = "true"
        resp = await self.request(
            "GET",
            f"/api/workspace/{workspace_id}/database/{database_id}/row/detail",
            params=params,
        )
        return resp.get("data") or []

    async def create_database_row(
        self, workspace_id: str, database_id: str, cells: dict[str, Any]
    ) -> str:
        resp = await self.request(
            "POST",
            f"/api/workspace/{workspace_id}/database/{database_id}/row",
            json={"cells": cells},
        )
        return resp.get("data")

    async def upsert_database_row(
        self,
        workspace_id: str,
        database_id: str,
        pre_hash: str,
        cells: dict[str, Any],
    ) -> str:
        resp = await self.request(
            "PUT",
            f"/api/workspace/{workspace_id}/database/{database_id}/row",
            json={"pre_hash": pre_hash, "cells": cells},
        )
        return resp.get("data")

    async def update_database_row(
        self,
        workspace_id: str,
        database_id: str,
        row_id: str,
        cells: dict[str, Any],
    ) -> dict[str, Any]:
        """Update an existing row's cells in place via CRDT."""
        from .database_collab import (
            apply_row_cells_update,
            decode_collab_doc,
            resolve_cells_dict,
        )

        fields = await self.get_database_fields(workspace_id, database_id)
        resolved = resolve_cells_dict(fields, cells)
        raw_collab = await self.get_collab(workspace_id, row_id, collab_type=4)
        doc = decode_collab_doc(raw_collab)
        update_bytes = apply_row_cells_update(doc, resolved)
        if update_bytes:
            await self.apply_doc_update_web(
                workspace_id, row_id, update_bytes, collab_type=4
            )
        return {
            "database_id": database_id,
            "row_id": row_id,
            "updated_fields": list(cells.keys()),
        }

    async def add_select_option(
        self,
        workspace_id: str,
        database_id: str,
        field_ref: str,
        name: str,
        color: str = "Purple",
    ) -> dict[str, Any]:
        """Add an option to a SingleSelect or MultiSelect field in a database."""
        from .database_collab import (
            apply_add_select_option,
            decode_collab_doc,
            get_field_type_int,
        )

        fields = await self.get_database_fields(workspace_id, database_id)
        field = None
        for f in fields:
            if str(f.get("id")) == str(field_ref) or str(f.get("name")) == str(field_ref):
                field = f
                break
        if field is None:
            avail = ", ".join(repr(f.get("name")) for f in fields if f.get("name"))
            raise AppFlowyError(
                f"field {field_ref!r} not found in database; available fields: {avail}"
            )
        fid = str(field["id"])
        fty = get_field_type_int(field)
        if fty not in (3, 4):
            raise AppFlowyError(
                f"field {field.get('name')!r} is not a SingleSelect or MultiSelect field"
            )

        raw_collab = await self.get_collab(workspace_id, database_id, collab_type=1)
        doc = decode_collab_doc(raw_collab)
        opt_id, is_existing, update_bytes = apply_add_select_option(
            doc, fid, fty, name, color
        )
        if update_bytes:
            await self.apply_doc_update_web(
                workspace_id, database_id, update_bytes, collab_type=1
            )
        return {
            "database_id": database_id,
            "field_id": fid,
            "field_name": field.get("name"),
            "option_id": opt_id,
            "name": name,
            "color": color,
            "existing": is_existing,
        }
