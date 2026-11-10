"""
kitchen app ke MCP tools: kitchen stations (BBQ, Fryer, Desserts, ...) aur routing settings.

Backend routes (kitchen/urls.py -> /api/kitchen-stations/):
  GET    kitchen-stations/                 koi bhi staff   (kitchen screen ke station chips, product/staff forms)
  POST   kitchen-stations/                 owner/manager
  GET    kitchen-stations/<id>/            koi bhi staff
  PATCH  kitchen-stations/<id>/            owner/manager   (PUT bhi hai, PATCH kaafi)
  GET    kitchen-stations/summary/         owner/manager   (setup checklist)
  GET    kitchen-stations/settings/        koi bhi staff
  PATCH  kitchen-stations/settings/        owner/manager
  (DELETE backend mein nahi hai: station ko `is_active=False` se band karte hain, taake purani order lines
   ki history bani rahe.)

Station kaise kaam karta hai:
  * Dish ka station = dish ka apna station, warna uski category ka station (catalog tools mein `station`);
    kuch bhi set na ho to 'General'. 'General' koi row nahi hai: station null = General, aur har kitchen
    screen ko General ki lines dikhti hain.
  * Chef ek ya zyada stations par kaam karta hai (accounts tools mein `stations`, `is_head_chef`);
    chef ki screen sirf apne stations + General dikhati hai. Head chef sab stations dekhta hai.
  * Line ka station kitchen ko FIRE hote waqt fix ho jata hai (orders tools). Band (inactive) station nayi
    lines General par bhejta hai; purani lines us station ki history mein rehti hain.
  * Station "staffed" tab maana jata hai jab uska koi chef kaam par ho; branch setting
    `kitchen_requires_clock_in` on ho to sirf clocked-in chefs ginte hain, off ho to har active chef.
    Unstaffed station ki lines har kitchen screen par dikhti hain.

Pagination: agar backend ka global pagination lagta hai to list {count,next,previous,results} deti hai
(default 25/page, `page_size` max 200); warna seedhi list. Dono sambhal liye jate hain.
"""
from __future__ import annotations

from typing import Any

from client import POSClient, drop_none


def register(mcp, client: POSClient) -> None:
    # ============================================================ stations
    @mcp.tool()
    async def kitchen_stations_list(
        active: bool | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ke kitchen stations (koi bhi staff), sort_order ke mutabiq. `active=True` sirf chalu,
        `active=False` sirf band stations (na do to sab). Har station mein:
        id, name_en, name_ar, sort_order, is_active,
        chefs [{id, name, on_duty, is_head_chef}] (sirf active kitchen-role staff; `on_duty` = abhi station ke
        liye ginta hai ya nahi, branch setting kitchen_requires_clock_in ke mutabiq),
        dish_count (dishes jo asal mein yahan jati hain: dish ka apna station, warna category ka),
        own_dish_count (sirf wo jin par station seedha set hai), category_count (categories jin ka default
        yahi hai), is_mine (requester khud yahan chef hai)."""
        return await client.get(
            "kitchen-stations/", {"active": active, "page": page, "page_size": page_size}
        )

    @mcp.tool()
    async def kitchen_station_get(station_id: int) -> Any:
        """Ek kitchen station ki detail (koi bhi staff): chefs, dish/category counts, is_mine
        (fields kitchen_stations_list jaise)."""
        return await client.get(f"kitchen-stations/{station_id}/")

    @mcp.tool()
    async def kitchen_station_create(
        name_en: str,
        name_ar: str,
        sort_order: int | None = None,
        is_active: bool = True,
    ) -> Any:
        """Naya station (owner/manager). Naam (name_en/name_ar, case ignore karke) branch mein unique, warna
        error.stationNameExists (error mein wo field jo clash hui); naam ke liye bilingual rule lagta hai, is
        liye dono dena safe hai. `sort_order` na do to sabse aakhir mein lagta hai (pichla + 1).
        Station banane ke baad dishes/categories par `station` (catalog tools) aur chefs par `stations`
        (staff_update) set karein, tab hi lines wahan routing hongi."""
        return await client.post(
            "kitchen-stations/",
            drop_none(
                {"name_en": name_en, "name_ar": name_ar, "sort_order": sort_order, "is_active": is_active}
            ),
        )

    @mcp.tool()
    async def kitchen_station_update(
        station_id: int,
        name_en: str | None = None,
        name_ar: str | None = None,
        sort_order: int | None = None,
        is_active: bool | None = None,
    ) -> Any:
        """Station PATCH (owner/manager); sirf diye gaye fields badalte hain. Station DELETE nahi hota: hataane
        ke liye `is_active=False` (nayi lines General par jayengi, purani lines ki history rehti hai).
        Naam ke rules create jaise (error.stationNameExists, bilingual pair)."""
        body = drop_none(
            {"name_en": name_en, "name_ar": name_ar, "sort_order": sort_order, "is_active": is_active}
        )
        return await client.patch(f"kitchen-stations/{station_id}/", body)

    @mcp.tool()
    async def kitchen_stations_summary() -> Any:
        """Kitchen setup checklist (owner/manager): station_count, active_station_count, dish_count,
        general_dish_count (dishes jo General par jati hain: na dish ka station, na category ka),
        unstaffed_stations [{id, name_en, name_ar}] (active stations jin par koi chef kaam hi nahi karta; clock-in
        nahi dekha jata, ye set-up hai), kitchen_staff_count, kitchen_staff_without_station (active kitchen
        staff jin ka koi station nahi). Station banane ke baad 'kuch reh to nahi gaya' check karne ke liye."""
        return await client.get("kitchen-stations/summary/")

    # ============================================================ settings
    @mcp.tool()
    async def kitchen_settings_get() -> Any:
        """Branch ki kitchen routing settings (koi bhi staff): kitchen_requires_clock_in (station tab staffed
        jab chef clocked-in ho), aur mine_includes_general (requester ki 'mine' screen mein General ki lines
        shamil hain ya nahi)."""
        return await client.get("kitchen-stations/settings/")

    @mcp.tool()
    async def kitchen_settings_update(kitchen_requires_clock_in: bool) -> Any:
        """Kitchen routing setting badalna (owner/manager): `kitchen_requires_clock_in=True` ho to station
        tab 'staffed' maana jata hai jab uska koi chef clocked-in ho (warna uski lines har kitchen screen par
        dikhti hain); False (default) par har active chef gina jata hai, clock-in ho ya nahi - un restaurants
        ke liye jo attendance istemal nahi karte. Updated settings wapas aati hain."""
        return await client.patch(
            "kitchen-stations/settings/", {"kitchen_requires_clock_in": kitchen_requires_clock_in}
        )
