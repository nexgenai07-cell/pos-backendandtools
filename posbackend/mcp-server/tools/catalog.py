"""
catalog app ke MCP tools: menu, categories, products, deals, variants.

Backend routes (catalog/urls.py):
  GET    menu/                                     public (AllowAny) customer menu
  CRUD   categories/                               read: koi bhi staff | write: owner/manager
  CRUD   products/                                 read: koi bhi staff | write: owner/manager
  POST   products/bulk-station/                    owner/manager (kai dishes ka kitchen station)
  PUT    products/<id>/deal/                       owner/manager (deal create-or-replace)
  DELETE products/<id>/deal/                       owner/manager
  CRUD   products/<product_id>/variants/[<id>/]    read: koi bhi staff | write: owner/manager

Design notes:
  * Sab data requester ki apni branch tak scoped hai.
  * PUT (full update) ke liye alag tool nahi banaya: PATCH `*_update` tools partial update
    karte hain aur PUT ka kaam bhi kar dete hain.
  * Pagination (common/pagination.py): list endpoints default 25 rows/page; `page_size` max 200.
    Response shape: {count, next, previous, results, metadata}. `metadata` mein poori selection
    ke stats hote hain (jaise products_list mein `with_recipe`), sirf current page ke nahi.
  * Prices/multipliers float mein lete hain aur DecimalField ki precision ke mutabiq round
    karke bhejte hain (price 2, recipe_multiplier 3 decimals).
  * `name_en` / `name_ar`: backend bilingual rule lagata hai (jo language admin UI mein active
    hai uska naam zaroori ho sakta hai) -> dono dena safe hai.
"""
from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict

from client import POSClient, drop_none

