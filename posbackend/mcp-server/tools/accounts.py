"""
accounts app ke tools.

Backend routes (config/urls.py mein /api/ ke neeche):
  GET   auth/login-choices/            public
  POST  auth/pin-login/                public (10/min throttle)
  POST  auth/refresh/                  public
  POST  auth/logout/                   any staff (server par no-op)
  GET/PATCH branches/active/attendance/  owner/manager
  staff/            CRUD               owner/manager
  staff/{id}/shifts|clock-in|clock-out self ya owner/manager
  shift-templates/  CRUD               owner/manager

Tokens kabhi tool output mein nahi aate (LLM context mein leak na ho).
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none
from tools._utils import location_payload

Role = Literal["owner", "manager", "cashier", "waiter", "kitchen"]


def register(mcp, client: POSClient) -> None:
    # ---------------------------------------------------------------- auth
    @mcp.tool()
    async def auth_login_choices(branch: int | None = None) -> Any:
        """Login screen ka data (public endpoint): branch info aur active staff ki list
        (id, name, role). `branch` na do to sabse chhoti id wali branch aati hai."""
        return await client.get("auth/login-choices/", {"branch": branch}, auth=False)

    @mcp.tool()
    async def auth_login() -> Any:
        """.env ke STAFF_ID/PIN se dobara PIN login karta hai aur logged-in staff
        (id, branch, name, role) wapas deta hai. Tokens return nahi hote.
        Note: PIN login par 10 requests/minute ki limit hai."""
        return await client.login()

    @mcp.tool()
    async def auth_whoami() -> Any:
        """Abhi MCP kis staff account (aur role) se logged in hai. Zaroorat ho to khud login kar leta hai."""
        if client.staff is None:
            await client.login()
        return client.staff

    @mcp.tool()
    async def auth_refresh_token() -> Any:
        """Refresh token se naya access token banata hai (POST auth/refresh/).
        Normally zaroorat nahi, client khud refresh karta hai."""
        ok = await client.refresh()
        if not ok:
            await client.login()
        return {"refreshed": ok, "re_logged_in": not ok}

    @mcp.tool()
    async def auth_logout() -> Any:
        """POST auth/logout/. Server par token blacklist nahi hai (no-op), is liye MCP apne
        tokens bhi clear karta hai; agla tool call automatically dobara login kar lega."""
        result = await client.post("auth/logout/")
        client.clear_tokens()
        return result

    # --------------------------------------------------------------- staff
    @mcp.tool()
    async def staff_list(
        search: str | None = None,
        role: Role | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ka staff (owner/manager only). `search` naam par, `role` filter,
        `ordering` keys: name, role ('-' prefix descending; default name). Pagination: default 25/page,
        `page_size` max 200. Har row mein kitchen chefs ke liye `stations` (station ids ki list) aur
        `is_head_chef`."""
        return await client.get(
            "staff/",
            {"search": search, "role": role, "ordering": ordering, "page": page, "page_size": page_size},
        )

    @mcp.tool()
    async def staff_get(staff_id: int) -> Any:
        """Ek staff member ki detail (owner/manager only)."""
        return await client.get(f"staff/{staff_id}/")

    @mcp.tool()
    async def staff_create(
        branch: int,
        name: str,
        role: Role,
        pin: str,
        is_active: bool = True,
        stations: list[int] | None = None,
        is_head_chef: bool | None = None,
    ) -> Any:
        """Naya staff member banata hai (owner/manager only).
        `branch` requester ki apni branch ki ID honi chahiye. `pin` branch mein unique hona chahiye
        (warna error.pinTaken). KITCHEN role ke liye optional: `stations` (isi branch ke kitchen station ids;
        chef ki screen un stations ki lines plus General dikhati hai) aur `is_head_chef` (head chef/expo:
        saare stations dekhta hai, lines stations ke darmiyan move karta hai, finished orders waiter ko
        handover karta hai). Kitchen ke ilawa kisi role par stations khali aur is_head_chef False save hote
        hain (chup-chaap). Errors: error.stationNotFound, error.stationBranchMismatch."""
        body = drop_none(
            {
                "branch": branch,
                "name": name,
                "role": role,
                "pin": pin,
                "is_active": is_active,
                "stations": stations,
                "is_head_chef": is_head_chef,
            }
        )
        return await client.post("staff/", body)

    @mcp.tool()
    async def staff_update(
        staff_id: int,
        name: str | None = None,
        role: Role | None = None,
        pin: str | None = None,
        is_active: bool | None = None,
        branch: int | None = None,
        stations: list[int] | None = None,
        is_head_chef: bool | None = None,
    ) -> Any:
        """Staff ko PATCH karta hai (owner/manager only); sirf diye gaye fields badalte hain.
        PIN dene par naya PIN set hota hai. Deactivate karne ke liye is_active=False.
        `stations`: kitchen chef ke stations POORI list se REPLACE hote hain (`[]` = sab hata do); na do to
        jaise hain waise. Role kitchen se badal kar kuch aur karein to stations khud saaf aur is_head_chef
        False ho jata hai. Errors: error.pinTaken, error.stationNotFound, error.stationBranchMismatch."""
        body = drop_none(
            {
                "name": name,
                "role": role,
                "pin": pin,
                "is_active": is_active,
                "branch": branch,
                "stations": stations,
                "is_head_chef": is_head_chef,
            }
        )
        return await client.patch(f"staff/{staff_id}/", body)

    @mcp.tool()
    async def staff_delete(staff_id: int) -> Any:
        """Staff delete karta hai (owner/manager only; soft-delete). Agar sirf login band karna
        hai to staff_update(is_active=False) behtar hai."""
        return await client.delete(f"staff/{staff_id}/")

    # -------------------------------------------------------------- shifts
    @mcp.tool()
    async def staff_shifts(staff_id: int) -> Any:
        """Staff ki shifts (clock-in/out, is_late), naye pehle. Sirf khud ki ya owner/manager."""
        return await client.get(f"staff/{staff_id}/shifts/")

    @mcp.tool()
    async def staff_clock_in(
        staff_id: int,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
        location_timestamp: float | None = None,
    ) -> Any:
        """Clock-in (khud ka ya manager kisi aur ka). Backend GPS geofence check karta hai
        (common/geofence.py): `latitude`, `longitude`, `accuracy_m` (meters) teeno ek saath do;
        `location_timestamp` unix SECONDS (na do to abhi ka waqt). Ye ASLI device GPS fix honi chahiye.
        Rules: fix 2 minute se purani nahi (error.locationStale) aur distance + accuracy_m branch ki
        attendance_radius_m ke andar (error.outsideRestaurantLocation; error mein distance_m,
        allowed_radius_m wagera aate hain). Branch ki location set na ho to error.locationNotConfigured
        (pehle branch_location_update). Baqi errors: alreadyClockedIn, maxDailyClockInsReached."""
        body = location_payload(latitude, longitude, accuracy_m, location_timestamp)
        return await client.post(f"staff/{staff_id}/clock-in/", body)

    @mcp.tool()
    async def staff_clock_out(
        staff_id: int,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
        location_timestamp: float | None = None,
    ) -> Any:
        """Clock-out (khud ka ya manager kisi aur ka); location rules clock-in jaise
        (latitude + longitude + accuracy_m, taaza GPS fix).
        Errors: notClockedIn, maxDailyClockOutsReached."""
        body = location_payload(latitude, longitude, accuracy_m, location_timestamp)
        return await client.post(f"staff/{staff_id}/clock-out/", body)

    # ------------------------------------------------------- shift templates
    @mcp.tool()
    async def shift_templates_list() -> Any:
        """Apni branch ke shift templates (naam, start/end time, active, assigned_staff). Owner/manager."""
        return await client.get("shift-templates/")

    @mcp.tool()
    async def shift_template_get(template_id: int) -> Any:
        """Ek shift template ki detail. Owner/manager."""
        return await client.get(f"shift-templates/{template_id}/")

    @mcp.tool()
    async def shift_template_create(
        name: str,
        start_time: str,
        end_time: str,
        active: bool = True,
        assigned_staff_ids: list[int] | None = None,
    ) -> Any:
        """Naya shift template. Times 'HH:MM' ya 'HH:MM:SS'; end < start ho to shift midnight cross
        karti hai. start == end allowed nahi. Naam branch mein unique. assigned_staff_ids isi branch ke
        staff honay chahiye."""
        return await client.post(
            "shift-templates/",
            drop_none(
                {
                    "name": name,
                    "start_time": start_time,
                    "end_time": end_time,
                    "active": active,
                    "assigned_staff_ids": assigned_staff_ids,
                }
            ),
        )

    @mcp.tool()
    async def shift_template_update(
        template_id: int,
        name: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        active: bool | None = None,
        assigned_staff_ids: list[int] | None = None,
    ) -> Any:
        """Shift template PATCH. assigned_staff_ids dene par assignment REPLACE hoti hai
        (list mein na hone wale staff se template hat jata hai); na do to assignments waisi hi rehti hain."""
        body = drop_none(
            {
                "name": name,
                "start_time": start_time,
                "end_time": end_time,
                "active": active,
                "assigned_staff_ids": assigned_staff_ids,
            }
        )
        return await client.patch(f"shift-templates/{template_id}/", body)

    @mcp.tool()
    async def shift_template_delete(template_id: int) -> Any:
        """Shift template delete (owner/manager). Assigned staff ka template NULL ho jata hai."""
        return await client.delete(f"shift-templates/{template_id}/")

    # ----------------------------------------------------- attendance settings
    @mcp.tool()
    async def attendance_settings_get() -> Any:
        """Active branch ki attendance settings: max_clock_ins_per_day, max_clock_outs_per_day,
        late_threshold_minutes. Owner/manager."""
        return await client.get("branches/active/attendance/")

    @mcp.tool()
    async def attendance_settings_update(
        max_clock_ins_per_day: int | None = None,
        max_clock_outs_per_day: int | None = None,
        late_threshold_minutes: int | None = None,
    ) -> Any:
        """Attendance settings PATCH. Limits: clock-ins/outs 1-24, late threshold 0-1440 minutes."""
        return await client.patch(
            "branches/active/attendance/",
            drop_none(
                {
                    "max_clock_ins_per_day": max_clock_ins_per_day,
                    "max_clock_outs_per_day": max_clock_outs_per_day,
                    "late_threshold_minutes": late_threshold_minutes,
                }
            ),
        )
