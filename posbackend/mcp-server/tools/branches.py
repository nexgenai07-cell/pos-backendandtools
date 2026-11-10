"""
branches app ke MCP tools.

Backend routes (branches/urls.py):
  GET   branches/public/            public (AllowAny, "menu" throttle) -> sirf name/logo/currency
  GET   branches/active/            any logged-in staff -> apni branch ki detail
  PATCH branches/<pk>/              owner/manager -> branch info update (sirf apni branch)
  GET   branches/active/location/   owner/manager -> coordinates + radius
  PATCH branches/active/location/   owner/manager -> coordinates + radius update

Note: "active branch" = request karne wale staff ki apni branch. Doosri branch ki id par
PATCH karoge to backend 404 dega (leak nahi karta ke wo exist karti hai).
"""
from __future__ import annotations

from typing import Any

from client import POSClient, drop_none


def register(mcp, client: POSClient) -> None:
    # ------------------------------------------------------------- read tools
    @mcp.tool()
    async def branch_public_get(branch: int | None = None) -> Any:
        """Customer-facing branding (public endpoint, login ki zaroorat nahi):
        id, name_en, name_ar, logo_url, favicon_url, currency.
        `branch` na do to sabse chhoti id wali branch aati hai. Error: error.noBranch (404)."""
        return await client.get("branches/public/", {"branch": branch}, auth=False)

    @mcp.tool()
    async def branch_active_get() -> Any:
        """Logged-in staff ki apni branch ki poori detail: naam (en/ar), address, timezone,
        currency, logo_url, favicon_url, created_at, updated_at. Koi bhi role."""
        return await client.get("branches/active/")

    @mcp.tool()
    async def branch_location_get() -> Any:
        """Branch ki location aur radius (owner/manager only): latitude, longitude,
        geofence_radius_m, attendance_radius_m. Coordinates sirf isi endpoint se milte hain."""
        return await client.get("branches/active/location/")

    # ----------------------------------------------------------- write tools
    @mcp.tool()
    async def branch_update(
        branch_id: int,
        name_en: str | None = None,
        name_ar: str | None = None,
        address: str | None = None,
        timezone: str | None = None,
        currency: str | None = None,
        logo_url: str | None = None,
        favicon_url: str | None = None,
    ) -> Any:
        """Branch info PATCH (owner/manager only); sirf diye gaye fields badalte hain.
        `branch_id` apni hi branch ki honi chahiye (branch_active_get se `id` lein), warna 404.
        `timezone` IANA naam (jaise 'Asia/Riyadh'), `currency` code (jaise 'SAR').
        Naam ke liye backend bilingual rule lagata hai: jo language admin UI mein active hai
        uska naam dena zaroori ho sakta hai."""
        body = drop_none(
            {
                "name_en": name_en,
                "name_ar": name_ar,
                "address": address,
                "timezone": timezone,
                "currency": currency,
                "logo_url": logo_url,
                "favicon_url": favicon_url,
            }
        )
        return await client.patch(f"branches/{branch_id}/", body)

    @mcp.tool()
    async def branch_location_update(
        latitude: float | None = None,
        longitude: float | None = None,
        geofence_radius_m: float | None = None,
        attendance_radius_m: float | None = None,
    ) -> Any:
        """Branch location PATCH (owner/manager only); sirf diye gaye fields badalte hain.
        Ranges: latitude -90..90, longitude -180..180, geofence_radius_m 0.01..10000,
        attendance_radius_m 1..10000 (meters; clock-in/out is radius ke andar hi allowed hai).
        Agar branch par pehle se location nahi hai to latitude aur longitude DONO dena zaroori
        hai (warna error.locationCoordinatesPairRequired)."""
        # DecimalField precision: lat/long 6 decimals, radius 2 decimals. Round karke bhejte hain
        # taake float ki lambi repr (e.g. 24.8607000000001) validation fail na kare.
        body = drop_none(
            {
                "latitude": round(latitude, 6) if latitude is not None else None,
                "longitude": round(longitude, 6) if longitude is not None else None,
                "geofence_radius_m": round(geofence_radius_m, 2) if geofence_radius_m is not None else None,
                "attendance_radius_m": round(attendance_radius_m, 2) if attendance_radius_m is not None else None,
            }
        )
        return await client.patch("branches/active/location/", body)
