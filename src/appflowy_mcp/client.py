import asyncio
import time
from typing import Any

import httpx


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

    async def update_page_collab(
        self,
        workspace_id: str,
        object_id: str,
        encoded_collab_v1: bytes,
        collab_type: int = 0,
    ) -> None:
        """Replace document content. encoded_collab_v1 must be bincode-serialized
        EncodedCollab struct (state_vector + doc_state + version) — see doc_builder.
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