Weekday = Literal["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
Badge = Literal["New", "Popular", "Deal", ""]  # "" = badge hata do


class DealWindow(TypedDict):
    """Deal ka ek time window (din + start/end time)."""

    day: Weekday
    start_time: str  # "HH:MM" ya "HH:MM:SS"; start < end honi chahiye
    end_time: str


def _money(value: float | None) -> float | None:
    """Price ko 2 decimals par round (DecimalField(max_digits=10, decimal_places=2))."""
    return round(value, 2) if value is not None else None


def register(mcp, client: POSClient) -> None:
    # ============================================================ public menu
    @mcp.tool()
    async def menu_public_get(branch: int | None = None) -> Any:
        """Customer-facing menu (public, login ki zaroorat nahi): categories -> products, deal price
        pehle se resolve, sirf orderable aur recipe-wali items. Variants mein active sizes. Branch ki
        timezone ke mutabiq aaj ka time use hota hai. `branch` na do to sabse chhoti id wali branch."""
        return await client.get("menu/", {"branch": branch}, auth=False)

    # ============================================================== categories
    @mcp.tool()
    async def categories_list(
        search: str | None = None,
        has_products: bool | None = None,
        ordering: str | None = None,
        language: Literal["en", "ar"] | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ki categories (koi bhi staff). Har row mein `product_count`.
        `search` naam par; `has_products` True/False; `ordering` keys: name, products, order
        ('-' prefix descending, jaise '-products'); `language` 'ar' ho to name ordering Arabic par.
        Pagination: default 25/page, `page_size` max 200."""
        return await client.get(
            "categories/",
            {
                "search": search,
                "has_products": has_products,
                "ordering": ordering,
                "language": language,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def category_get(category_id: int) -> Any:
        """Ek category ki detail (koi bhi staff)."""
        return await client.get(f"categories/{category_id}/")

    @mcp.tool()
    async def category_create(
        branch: int,
        name_en: str,
        name_ar: str,
        sort_order: int = 0,
        station: int | None = None,
    ) -> Any:
        """Nayi category (owner/manager). `branch` apni branch ki id honi chahiye.
        Naam (name_en, case-insensitive) branch mein unique, warna error.duplicateName.
        `station` (optional): kitchen station id jahan is category ki dishes ki lines jati hain jab tak dish
        apna station set na kare; na do to General. Station isi branch ka hona chahiye
        (error.stationNotFound / error.stationBranchMismatch)."""
        return await client.post(
            "categories/",
            drop_none(
                {
                    "branch": branch,
                    "name_en": name_en,
                    "name_ar": name_ar,
                    "sort_order": sort_order,
                    "station": station,
                }
            ),
        )

    @mcp.tool()
    async def category_update(
        category_id: int,
        name_en: str | None = None,
        name_ar: str | None = None,
        sort_order: int | None = None,
        station: int | None = None,
        clear_station: bool = False,
    ) -> Any:
        """Category PATCH (owner/manager); sirf diye gaye fields badalte hain.
        `station` = kitchen station id (is category ki dishes ka default station); station hatane ke liye
        `clear_station=True` (category phir General par). Errors: error.stationNotFound,
        error.stationBranchMismatch."""
        body = drop_none({"name_en": name_en, "name_ar": name_ar, "sort_order": sort_order, "station": station})
        if clear_station:
            body["station"] = None  # explicit null: station hata do
        return await client.patch(f"categories/{category_id}/", body)

    @mcp.tool()
    async def category_delete(category_id: int) -> Any:
        """Category delete (owner/manager). Agar us mein products hain to 409 error.categoryInUse;
        pehle products doosri category mein move ya delete karein."""
        return await client.delete(f"categories/{category_id}/")

    # =============================================================== products
    @mcp.tool()
    async def products_list(
        category: int | None = None,
        available: bool | None = None,
        search: str | None = None,
        badge: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        has_recipe: bool | None = None,
        has_deal: bool | None = None,
        station: str | None = None,
        ordering: str | None = None,
        language: Literal["en", "ar"] | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ke products (koi bhi staff). Har product mein price, costing (live margin),
        deal, variants, is_orderable, available_quantity.
        Filters: `category` id, `available` (is_available flag), `search` (naam/description/category),
        `badge` ('New'|'Popular'|'Deal'|'none'|'all'), `price_min`/`price_max`, `has_recipe`,
        `has_deal` (sirf True filter karta hai), `station` (kitchen station id ya 'general': wo dishes jin ki
        lines wahan jati hain - dish ka apna station ya category se mila hua; 'general' = kisi station par nahi).
        `ordering` keys: name, category, price, available, ingredients, cost, margin ('-' = descending).
        Pagination: default 25/page, `page_size` max 200; response mein `metadata.with_recipe`
        (poori selection mein recipe wale products ki ginti) bhi aata hai."""
        return await client.get(
            "products/",
            {
                "category": category,
                "available": available,
                "search": search,
                "badge": badge,
                "price_min": price_min,
                "price_max": price_max,
                "has_recipe": has_recipe,
                # Backend sirf "true" string pehchanta hai; False par filter lagana hi nahi.
                "has_deal": True if has_deal else None,
                "station": station,
                "ordering": ordering,
                "language": language,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def product_get(product_id: int) -> Any:
        """Ek product ki poori detail (koi bhi staff): deal, variants, costing."""
        return await client.get(f"products/{product_id}/")

    @mcp.tool()
    async def product_create(
        branch: int,
        category: int,
        name_en: str,
        name_ar: str,
        price: float,
        description_en: str | None = None,
        description_ar: str | None = None,
        cost_price: float | None = None,
        image: str | None = None,
        is_available: bool = True,
        badge: Badge | None = None,
        days: list[Weekday] | None = None,
        station: int | None = None,
    ) -> Any:
        """Naya product (owner/manager). `branch` apni branch, `category` us branch ki category id.
        `cost_price` na do to 0 set hota hai (recipe add hone par cost recipe se nikalti hai).
        `days` khali/None = har din available; warna weekday codes ('sun'..'sat').
        `image` ek URL (Cloudinary). Name branch mein unique (error.duplicateName).
        `station` (optional): is dish ka kitchen station id (category ke station par override); na do to dish
        category ka station follow karti hai (wo bhi na ho to General). Response mein `effective_station`
        {source: product|category|general, station} batata hai ke lines asal mein kahan jayengi."""
        body = drop_none(
            {
                "branch": branch,
                "category": category,
                "name_en": name_en,
                "name_ar": name_ar,
                "description_en": description_en,
                "description_ar": description_ar,
                "price": _money(price),
                "cost_price": _money(cost_price),
                "image": image,
                "is_available": is_available,
                "badge": badge,
                "days": days,
                "station": station,
            }
        )
        return await client.post("products/", body)

    @mcp.tool()
    async def product_update(
        product_id: int,
        category: int | None = None,
        name_en: str | None = None,
        name_ar: str | None = None,
        price: float | None = None,
        description_en: str | None = None,
        description_ar: str | None = None,
        cost_price: float | None = None,
        image: str | None = None,
        is_available: bool | None = None,
        badge: Badge | None = None,
        days: list[Weekday] | None = None,
        station: int | None = None,
        clear_station: bool = False,
    ) -> Any:
        """Product PATCH (owner/manager); sirf diye gaye fields badalte hain.
        `cost_price` na do aur product ki recipe ho to cost recipe se khud sync ho jati hai.
        `badge=""` badge hata deta hai. `days=[]` ka matlab har din.
        Deal wale product ki price badalte waqt dhyan: deal price hamesha nayi price se kam honi chahiye.
        `station` = is dish ka kitchen station id; `clear_station=True` se dish wapas category ke station ko
        follow karti hai. Station sirf tab padha jata hai jab line kitchen ko fire hoti hai, is liye pehle se
        fire ho chuki lines nahi badalti. Errors: error.stationNotFound, error.stationBranchMismatch."""
        body = drop_none(
            {
                "category": category,
                "name_en": name_en,
                "name_ar": name_ar,
                "price": _money(price),
                "description_en": description_en,
                "description_ar": description_ar,
                "cost_price": _money(cost_price),
                "image": image,
                "is_available": is_available,
                "badge": badge,
                "days": days,
                "station": station,
            }
        )
        if clear_station:
            body["station"] = None  # explicit null: dish category ka station follow kare
        return await client.patch(f"products/{product_id}/", body)

    @mcp.tool()
    async def product_delete(product_id: int) -> Any:
        """Product delete (owner/manager, soft-delete). Pehle uski recipe bhi delete hoti hai.
        Sirf temporarily hatana ho to product_update(is_available=False) behtar hai."""
        return await client.delete(f"products/{product_id}/")


    @mcp.tool()
    async def products_bulk_set_station(product_ids: list[int], station: int | None = None) -> Any:
        """Kai dishes ka kitchen station ek saath set karna (POST products/bulk-station/; owner/manager) -
        menu ka "Set station". `station=None` dene se un dishes ka apna station hat jata hai aur wo dobara
        category ke station ko follow karti hain. `product_ids` khali nahi honi chahiye (error.productsRequired);
        station isi branch ka (400 error.stationNotFound); koi product id isi branch ki na ho to poori call 404
        error.notFound aur kuch nahi badalta. Har dish alag save hoti hai taake menu screens ko product:updated
        event milay. Result: {updated, station}."""
        # `station` ko explicit bhejna zaroori hai (None = clear), is liye drop_none nahi.
        return await client.post("products/bulk-station/", {"products": product_ids, "station": station})


    # ================================================================== deals
    @mcp.tool()
    async def product_deal_set(
        product_id: int,
        price: float,
        windows: list[DealWindow],
    ) -> Any:
        """Product ki deal create ya REPLACE karta hai (PUT; owner/manager). Ek product par ek hi deal.
        Rules: deal `price` product ki normal price se kam (error.dealPriceTooHigh), kam az kam ek
        window (error.dealWindowsRequired), har window mein start_time < end_time
        (error.dealWindowInvalid). Pehle wali windows poori replace hoti hain.
        Example windows: [{"day": "fri", "start_time": "11:00", "end_time": "14:00"}]"""
        return await client.put(
            f"products/{product_id}/deal/",
            {"price": _money(price), "windows": [dict(w) for w in windows]},
        )

    @mcp.tool()
    async def product_deal_delete(product_id: int) -> Any:
        """Product ki deal hata deta hai (owner/manager). Deal na ho tab bhi success (204)."""
        return await client.delete(f"products/{product_id}/deal/")

    # =============================================================== variants
    @mcp.tool()
    async def product_variants_list(product_id: int) -> Any:
        """Product ke sizes/portions (koi bhi staff), sort_order ke mutabiq. Har variant mein
        price, recipe_multiplier, is_active aur live costing."""
        return await client.get(f"products/{product_id}/variants/")

    @mcp.tool()
    async def product_variant_get(product_id: int, variant_id: int) -> Any:
        """Ek variant ki detail (koi bhi staff)."""
        return await client.get(f"products/{product_id}/variants/{variant_id}/")

    @mcp.tool()
    async def product_variant_create(
        product_id: int,
        name_en: str,
        name_ar: str,
        price: float,
        recipe_multiplier: float | None = None,
        is_active: bool = True,
        sort_order: int = 0,
    ) -> Any:
        """Product ka naya size (owner/manager), jaise 'Half KG', '1 KG'.
        `recipe_multiplier` recipe (aur stock/costing) ko scale karta hai: 1 KG = 2.0 matlab Half KG se
        double ingredients. Default 1.000 (range 0.001..999). Variant ka naam product ke andar unique
        (error.variantNameExists)."""
        body = drop_none(
            {
                "name_en": name_en,
                "name_ar": name_ar,
                "price": _money(price),
                # recipe_multiplier DecimalField(decimal_places=3)
                "recipe_multiplier": round(recipe_multiplier, 3) if recipe_multiplier is not None else None,
                "is_active": is_active,
                "sort_order": sort_order,
            }
        )
        return await client.post(f"products/{product_id}/variants/", body)

    @mcp.tool()
    async def product_variant_update(
        product_id: int,
        variant_id: int,
        name_en: str | None = None,
        name_ar: str | None = None,
        price: float | None = None,
        recipe_multiplier: float | None = None,
        is_active: bool | None = None,
        sort_order: int | None = None,
    ) -> Any:
        """Variant PATCH (owner/manager); sirf diye gaye fields badalte hain.
        Size ko menu se chhupana ho to is_active=False (delete ke bajaye)."""
        body = drop_none(
            {
                "name_en": name_en,
                "name_ar": name_ar,
                "price": _money(price),
                "recipe_multiplier": round(recipe_multiplier, 3) if recipe_multiplier is not None else None,
                "is_active": is_active,
                "sort_order": sort_order,
            }
        )
        return await client.patch(f"products/{product_id}/variants/{variant_id}/", body)

    @mcp.tool()
    async def product_variant_delete(product_id: int, variant_id: int) -> Any:
        """Variant delete (owner/manager, soft-delete)."""
        return await client.delete(f"products/{product_id}/variants/{variant_id}/")
