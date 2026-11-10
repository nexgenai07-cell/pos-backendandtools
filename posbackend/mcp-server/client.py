"""
HTTP client for the POS Django API.

- PIN login (POST auth/pin-login/) lazily, token cache karta hai.
- Access token expire hone se pehle refresh, refresh fail ho to dobara PIN login.
- 401 aaye to ek baar re-auth karke retry.
- Backend errors (`error.orderClosed` jaisay codes) ko POSAPIError mein badalta hai.

NOTE: stdio transport mein stdout MCP protocol ke liye hai, isliye yahan kahin
print() nahi; logging stderr par jati hai.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx
try:  # optional: .env file ho to load karo, warna MCP config ke env block se bhi chalega
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass
log = logging.getLogger("pos-mcp.client")

_EXPIRY_MARGIN_S = 60  # token ko expiry se 60s pehle refresh kar do


class POSAPIError(Exception):
    """Backend ne non-2xx diya."""

    def __init__(self, status: int, code: str, payload: Any, method: str, path: str):
        self.status = status
        self.code = code
        self.payload = payload
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> HTTP {status}: {code}")


def _flatten(payload: Any) -> str:
    """DRF errors ({"error": [...]}, {"pin": "error.pinTaken"}, {"detail": ...}) ko ek line mein."""
    if payload is None:
        return "unknown error"
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return "; ".join(_flatten(p) for p in payload)
    if isinstance(payload, dict):
        if "error" in payload:
            code = _flatten(payload["error"])
            # Kuch errors extra context bhi dete hain (e.g. error.tableHasOpenOrder ke saath
            # open_orders ki list) -> usay bhi message mein rakho taake model ko wo mil jaye.
            extra = {k: v for k, v in payload.items() if k != "error"}
            return f"{code} | {json.dumps(extra, default=str)}" if extra else code
        if "detail" in payload:
            return _flatten(payload["detail"])
        return "; ".join(f"{k}: {_flatten(v)}" for k, v in payload.items())
    return str(payload)


def _jwt_exp(token: str) -> float:
    """JWT ka `exp` (signature verify kiye baghair, sirf scheduling ke liye)."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return float(json.loads(base64.urlsafe_b64decode(body))["exp"])
    except Exception:
        return 0.0


def drop_none(data: dict[str, Any]) -> dict[str, Any]:
    """PATCH/POST body se None values hata do (sirf wohi bhejo jo diya gaya)."""
    return {k: v for k, v in data.items() if v is not None}


class POSClient:
    def __init__(
        self,
        base_url: str | None = None,
        staff_id: int | str | None = None,
        pin: str | None = None,
        timeout: float | None = None,
    ):
        base_url = base_url or os.getenv("API_BASE_URL", "http://127.0.0.1:8000/api")
        self.base_url = base_url.rstrip("/") + "/"
        self.staff_id = staff_id if staff_id is not None else os.getenv("STAFF_ID")
        self.pin = pin if pin is not None else os.getenv("PIN")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout or float(os.getenv("REQUEST_TIMEOUT", "30")),
        )
        self._access: str | None = None
        self._refresh: str | None = None
        self.staff: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth
    async def login(self) -> dict[str, Any]:
        """PIN login. Tokens client ke andar rehte hain, caller ko sirf staff info milti hai."""
        if not self.staff_id or not self.pin:
            raise POSAPIError(0, "STAFF_ID / PIN .env mein set nahi hain", None, "POST", "auth/pin-login/")
        data = await self._send(
            "POST", "auth/pin-login/",
            json={"staff_id": int(self.staff_id), "pin": str(self.pin)},
            auth=False,
        )
        self._access = data["access"]
        self._refresh = data["refresh"]
        self.staff = data.get("staff")
        return self.staff or {}

    async def refresh(self) -> bool:
        """Refresh token se naya access token. Fail ho to False."""
        if not self._refresh or _jwt_exp(self._refresh) - time.time() < _EXPIRY_MARGIN_S:
            return False
        try:
            data = await self._send("POST", "auth/refresh/", json={"refresh": self._refresh}, auth=False)
        except POSAPIError:
            return False
        self._access = data["access"]
        return True

    async def _reauth(self) -> None:
        if not await self.refresh():
            await self.login()

    async def _ensure_token(self) -> None:
        async with self._lock:
            if self._access and _jwt_exp(self._access) - time.time() > _EXPIRY_MARGIN_S:
                return
            await self._reauth()

    def clear_tokens(self) -> None:
        self._access = None
        self._refresh = None

    # --------------------------------------------------------------- requests
    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth: bool = True,
        raw: bool = False,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._access}"} if auth and self._access else {}
        try:
            resp = await self._http.request(
                method, path.lstrip("/"), params=params, json=json, data=data, files=files, headers=headers
            )
        except httpx.HTTPError as exc:
            raise POSAPIError(0, f"Backend tak pahunch nahi paya ({exc.__class__.__name__}: {exc})", None, method, path)

        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text[:500]
            message = _flatten(payload)
            # Throttled (429) par server batata hai kitni der baad dobara try karein (PIN login 10/min wagera).
            retry_after = resp.headers.get("Retry-After")
            if resp.status_code == 429 and retry_after:
                message += f" | retry_after_seconds={retry_after}"
            raise POSAPIError(resp.status_code, message, payload, method, path)

        if raw:  # binary download (PDF wagera): bytes wapas
            return resp.content

        if resp.status_code in (204, 205) or not resp.content:
            return {"success": True, "status": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth: bool = True,
        raw: bool = False,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        params = drop_none(params) if params else None
        if auth:
            await self._ensure_token()
        try:
            return await self._send(method, path, params=params, json=json, auth=auth, raw=raw, data=data, files=files)
        except POSAPIError as exc:
            if exc.status == 401 and auth:
                async with self._lock:
                    await self._reauth()
                return await self._send(method, path, params=params, json=json, auth=auth, raw=raw, data=data, files=files)
            raise

    # Convenience helpers
    async def get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True):
        return await self.request("GET", path, params=params, auth=auth)

    async def post(self, path: str, json: Any = None, *, auth: bool = True):
        return await self.request("POST", path, json=json, auth=auth)

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """Binary response (jaise PDF) ke liye; JSON parse nahi hota."""
        return await self.request("GET", path, params=params, raw=True)

    async def post_file(
        self,
        path: str,
        file_path: str,
        data: dict[str, Any] | None = None,
        *,
        raw: bool = False,
    ) -> Any:
        """multipart/form-data upload (field naam 'file'). File bytes memory mein padh li jati hain,
        is liye 401 par re-auth ke baad retry bhi safe hai. `raw=True` par response bytes (xlsx wagera)."""
        from pathlib import Path

        target = Path(file_path).expanduser()
        if not target.is_file():
            raise POSAPIError(0, f"File nahi mili: {target}", None, "POST", path)
        content = target.read_bytes()
        files = {"file": (target.name, content, "application/octet-stream")}
        return await self.request("POST", path, data=data, files=files, raw=raw)

    async def put(self, path: str, json: Any = None):
        return await self.request("PUT", path, json=json)

    async def patch(self, path: str, json: Any = None):
        return await self.request("PATCH", path, json=json)

    async def delete(self, path: str, params: dict[str, Any] | None = None):
        """DELETE; `params` optional query string (jaise lock_override_reason, jo backend DELETE par
        query string se padhta hai)."""
        return await self.request("DELETE", path, params=params)

    async def aclose(self) -> None:
        await self._http.aclose()
