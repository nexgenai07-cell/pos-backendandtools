"""
inventory app ke MCP tools: ingredients (stock), stock movements ledger, recipes.

Backend routes (inventory/urls.py) -- SAB owner/manager only:
  GET    inventory-items/                  list (filters + metadata)
  POST   inventory-items/                  create
  GET    inventory-items/<id>/             detail
  PATCH  inventory-items/<id>/             update (current_stock yahan se NAHI badalta)
  DELETE inventory-items/<id>/             delete (recipe mein use ho to 409 error.inventoryItemInUse)
  GET    inventory-items/low-stock/        sirf stock <= par_level wali items
  POST   inventory-items/<id>/adjust/      manual stock adjustment (waste / correction)
  GET    stock-movements/                  append-only ledger (read-only)
  GET    recipes/                          branch ki saari recipe lines ek call mein
  GET    products/<id>/recipe/             ek product ki recipe
  PUT    products/<id>/recipe/             poori recipe REPLACE
  DELETE products/<id>/recipe/             recipe clear

Design notes:
  * Stock sirf do tareeqon se badalta hai: automatic (order send-to-kitchen = 'sale', purchase receive =
    'purchase') ya manual `inventory_item_adjust_stock` (waste/adjustment). Har change ka ek
    StockMovement row banta hai jo kabhi edit/delete nahi hota.
  * Stock negative nahi ho sakta manual adjustment se (error.stockAdjustmentBelowZero); sales se
    discrepancy ho sakti hai, wo baad mein reconcile hoti hai.
  * Units: recipe unit aur stock unit compatible hone chahiye -- mass (kg/g), volume (l/ml) aur count
    (unit/units) ke darmiyan conversion hota hai; custom unit sirf exact match par chalta hai.
  * Quantities 3 decimals, cost 2 decimals par round hoti hain (DecimalField precision).
  * PUT (full update) ke liye alag tool nahi; `inventory_item_update` (PATCH) kaafi hai.
  * Pagination: default 25/page, `page_size` max 200; response {count,next,previous,results,metadata}.
"""
from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict

from client import POSClient, drop_none

StockStatus = Literal["out", "low", "ok"]
ManualReason = Literal["waste", "adjustment"]


class RecipeLine(TypedDict):
    """Recipe ki ek ingredient line."""

    inventory_item: int  # isi branch ki inventory item id
    quantity: float  # > 0, 3 decimals tak
    unit: str  # stock unit se compatible (jaise 'g' recipe, 'kg' stock)


def _qty(value: float | None) -> float | None:
    """Quantity/par level: DecimalField(decimal_places=3)."""
    return round(value, 3) if value is not None else None


