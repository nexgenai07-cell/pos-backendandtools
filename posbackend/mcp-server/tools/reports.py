"""
Reports MCP tools: read-only business reports (owner/manager only).

Every tool is a thin GET wrapper over `reports/<name>/`. All numbers and judgements (margin verdicts,
"needs attention" list, cost snapshots) are computed by the backend; the tools never calculate anything.

WHICH REPORT FOR WHICH QUESTION
  "How is business overall?"                -> report_business_summary   (one call: money, waste, top dishes, alerts)
  "How much did we earn / spend / profit?"  -> report_financial_summary
  "What happened day by day?"               -> report_daily_sales
  "Quick dashboard numbers"                 -> report_dashboard_summary
  "Sales split by day / payment method"     -> report_sales_overview
  "Every order, line by line (+cancelled)"  -> report_order_details       (large)
  "Browse recent orders (any status)"       -> report_recent_orders
  "What sells most?"                        -> report_product_performance
  "Which dish/size earns most? (real cost)" -> report_product_profitability
  "Rough margin per dish (today's cost)"    -> report_margins            (prefer profitability)
  "What did we buy / use per ingredient?"   -> report_ingredient_costs
  "What did we throw away?"                 -> report_wastage
  "Which hours are busy?"                   -> report_peak_hours
  "Which tables turn over fastest?"         -> report_table_turnover
  "Who are repeat customers?"               -> report_repeat_customers
  "Who is on shift / hours worked?"         -> report_staff_attendance
  "How fast is each kitchen station / chef?" -> report_kitchen_stations

DATE RANGE (identical on every report; days are in the BRANCH timezone; both ends inclusive)
  Precedence: `preset`  >  `date_from`  >  `days`  >  default (last 30 days).
  * preset: 'today' | 'yesterday' | 'this_week' (Monday..today) | 'this_month' (1st..today) |
            'last_<N>_days' e.g. 'last_7_days' (includes today) | 'custom' (= use date_from/date_to).
  * date_from / date_to: 'YYYY-MM-DD'. `date_to` defaults to today. With only `date_to`, the range is the
    30 days ending on it.
  * days: N -> the N days ENDING ON `date_to` (today if omitted), e.g. days=7 = today and the 6 days before.
  The response always echoes the resolved range as `from` / `to`.
  Errors: error.dateInvalid, error.daysInvalid, error.datePresetInvalid.

HOW MONEY IS COUNTED (so figures from different reports agree)
  * "Sales" = orders that are PAID and not cancelled, dated by the day the (latest) payment was taken, in
    branch time. Revenue = payments taken (after discounts, tips excluded).
  * Exceptions that use MENU PRICES instead (price snapshot x quantity, before discounts): product
    performance, margins, product profitability, per-product rows of daily sales, repeat-customer spend.
  * HISTORICAL cost = the unit-cost snapshot stored on each SALE stock movement. A `cost_complete` /
    `historical_cost_complete` flag of false means some sold items had no priced stock movement (no recipe,
    or sold before snapshots existed): the cost shown is then PARTIAL, so profit is overstated.
  * Amounts are decimal strings/numbers in the branch currency (SAR); percentages are 0-100.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from client import POSClient

# --------------------------------------------------------------------------- shared parameter types
Preset = Annotated[
    str | None,
    Field(
        description=(
            "Named date range, wins over date_from/days. One of: 'today', 'yesterday', 'this_week' "
            "(Monday to today), 'this_month', 'last_<N>_days' (e.g. 'last_7_days', includes today), "
            "'custom' (use date_from/date_to)."
        ),
        pattern=r"^(today|yesterday|this_week|this_month|custom|last_\d+_days)$",
    ),
]
Days = Annotated[
    int | None,
    Field(
        description=(
            "Last N days ending on date_to (today if date_to is omitted), e.g. 7 = today + previous 6 days. "
            "Ignored when preset or date_from is given."
        ),
        ge=1,
    ),
]
DateFrom = Annotated[
    str | None,
    Field(
        description="Range start, 'YYYY-MM-DD', branch timezone, inclusive. Wins over `days`.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]
DateTo = Annotated[
    str | None,
    Field(
        description="Range end, 'YYYY-MM-DD', branch timezone, inclusive. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]

OrderStatusFilter = Literal[
    "all", "open", "pending_cashier", "awaiting_waiter", "confirmed", "sent",
    "preparing", "ready", "served", "paid", "closed", "cancelled",
]
PaymentMethodFilter = Literal["all", "cash", "card", "online", "other"]


def _dates(preset: str | None, days: int | None, date_from: str | None, date_to: str | None) -> dict[str, Any]:
    """Map the tool's date arguments to the backend query params (preset / days / from / to)."""
    return {"preset": preset, "days": days, "from": date_from, "to": date_to}


