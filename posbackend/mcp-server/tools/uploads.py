"""
uploads app ke MCP tools: Cloudinary signed uploads (product images, expense documents, campaign media).

Backend route (POST /api/uploads/signature/, owner/manager):
  Backend sirf SIGNATURE deta hai (API secret server se bahar nahi jata); file BROWSER (ya client) seedha
  Cloudinary ko upload karta hai, Django se guzarti nahi. Upload ke baad Cloudinary ka `secure_url` wo URL hai
  jo baqi tools mein chahiye:
    * product `image`                      (catalog: product_create / product_update)
    * campaign `media_url`                 (marketing: campaign_create / campaign_update; sirf apna Cloudinary URL)
    * expense `attachment_url`             (purchasing: expense_create / expense_update; https:// zaroori)
    * supplier-document `pdf_url`          (purchasing)

Tools:
  uploads_signature     -> signature + upload parameters (jaisa backend deta hai)
  uploads_upload_file   -> convenience: signature le kar LOCAL file Cloudinary par upload karta hai aur URL wapas deta hai
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Literal

import httpx

from client import POSAPIError, POSClient

UploadKind = Literal["image", "document", "expense", "campaign_image", "campaign_video"]


def register(mcp, client: POSClient) -> None:
    @mcp.tool()
    async def uploads_signature(kind: UploadKind | None = None) -> Any:
        """Cloudinary upload signature (owner/manager). `kind` (optional): 'image' (default; anything else bhi image
        maana jata hai), 'document', 'expense', 'campaign_image', 'campaign_video' - kind folder aur allowed types
        tay karta hai. Response: {signature, timestamp, folder, api_key, cloud_name, upload_url, max_bytes,
        allowed_types}. Signature kuch der ke liye valid hai (timestamp se), is liye foran istemal karein.
        File Cloudinary ko `upload_url` par multipart POST (fields: file, api_key, timestamp, signature, folder)
        se jati hai. Cloudinary configured na ho to 503 error.uploadsNotConfigured.
        Seedha file upload ke liye uploads_upload_file use karein."""
        return await client.post("uploads/signature/", {"kind": kind} if kind else {})

    @mcp.tool()
    async def uploads_upload_file(file_path: str, kind: UploadKind = "image") -> Any:
        """LOCAL file ko Cloudinary par upload karke uska URL wapas deta hai (owner/manager): pehle backend se
        signature leta hai (uploads_signature), file ka size (max_bytes) aur type (allowed_types) check karta hai,
        phir file seedha Cloudinary ko bhejta hai (backend ko file nahi jati aur POS ka token Cloudinary ko nahi
        bheja jata). `file_path` MCP server wali machine ka raasta hai. Result: {secure_url, public_id,
        resource_type, format, bytes, width, height} (jo Cloudinary ne diya). `secure_url` ko product `image`,
        campaign `media_url` ya expense `attachment_url` mein dein. Errors: file nahi mili, file bohot bari
        (max_bytes), type allowed nahi (allowed_types), Cloudinary ka error message (4xx).
        NOTE: ye standard Cloudinary signed-upload flow (file, api_key, timestamp, signature, folder) par chalta
        hai; agar backend signature mein koi aur parameter bhi sign karta ho to Cloudinary 'Invalid Signature' dega."""
        target = Path(file_path).expanduser()
        if not target.is_file():
            raise POSAPIError(0, f"File nahi mili: {target}", None, "POST", "uploads/signature/")
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"

        sig = await client.post("uploads/signature/", {"kind": kind})
        max_bytes = sig.get("max_bytes")
        allowed = sig.get("allowed_types") or []
        if max_bytes and len(content) > int(max_bytes):
            raise POSAPIError(0, f"File bohot bari hai: {len(content)} bytes (max {max_bytes})", sig, "POST", "uploads/signature/")
        if allowed and mime not in allowed:
            raise POSAPIError(0, f"File type '{mime}' allowed nahi; allowed: {', '.join(allowed)}", sig, "POST", "uploads/signature/")

        fields = {
            "api_key": str(sig["api_key"]),
            "timestamp": str(sig["timestamp"]),
            "signature": str(sig["signature"]),
            "folder": str(sig["folder"]),
        }
        # Alag client: POS ka Authorization header Cloudinary ko kabhi nahi jana chahiye.
        async with httpx.AsyncClient(timeout=120.0) as cloud:
            try:
                resp = await cloud.post(
                    sig["upload_url"], data=fields, files={"file": (target.name, content, mime)}
                )
            except httpx.HTTPError as exc:
                raise POSAPIError(0, f"Cloudinary tak pahunch nahi paya ({exc.__class__.__name__}: {exc})", None, "POST", sig["upload_url"])
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:300]}
        if resp.status_code >= 400:
            message = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else str(data)
            raise POSAPIError(resp.status_code, f"Cloudinary: {message}", data, "POST", sig["upload_url"])
        keep = ("secure_url", "url", "public_id", "resource_type", "format", "bytes", "width", "height")
        return {k: data[k] for k in keep if k in data}