def register(mcp, client: POSClient) -> None:
    # =================================================== inventory items
    @mcp.tool()
    async def inventory_items_list(
        search: str | None = None,
        supplier: int | None = None,
        unit: str | None = None,
        stock_status: StockStatus | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Inventory items (owner/manager). Har row mein current_stock, par_level, cost_per_unit,
        supplier, recipe_usage (kitne products is ingredient ko use karte hain).
        Filters: `search` naam par, `supplier` id, `unit`, `stock_status`:
        'out' (stock <= 0), 'low' (0 < stock <= par_level), 'ok' (stock > par_level).
        `ordering` keys: name, unit, stock, par, cost, value (stock x cost), usage ('-' = descending).
        metadata: low_stock (poori selection mein low/out ginti), stock_value (total value), units (branch ki
        sab units ki list)."""
        return await client.get(
            "inventory-items/",
            {
                "search": search,
                "supplier": supplier,
                "unit": unit,
                "stock_status": stock_status,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def inventory_items_low_stock(
        search: str | None = None,
        supplier: int | None = None,
        unit: str | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Reorder list: sirf wo items jinka stock par_level ya us se kam hai (out-of-stock bhi shamil).
        Baqi filters inventory_items_list jaise; paginated response."""
        return await client.get(
            "inventory-items/low-stock/",
            {
                "search": search,
                "supplier": supplier,
                "unit": unit,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def inventory_item_get(item_id: int) -> Any:
        """Ek inventory item ki detail (owner/manager)."""
        return await client.get(f"inventory-items/{item_id}/")

    @mcp.tool()
    async def inventory_item_create(
        branch: int,
        name: str,
        unit: str,
        current_stock: float = 0,
        par_level: float | None = None,
        cost_per_unit: float | None = None,
        supplier: int | None = None,
    ) -> Any:
        """Naya ingredient (owner/manager). `branch` apni branch ki id. `unit` jaise 'kg', 'g', 'l', 'ml', 'unit'.
        `current_stock` (>= 0) opening stock hai: backend isay 'adjustment' stock movement ke taur par
        record karta hai (item pehle 0 par banta hai). `par_level` reorder threshold. `supplier` isi branch ka
        supplier id (optional). Error: error.nameRequired, error.stockQuantityInvalid."""
        body = drop_none(
            {
                "branch": branch,
                "name": name,
                "unit": unit,
                "current_stock": _qty(current_stock),
                "par_level": _qty(par_level),
                "cost_per_unit": round(cost_per_unit, 2) if cost_per_unit is not None else None,
                "supplier": supplier,
            }
        )
        return await client.post("inventory-items/", body)

    @mcp.tool()
    async def inventory_item_update(
        item_id: int,
        name: str | None = None,
        unit: str | None = None,
        par_level: float | None = None,
        cost_per_unit: float | None = None,
        supplier: int | None = None,
    ) -> Any:
        """Inventory item PATCH (owner/manager); sirf diye gaye fields badalte hain.
        STOCK yahan se nahi badalta (backend current_stock ignore karta hai) -- iske liye
        inventory_item_adjust_stock use karein. Unit badalne se pehle dekh lein ke recipes ki units
        compatible rahengi."""
        body = drop_none(
            {
                "name": name,
                "unit": unit,
                "par_level": _qty(par_level),
                "cost_per_unit": round(cost_per_unit, 2) if cost_per_unit is not None else None,
                "supplier": supplier,
            }
        )
        return await client.patch(f"inventory-items/{item_id}/", body)

    @mcp.tool()
    async def inventory_item_delete(item_id: int) -> Any:
        """Inventory item delete (owner/manager, soft-delete). Agar kisi recipe mein use ho raha ho to
        409 error.inventoryItemInUse -- pehle recipes se hataein."""
        return await client.delete(f"inventory-items/{item_id}/")

    @mcp.tool()
    async def inventory_item_adjust_stock(
        item_id: int,
        quantity_delta: float,
        reason: ManualReason,
    ) -> Any:
        """Manual stock adjustment (owner/manager). `quantity_delta` SIGNED hai: negative = stock kam
        (waste/kharabi, jaise -2.5), positive = stock zyada (gintee mein kami theek karna). 0 allowed nahi.
        `reason`: 'waste' ya 'adjustment' ('sale'/'purchase' automatic hain, yahan nahi).
        Stock 0 se neeche jaye to 409 error.stockAdjustmentBelowZero. Har call ledger mein permanent row banati
        hai (undo nahi; galti ho to ulta delta dein). Updated item wapas aata hai."""
        return await client.post(
            f"inventory-items/{item_id}/adjust/",
            {"quantity_delta": _qty(quantity_delta), "reason": reason},
        )

    # ================================================== stock movements
    @mcp.tool()
    async def stock_movements_list(
        item: int | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Stock ledger (owner/manager, read-only, append-only). Har row: inventory_item, previous_stock,
        quantity_delta, resulting_stock, reason (sale/purchase/waste/adjustment), order, created_at.
        `item` dene par sirf us ingredient ki history. Paginated."""
        return await client.get("stock-movements/", {"item": item, "page": page, "page_size": page_size})

    # ======================================================== recipes
    @mcp.tool()
    async def recipes_list(product_ids: list[int] | None = None) -> Any:
        """Branch ki recipe lines ek call mein (owner/manager): har row {product, inventory_item, quantity, unit}.
        'Kaun se products ye ingredient use karte hain?' jaise sawal ke liye. `product_ids` se sirf chunay
        hue products. Deleted products shamil nahi."""
        ids = ",".join(str(i) for i in product_ids) if product_ids else None
        return await client.get("recipes/", {"products": ids})

    @mcp.tool()
    async def product_recipe_get(product_id: int) -> Any:
        """Ek product ki recipe (owner/manager): list of {inventory_item, quantity, unit}."""
        return await client.get(f"products/{product_id}/recipe/")

    @mcp.tool()
    async def product_recipe_set(product_id: int, lines: list[RecipeLine]) -> Any:
        """Product ki POORI recipe REPLACE karta hai (PUT; owner/manager): jo lines nahi dengi wo hat jayengi.
        Ek ingredient ek hi baar (error.duplicateRecipeIngredient); ingredient isi branch ka
        (error.inventoryItemInvalid); quantity > 0 (error.recipeQuantityInvalid); unit stock unit se
        compatible (error.recipeUnitInvalid). Success par product ki cost_price recipe se khud sync ho jati
        hai, aur ab order kitchen jane par stock yahi recipe (variant ke recipe_multiplier ke saath) katega.
        Khali list dene se recipe clear ho jati hai (lekin product_recipe_delete behtar signal hai)."""
        payload = [
            {"inventory_item": l["inventory_item"], "quantity": _qty(l["quantity"]), "unit": l["unit"]}
            for l in lines
        ]
        return await client.put(f"products/{product_id}/recipe/", payload)

    @mcp.tool()
    async def product_recipe_delete(product_id: int) -> Any:
        """Product ki recipe clear (owner/manager, 204). Note: ye cost_price ko sync NAHI karta (backend sirf
        recipe set par karta hai), aur recipe na hone par product orderable nahi rehta."""
        return await client.delete(f"products/{product_id}/recipe/")