def register(mcp, client: POSClient) -> None:
    # ======================================================================= owner overviews
    @mcp.tool()
    async def report_business_summary(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Owner's one-call business overview for a period: money, cost and profit, waste, cancellations, best
        dishes, busiest hours, low stock and a prioritised "needs attention" list. Start here for any broad
        "how is the business doing?" question; use the specific reports below to drill down.

        Response shape (JSON):
          from, to
          money            -> the full report_financial_summary object (revenue, tips, cash/card/other,
                              ingredient cost, gross profit/margin, purchases)
          waste            -> {total, unpriced_items, item_count}
          cancelled        -> {count, total, refunded}  (orders cancelled in the period)
          dish_totals      -> totals of report_product_profitability
          top_profit       -> up to 5 dish/size rows with the highest positive gross profit
          most_sold        -> up to 5 dish/size rows by units sold
          busiest_hours    -> top 3 [{hour 0-23, order_count}] (by the hour orders were OPENED)
          low_stock        -> [{id, name, unit, current_stock, par_level}] items at or below par level
                              (current state, not tied to the date range)
          attention        -> ordered list of {code, tone: danger|warn|info, link, ...figures}; codes:
                              no_recipe, loss, low (margin under 30%), cost_up (recipe now >10% dearer than
                              when sold), cancelled, low_stock, waste, waste_unpriced.
        Dish rows follow report_product_profitability. Takes ~5 queries; fine for a single call."""
        return await client.get("reports/business-summary/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_financial_summary(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Money in, ingredient cost and purchases for a period: revenue (with cash/card/other split and tips),
        gross profit and margin from HISTORICAL ingredient cost, and what was spent on received purchases.

        Response (flat object):
          from, to, currency
          revenue, paid_order_count, items_sold, average_order_value
          cash_revenue, card_revenue, other_revenue   (by payment method; tips are NOT included in revenue)
          tips
          ingredient_cost_consumed   -> total cost of ingredients used by the sold items, or null when the cost
                                        is not fully known (see historical_cost_complete)
          known_ingredient_cost      -> the cost that IS known (always a number)
          historical_cost_complete   -> false if any sold item has no priced stock movement
          unpriced_movement_count    -> sale stock movements with no cost snapshot
          gross_profit, gross_margin_pct   -> null unless the cost is complete
          known_gross_profit         -> revenue - known_ingredient_cost (an upper bound when the cost is incomplete)
          known_cost_share_pct       -> known cost as % of revenue
          total_purchases, received_purchase_count   -> purchases RECEIVED in the period (by received date)
        Do not report gross_profit as exact when historical_cost_complete is false."""
        return await client.get("reports/financial-summary/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_dashboard_summary(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """The five dashboard cards: sales total, order count, average order value, and two LIVE counters.

        Response: from, to, total_sales (payments taken, after discounts, no tips - same figure as the sales,
        daily and financial reports), order_count (paid orders), average_order_value,
        open_orders_count (orders not yet paid/closed/cancelled right now - not limited to the date range),
        low_stock_count (inventory items at or below par level right now - not limited to the date range).
        Use report_business_summary for anything richer."""
        return await client.get("reports/dashboard-summary/", _dates(preset, days, date_from, date_to))

    # ======================================================================= sales
    @mcp.tool()
    async def report_daily_sales(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Day-by-day report for a period: for each day the revenue, orders, what was sold (dish + size), what
        it cost (historical) and which ingredients left stock. Use it for trends and "how was <day>?".
        Can be large for long ranges; prefer <= 31 days.

        Response shape (JSON):
          from, to, currency
          totals -> {revenue, orders, items_sold, ingredient_cost, gross_profit, gross_margin_pct, cost_complete}
          days   -> NEWEST FIRST, each {
                      day 'YYYY-MM-DD', revenue (payments taken), orders, items_sold,
                      ingredient_cost, gross_profit, gross_margin_pct, verdict, cost_complete,
                      products    -> [{product_id, name_en, name_ar, variant_name_en, variant_name_ar,
                                       quantity, revenue (menu price x qty), cost, cost_complete, profit,
                                       gross_margin_pct (null if cost incomplete), verdict}] by revenue,
                      ingredients -> [{inventory_item_id, name, unit, quantity, cost, cost_complete}] by cost }
        verdict is one of loss | low (<30%) | ok (30-55%) | good (>=55%) | no_recipe (cost unknown).
        Only days that had sales appear. cost_complete=false means that cost is partial."""
        return await client.get("reports/daily-sales/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_sales_overview(
        method: Annotated[
            Literal["cash", "card", "online", "other"] | None,
            Field(description="Restrict ONLY the by_payment_method breakdown to this method; totals stay unfiltered."),
        ] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """Sales per day and per payment method for a period. Lighter than report_daily_sales (no product or
        cost detail).

        Response shape (JSON):
          from, to
          total_sales, total_paid_amount   (identical: payments taken, after discounts, tips excluded)
          paid_order_count
          by_day            -> [{day, total, order_count, average_order_value}] ascending; the day money was taken
          by_payment_method -> [{method, total, order_count}]; `total` sums every payment made with the method,
                               while `order_count` counts orders whose LAST payment used that method (so a split
                               payment's order is counted once). Filtered by `method` if given."""
        return await client.get(
            "reports/sales-overview/", {"method": method, **_dates(preset, days, date_from, date_to)}
        )

    @mcp.tool()
    async def report_order_details(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Every paid order in a period with full line-level money detail, plus cancelled orders and per-customer
        and per-table groupings. VERY LARGE: use a short range (a day or a week). Prefer report_recent_orders or
        report_daily_sales unless you need per-order cost, discount, tips or seating time.

        Response shape (JSON):
          from, to
          truncated -> true when more than 3000 paid orders matched and only the newest 3000 are returned
          totals    -> {orders, items_total, paid, discount, tips, voided_total, cost, cost_complete, profit,
                       cancelled_count, cancelled_total, refunded, cancelled_waste}
          orders    -> newest paid first, each {id, order_number, source, table {id,label_en,label_ar}|null,
                       customer {id,name,phone}|null, waiter, cashier, taken_by, opened_at, paid_at, closed_at,
                       seated_minutes, plates, items_total (menu price of live lines), voided_total, paid,
                       discount (= items_total - paid when positive), tips, methods [{method, amount}],
                       cost, cost_complete, profit,
                       items [{name_en,name_ar,variant_en,variant_ar,quantity,unit_price,total,voided,notes}]}
          cancelled -> orders cancelled in the period (max 3000, not flagged when truncated): {id, order_number,
                       table, customer, opened_at, cancelled_at, cancelled_by, from_status, reason, total,
                       refunded, waste_cost, items}
          customers -> per customer: {id, name, phone, orders, spend (payments), visits, total_minutes,
                       average_minutes, spend_per_visit, spend_per_hour, last_visit}, by spend
          tables    -> same shape per table
          customer_totals, table_totals -> {orders, visits, total_minutes, spend}
        seated_minutes is the table sitting (bill open-to-release, shared by that bill's orders) or the order's own
        open-to-close time; null for takeaway."""
        return await client.get("reports/order-details/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_recent_orders(
        limit: Annotated[
            int | None, Field(description="Max rows when NOT paginating (default 25, max 100). Ignored if page/page_size is set.", ge=1, le=100)
        ] = None,
        status: Annotated[OrderStatusFilter | None, Field(description="Order status, or 'all' (default).")] = None,
        table: Annotated[int | Literal["all"] | None, Field(description="Table id, or 'all' (default).")] = None,
        method: Annotated[PaymentMethodFilter | None, Field(description="Orders that have a payment with this method, or 'all'.")] = None,
        search: Annotated[str | None, Field(description="Order number/code or table label (en/ar), partial match.")] = None,
        ordering: Annotated[
            str | None,
            Field(description="Paginated mode only. Keys: order, date, table, status, source, total, items, payment; prefix '-' for descending (default '-date')."),
        ] = None,
        page: Annotated[int | None, Field(description="Page number. Setting page or page_size switches to PAGINATED mode.", ge=1)] = None,
        page_size: Annotated[int | None, Field(description="Rows per page in paginated mode (default 25, max 200).", ge=1, le=200)] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """Orders OPENED in the period, newest first, in ANY status (including unpaid, open and cancelled) - the
        live order list with filters. For revenue use the sales reports; for the full staff order list with more
        filters use `orders_list`.

        Response shape (JSON) depends on paging:
          * default (no page/page_size): a plain JSON ARRAY of up to `limit` orders (full order objects with
            items, payments and history).
          * paginated (page or page_size given): {count, next, previous, results: [orders], metadata:
            {total_spend}}; total_spend = sum of non-voided line prices (menu price x qty) over ALL matches.
        All filters apply before `limit`/paging. Error: error.limitInvalid if limit is not an integer."""
        return await client.get(
            "reports/recent-orders/",
            {
                "limit": limit,
                "status": status,
                "table": table,
                "method": method,
                "search": search,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
                **_dates(preset, days, date_from, date_to),
            },
        )

    # ======================================================================= products
    @mcp.tool()
    async def report_product_performance(
        category: Annotated[int | None, Field(description="Category id; limits the report to that category.")] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """What sold: one row per dish AND size with quantity, revenue and share of dish sales, best revenue first.
        Voided items are excluded. Use for "best sellers / worst sellers". It has NO cost or profit - use
        report_product_profitability for that.

        Revenue here = menu price x quantity (BEFORE discounts), so the total can exceed the payments taken.
        Response shape (JSON):
          from, to
          results -> [{product_id, name_en_snapshot, name_ar_snapshot, variant_name_en_snapshot,
                       variant_name_ar_snapshot, quantity_sold, revenue, average_price, share_pct}]
          totals  -> {quantity_sold, revenue}"""
        return await client.get(
            "reports/product-performance/", {"category": category, **_dates(preset, days, date_from, date_to)}
        )

    @mcp.tool()
    async def report_product_profitability(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """The authoritative profit report: one row per dish AND size ("Karahi - Half KG" and "- 1 KG" are separate)
        with revenue, HISTORICAL ingredient cost, gross profit/margin, a verdict, and whether the recipe has got
        dearer since. Use this (not report_margins) for pricing and menu decisions.

        Response shape (JSON):
          from, to
          results -> by revenue, each {product_id, variant_id, product_name, product_name_ar, variant_name,
                     variant_name_ar, recipe_multiplier, units_sold, average_selling_price, revenue (menu price x
                     qty), ingredient_cost, gross_profit, gross_profit_per_unit, gross_margin_pct, cost_per_plate
                     (what the SOLD plates cost), current_recipe_cost_per_unit (today's recipe at the latest
                     purchase prices x size multiplier), cost_rose (true if today's cost > sold cost by >10%),
                     verdict, needs_attention, historical_cost_complete}
                     Cost fields are null when the cost is unknown (no recipe or no priced stock movement).
          totals  -> {units_sold, revenue, ingredient_cost, gross_profit, priced_revenue, gross_margin_pct (over
                     priced dishes only), unpriced_revenue, no_recipe_count, attention_count}
          bands   -> {low_margin_pct: 30, good_margin_pct: 55, cost_rise_pct: 10}
        verdict: loss (<0%) | low (<30%) | ok (<55%) | good (>=55%) | no_recipe (cost unknown);
        needs_attention is true for loss, low and no_recipe."""
        return await client.get("reports/product-profitability/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_margins(
        category: Annotated[int | None, Field(description="Category id; limits the report to that category.")] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """LEGACY quick margin per dish using TODAY's `cost_price` - an approximation. Prefer
        report_product_profitability, which uses the real historical cost per sale and handles sizes correctly.

        Caveats: cost = quantity x the product's CURRENT cost_price, so past sales are valued at today's cost, and
        the size's recipe multiplier is NOT applied (a bigger size is costed like the base size, overstating its
        margin). Revenue = menu price x quantity (before discounts).
        Response: from, to, results -> [{product_id, name_en_snapshot, variant_name_en_snapshot, quantity_sold,
        revenue, cost, margin}] by revenue (margin is an amount, not a percentage)."""
        return await client.get(
            "reports/margins/", {"category": category, **_dates(preset, days, date_from, date_to)}
        )

    @mcp.tool()
    async def report_ingredient_costs(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Per-ingredient accounting for a period: what was bought, what was used by sales, and what the stock on
        the shelf is worth. Use for purchasing decisions and "which ingredient costs us most?".
        Ingredients with no purchases, no usage and zero stock are omitted.

        Response shape (JSON):
          from, to
          results -> by name, each {inventory_item_id, name, unit, current_stock,
                     purchase_quantity, purchase_spending (RECEIVED purchases in the period),
                     average_purchase_cost (quantity-weighted), latest_purchase_cost (within the period),
                     used_quantity, consumed_cost (historical cost of stock used by SALES; null if any of it
                     had no cost snapshot),
                     current_stock_value_at_latest_cost (stock x latest received purchase price ever; null if
                     never purchased)}
          totals  -> {purchase_spending, consumed_cost, consumed_cost_complete, current_stock_value,
                     unvalued_items (in stock but never purchased)}
        used_quantity counts sale usage only (not waste)."""
        return await client.get("reports/ingredient-costs/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_wastage(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Stock thrown away in a period, per ingredient, valued at the purchase cost of that stock when it was
        wasted. Covers every stock movement recorded with reason 'waste' (manual waste adjustments and
        waste created by voided/cancelled dispatched items).

        Response shape (JSON):
          from, to
          results -> by cost then quantity (largest first), each {inventory_item_id, inventory_item_name, unit,
                     quantity_wasted, estimated_cost, unpriced_quantity, cost_complete}
          totals  -> {cost, unpriced_items}
        Movements from before cost snapshots existed are counted in quantity_wasted/unpriced_quantity but add 0 to
        the cost, so cost_complete=false means the real cost is higher."""
        return await client.get("reports/wastage/", _dates(preset, days, date_from, date_to))

    # ======================================================================= operations
    @mcp.tool()
    async def report_peak_hours(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """When the restaurant is busy: number of PAID orders per hour of day, summed over the whole period. It is
        order volume, not revenue, and the hour is when the order was OPENED (restaurant's local clock).

        Response: from, to, results -> [{hour (0-23), order_count}] ascending by hour; hours with no orders are
        omitted. For the top 3 hours in one call see `busiest_hours` in report_business_summary."""
        return await client.get("reports/peak-hours/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_table_turnover(
        table: Annotated[int | None, Field(description="Table id; limits the report to that table.")] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """Table usage in a period: paid orders per table and the average time from order open to close, most-used
        tables first. Takeaway/web orders without a table are excluded.

        Response: from, to, results -> [{table_id, table_label_en, order_count, average_duration_seconds}]
        (average_duration_seconds is null when orders are not closed yet; it is per ORDER, not per sitting - for
        sittings use `tables` in report_order_details)."""
        return await client.get(
            "reports/table-turnover/", {"table": table, **_dates(preset, days, date_from, date_to)}
        )

    @mcp.tool()
    async def report_repeat_customers(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Customers who placed MORE THAN ONE order in the period, most orders first, plus the share of orders that
        came from them. Only orders linked to a customer count (QR/website orders, or POS orders where a customer
        was attached); cancelled orders are excluded, unpaid ones are included, dated by when they were opened.

        Response shape (JSON):
          from, to
          results -> [{customer_id, customer__phone, customer__name, order_count, total_spend (menu price of
                     non-voided lines, before discounts), last_order_at}]
          repeat_order_share -> % of all customer-linked orders in the period that were repeat customers' orders
        An empty `results` means no customer ordered twice in the period (or orders are not linked to customers)."""
        return await client.get("reports/repeat-customers/", _dates(preset, days, date_from, date_to))

    @mcp.tool()
    async def report_staff_attendance(
        role: Annotated[
            Literal["owner", "manager", "cashier", "waiter", "kitchen", "all"] | None,
            Field(description="Only staff with this role; 'all' (default) = everyone."),
        ] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """Who is on shift right now and how many hours each staff member worked in the period (one row per staff
        member, by name, including those with 0 hours). Hours count shifts that STARTED inside the period; a shift
        still open counts up to now.

        Response: from, to, results -> [{staff_id, name, role, on_shift (their latest shift has no clock-out),
        last_clock_in, last_clock_out, hours_in_window}]. last_clock_in/out are the most recent shift overall (not
        limited to the period). To clock someone in/out use staff_clock_in / staff_clock_out."""
        return await client.get(
            "reports/staff-attendance/", {"role": role, **_dates(preset, days, date_from, date_to)}
        )

    @mcp.tool()
    async def report_kitchen_stations(
        station: Annotated[
            str | None,
            Field(description="Kitchen station id, or 'general' (lines with no station). Omit for all stations."),
        ] = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """How long each kitchen station (and chef) takes: waiting time, cooking time and total time per line, from
        the timestamps every kitchen line carries - fired (reached the kitchen), started (a chef pressed Start) and
        ready. Use it to find slow stations, slow dishes and slow hours. Lines belong to the branch-local day and hour
        they were FIRED, and to the station they were fired to (a snapshot, so moving a dish later never rewrites
        history; station null = General).

        Definitions: wait = fired -> started, cook = started -> ready, total = fired -> ready. Voided lines are
        counted (`voided`) but never timed. A line started before Start was recorded has a total time but no wait or
        cook time, so every average carries the number of lines it is over (`wait_lines`, `cook_lines`,
        `total_lines`); an average is null when nothing was timed.

        Response shape (JSON):
          from, to
          totals   -> timing object for everything in range
          stations -> in screen order, General last, each {station_id (null = General), name_en, name_ar,
                      ...timing, slowest_dishes: up to 3 [{product_id, name_en, name_ar, variant_name_en,
                      variant_name_ar, ...timing}] by average total time, slowest first}
          days     -> newest day first, per (day, station): {day, station_id, name_en, name_ar, ...timing}
          hours    -> per (hour 0-23, station): {hour, station_id, name_en, name_ar, ...timing}
          chefs    -> who pressed Start, most lines first: {staff_id, name, ...timing}
        Timing object: {lines, portions (sum of quantities), voided, avg_wait_min, wait_lines, avg_cook_min,
        cook_lines, avg_total_min, total_lines}; averages are in MINUTES to one decimal."""
        return await client.get(
            "reports/kitchen-stations/", {"station": station, **_dates(preset, days, date_from, date_to)}
        )
