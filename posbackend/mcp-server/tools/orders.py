"""
orders app ke MCP tools: orders, items, kitchen (KDS), payments, refunds, bills, customer QR flow.

Order lifecycle (orders/workflow.py ki state machine):
  open -> pending_cashier -> awaiting_waiter -> confirmed -> sent -> preparing -> ready -> served
       -> paid -> closed            (kisi bhi waqt: cancelled)
Item status: pending -> fired -> preparing -> ready -> served  (ya voided)
Bill status: open -> pay_requested -> paid -> closed

Kitchen stations (backend ka `kitchen` app): menu ki dish/category ek station (BBQ, Grill, ...) par set hoti hai.
Jab line kitchen ko FIRE hoti hai to uska station us waqt FIX ho jata hai (baad mein dish ka station badalne se
pehle se pakti lines nahi hilti); station na ho to 'General'. Sirf us station ke chefs ko notification jati hai
(General ya jis station par koi chef nahi, wahan har kitchen chef ko). Line par `station`, `route_station`
(fire se pehle kahan jayegi), `station_moved` (haath se move hui), `started_by`/`started_by_name` (kis chef ne
Start kiya) aate hain.

Sections:
  1. Staff: orders list/summary/create/detail/history
  2. Staff: order items
  3. Staff: workflow (cashier review, waiter, send to kitchen, close, cancel, customer)
  4. Staff: payments & refunds
  5. Staff: bills (settlement queue)
  6. Kitchen (KDS): tickets, expo, print, item status, station move, handover
  7. Public/customer flow (QR + website)  -- koi login nahi, table ke QR token se authenticate

Jaan-boojh kar EXCLUDE kiya gaya:
  POST payments/moyasar/webhook/ -- ye Moyasar ka server-to-server callback hai jo shared
  secret_token se protected hai; isay LLM tool bana dena payment-forgery ka rasta khol sakta hai.

Notes:
  * Prices backend order-time par snapshot karta hai; client kabhi price nahi bhejta.
  * Idempotency: payment aur customer-order calls `request_id` maangti hain. Agar aap nahi dete to
    tool naya uuid banata hai (aur response mein request_id wapas milti hai). Retry karte waqt
    WAHI request_id dubara bhejein, warna duplicate ban sakta hai.
  * Pagination: default 25/page, `page_size` max 200; response {count,next,previous,results,metadata}.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

from client import POSClient, drop_none
from tools._utils import location_payload, path_token

OrderStatus = Literal[
    "open", "pending_cashier", "awaiting_waiter", "confirmed", "sent",
    "preparing", "ready", "served", "paid", "closed", "cancelled",
]
PaymentMethod = Literal["cash", "card", "other"]
BillStatus = Literal["open", "pay_requested", "paid", "closed"]
KitchenItemStatus = Literal["preparing", "ready", "served", "voided"]


class OrderLineInput(TypedDict):
    """Order mein ek item line. Price server khud lagata hai."""

    product: int
    variant: NotRequired[int | None]  # product ke variant ki id (agar size hai)
    quantity: NotRequired[int]  # default 1, >= 1
    notes: NotRequired[str]


class RefundLineInput(TypedDict):
    item: int  # order item id
    quantity: int  # kitni quantity refund karni hai


def _money(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _new_request_id(request_id: str | None) -> str:
    """Idempotency key (max 128 chars). Na di gayi ho to naya uuid."""
    return request_id or uuid.uuid4().hex


def register(mcp, client: POSClient) -> None:
    # ============================================================
    # 1. STAFF: ORDERS - list / summary / create / detail / history
    # ============================================================
    @mcp.tool()
    async def orders_list(
        status: OrderStatus | None = None,
        table: int | None = None,
        customer: int | None = None,
        waiter: str | None = None,
        cashier: int | None = None,
        search: str | None = None,
        latest_per_table: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Orders ki list (order-area staff). Waiter ko sirf apne assigned orders milte hain.
        Filters: `status`, `table` id, `customer` id, `waiter` (staff id ya 'unassigned'), `cashier` id,
        `search` (order code/number se), `latest_per_table` (har table ka sirf latest order),
        `date_from`/`date_to` (YYYY-MM-DD, branch timezone, inclusive; galat date par kuch nahi aata).
        `ordering` keys: order, date, status, total, source ('-' = descending; default newest pehle).
        metadata mein paid_count aur total_spend (poori selection ke)."""
        return await client.get(
            "orders/",
            {
                "status": status,
                "table": table,
                "customer": customer,
                "waiter": waiter,
                "cashier": cashier,
                "search": search,
                "latest_per_table": latest_per_table,
                "from": date_from,
                "to": date_to,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def orders_summary(
        status: OrderStatus | None = None,
        table: int | None = None,
        customer: int | None = None,
        waiter: str | None = None,
        cashier: int | None = None,
        search: str | None = None,
        latest_per_table: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> Any:
        """orders_list wale hi filters par poori selection ke counts aur sales:
        total, pending, preparing, ready, served, paid, cancelled, sales (payments ka jama)."""
        return await client.get(
            "orders/summary/",
            {
                "status": status,
                "table": table,
                "customer": customer,
                "waiter": waiter,
                "cashier": cashier,
                "search": search,
                "latest_per_table": latest_per_table,
                "from": date_from,
                "to": date_to,
            },
        )

    @mcp.tool()
    async def order_open_or_get(table: int | None = None) -> Any:
        """Order get-or-create (POS staff; waiter sirf us table par jahan wo already assigned ho).
        `table` do to us table ka open order wapas milta hai ya naya banta hai (201 = naya, 200 = pehle se
        tha); table na do to bina table ka takeaway order banta hai.
        Table par ek waqt mein EK party (phone) hoti hai jab tak staff table release na kare. Agar table par
        koi live order nahi lekin ek party ki paid (abhi release na hui) bill baithi hai to staff ka add usi
        party ka hota hai: wohi bill/order dobara khulta hai, table par doosri bill nahi banti.
        Agar table ka bill pay_requested/locked ho to 409 error.billLocked."""
        return await client.post("orders/", drop_none({"table": table}))

    @mcp.tool()
    async def order_get(order_id: int) -> Any:
        """Ek order ki poori detail: items, payments, history, total (order-area staff;
        waiter sirf apna assigned order). Har item mein station (fire hone par fix), route_station
        {id, name_en, name_ar}|null (jahan jayegi/gayi), station_moved, started_by/started_by_name/started_at.
        History rows mein `actor_name` bhi aata hai (None = customer ya system)."""
        return await client.get(f"orders/{order_id}/")

    @mcp.tool()
    async def order_history(order_id: int) -> Any:
        """Order ka audit trail: kis ne, kab, kya badla (event, from_status, to_status, details)."""
        return await client.get(f"orders/{order_id}/history/")

    @mcp.tool()
    async def table_open_order_get(table_id: int) -> Any:
        """Table ka abhi chalta (non-terminal) order (POS staff). Na ho to 404 error.notFound."""
        return await client.get(f"tables/{table_id}/open-order/")

    # ============================================================
    # 2. STAFF: ORDER ITEMS
    # ============================================================
    @mcp.tool()
    async def order_item_add(
        order_id: int,
        product: int,
        quantity: int = 1,
        variant: int | None = None,
        notes: str | None = None,
    ) -> Any:
        """Order mein item add (order-area staff). Price/naam us waqt ke snapshot hote hain.
        Behaviour order ki state par depend karta hai. Kitchen se PEHLE (open/pending/awaiting/confirmed) wohi dish
        (same product, size, note aur price) dubara add karne se pehle se waiting (pending) line ki quantity BADHTI
        hai, nayi row nahi banti (stock ka check nayi total quantity par). Order kitchen mein ho
        (sent/preparing/ready/served) to har addition apni alag kitchen line banti hai, foran fire hoti hai, stock
        katta hai aur apne station ke chefs ko jati hai (line ka station fire ke waqt fix).
        Errors: error.orderClosed, error.billLocked, error.productUnavailable (stock/din/available),
        error.variantInvalid / error.variantUnavailable, error.orderNotAssignedToWaiter."""
        return await client.post(
            f"orders/{order_id}/items/",
            drop_none({"product": product, "quantity": quantity, "variant": variant, "notes": notes}),
        )

    @mcp.tool()
    async def order_item_update(
        order_id: int,
        item_id: int,
        quantity: int | None = None,
        notes: str | None = None,
    ) -> Any:
        """Item ki quantity/notes badalna (PATCH). Sirf PENDING item (jo abhi kitchen ko nahi gaya),
        warna 409 error.itemAlreadyInKitchen. Waiter sirf apna assigned order jab wo awaiting_waiter ho.
        quantity <= 0 dene se item hat jata hai (order_item_remove dekhein)."""
        return await client.patch(
            f"orders/{order_id}/items/{item_id}/",
            drop_none({"quantity": quantity, "notes": notes}),
        )

    @mcp.tool()
    async def order_item_remove(order_id: int, item_id: int) -> Any:
        """Pending item hata do. Backend mein alag DELETE route nahi; ye PATCH quantity=0 bhejta hai
        (soft-delete). Sirf PENDING item; kitchen ko gaye item ke liye order_item_set_status(voided) use karein.
        Agar ye staff ke apne ticket (bina customer, open/pending_cashier) ki AAKHRI line thi to poora order
        delete ho jata hai aur table (agar kuch aur nahi) khali ho jati hai: tab response khali success
        (HTTP 204) aata hai, order wapas nahi aata. Warna order khali ho jaye to cashier ko 'order:empty'
        notification jati hai."""
        return await client.patch(f"orders/{order_id}/items/{item_id}/", {"quantity": 0})

    # ============================================================
    # 3. STAFF: WORKFLOW
    # ============================================================
    @mcp.tool()
    async def order_cashier_review(order_id: int, waiter: int | None = None) -> Any:
        """Cashier review (cashier staff). pending_cashier order ko awaiting_waiter par le jata hai; `waiter`
        (active waiter ka staff id, isi branch ka) optionally assign hota hai. Cashier ko review ke dauran har
        line par `route_station` dikhta hai (kahan jayegi); galat ho to order_item_move_station se badal sakta hai.
        Agar order pehle se kitchen mein hai aur naye customer items pending hain to wo review ho kar
        seedha kitchen ko fire hote hain (apne station ko).
        Errors: error.orderNotAwaitingCashier, error.orderHasNoItems, error.productUnavailable."""
        return await client.post(f"orders/{order_id}/cashier-review/", drop_none({"waiter": waiter}))

    @mcp.tool()
    async def order_assign_waiter(order_id: int, waiter: int) -> Any:
        """Order par waiter assign/badalna (cashier staff). Waiter active aur isi branch ka hona chahiye.
        Errors: error.orderAlreadyClosed, error.orderAlreadyAssigned."""
        return await client.post(f"orders/{order_id}/assign-waiter/", {"waiter": waiter})

    @mcp.tool()
    async def order_waiter_confirm(order_id: int) -> Any:
        """Waiter confirmation (waiter; owner/manager bhi kar sakte hain): awaiting_waiter -> confirmed,
        phir pending items kitchen ko fire hote hain (stock katta hai, har line ka station fix hota hai) aur
        order 'sent' ho jata hai; sirf un lines ke station ke chefs ko order:kitchen_new jata hai.
        Errors: error.orderNotAwaitingWaiter, error.orderWaiterNotAssigned,
        error.orderNotAssignedToWaiter, error.productUnavailable."""
        return await client.post(f"orders/{order_id}/waiter-confirm/")

    @mcp.tool()
    async def order_send_to_kitchen(order_id: int) -> Any:
        """Pending items ko kitchen ko bhejna aur recipe ka stock kaatna; har line apne station par jati hai
        (station fire ke waqt fix) aur sirf wahan ke chefs ko notification milti hai.
        Pehli dafa: sirf confirmed order par (owner/manager/waiter), warna error.waiterConfirmationRequired.
        Baad mein (order sent/preparing/ready/served): additional items ka batch bhejne ke liye
        waiter/cashier/owner/manager. Error: error.noPendingItems, error.productUnavailable."""
        return await client.post(f"orders/{order_id}/send-to-kitchen/")

    @mcp.tool()
    async def order_set_customer(order_id: int, customer: int | None = None) -> Any:
        """Order par customer link karna (POS staff). `customer=None` se customer hat jata hai.
        Customer isi branch ka hona chahiye. Error: error.orderClosed, error.tableOccupied
        (table ka bill kisi aur customer ka ho)."""
        # Yahan None explicitly bhejna zaroori hai (clear karne ke liye), is liye drop_none nahi.
        return await client.post(f"orders/{order_id}/customer/", {"customer": customer})

    @mcp.tool()
    async def order_close(order_id: int) -> Any:
        """Paid order ko close karna (cashier staff). Sirf paid order, warna 409 error.orderMustBePaid."""
        return await client.post(f"orders/{order_id}/close/")

    @mcp.tool()
    async def order_cancel_preview(order_id: int) -> Any:
        """Cancel karne se pehle preview (cashier staff): allowed, total, refund (jo wapas dena hoga),
        cooked_plates, reason_required. Wahi rules jo order_cancel lagata hai."""
        return await client.get(f"orders/{order_id}/cancel/")

    @mcp.tool()
    async def order_cancel(order_id: int, reason: str | None = None) -> Any:
        """Order cancel. Cashier sirf kitchen se pehle cancel kar sakta hai; owner/manager kisi bhi stage par
        (cooking/served/paid bhi). Admin ko `reason` dena zaroori hai agar khana ban chuka ho ya paisa
        liya ja chuka ho (error.cancelReasonRequired). Asar: kitchen ko gaye lines ka stock waste ban jata hai,
        liye hue paise 'refunded_amount' (wapas dene hain) record hote hain, ZATCA ko credit note jata hai.
        DESTRUCTIVE: pehle order_cancel_preview chalayein."""
        return await client.post(f"orders/{order_id}/cancel/", drop_none({"reason": reason}))

    # ============================================================
    # 4. STAFF: PAYMENTS & REFUNDS
    # ============================================================
    @mcp.tool()
    async def order_payment_record(
        order_id: int,
        amount: float,
        method: PaymentMethod,
        tip: float | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Order par payment record (cashier staff). Sirf SERVED order par (error.orderNotServed).
        `amount` baqi balance se zyada nahi ho sakta (error.paymentExceedsBalance); poora ada hone par order
        'paid' ho jata hai aur ZATCA invoice trigger hota hai, warna 'partial'.
        IDEMPOTENT: `request_id` (<=128 chars) naya payment ban'ne se rokta hai; retry par wahi id dein.
        Na dein to naya uuid banta hai. Same id + alag amount/method => 409 error.idempotencyKeyConflict."""
        key = _new_request_id(request_id)
        data = await client.post(
            f"orders/{order_id}/payments/",
            drop_none({"amount": _money(amount), "method": method, "tip": _money(tip), "request_id": key}),
        )
        # Response ke saath key bhi wapas do taake retry mumkin ho.
        return {"request_id": key, "payment": data}

    @mcp.tool()
    async def order_refund_preview(order_id: int) -> Any:
        """Partial refund ke liye kya baqi hai (owner/manager only): allowed, paid, refunded,
        har line ka refundable quantity/unit_price, aur pichle refunds ki list."""
        return await client.get(f"orders/{order_id}/refund/")

    @mcp.tool()
    async def order_refund(
        order_id: int,
        lines: list[RefundLineInput],
        method: PaymentMethod,
        reason: str,
    ) -> Any:
        """Paid order ke kuch items (ya quantity) ka partial refund (owner/manager only), unit price par.
        `reason` zaroori (<=255 chars). Order cancel nahi hota. ZATCA credit note automatically banta hai.
        Errors: error.refundNotPaid, error.refundQuantityTooHigh, error.refundMoreThanPaid,
        error.refundLinesRequired. MONEY-AFFECTING: pehle order_refund_preview dekhein."""
        return await client.post(
            f"orders/{order_id}/refund/",
            {"lines": [dict(line) for line in lines], "method": method, "reason": reason},
        )

    # ============================================================
    # 5. STAFF: BILLS (cashier settlement queue)
    # ============================================================
    @mcp.tool()
    async def bills_list(
        status: list[BillStatus] | None = None,
        table: int | None = None,
    ) -> Any:
        """Cashier ki bills queue (cashier staff). `status` list (jaise ['pay_requested']);
        na do to closed ke ilawa sab bills. Sabse nayi pay_request pehle.
        `table` (table id) dene par sirf us table ki bills (table detail panel); ghalat/non-numeric
        value par khali list. Har bill mein table_label aur table_label_ar."""
        return await client.get("bills/", {"status": status, "table": table})

    @mcp.tool()
    async def bill_get(bill_id: int) -> Any:
        """Bill ki detail: table, table_label aur table_label_ar (chhape hue slip dono language mein),
        orders, total, amount_paid/outstanding, invoices, aur moyasar_payments."""
        return await client.get(f"bills/{bill_id}/")

    @mcp.tool()
    async def bill_mark_paid(bill_id: int, method: Literal["cash", "card"] = "cash") -> Any:
        """Till par paisa le liya (cashier staff): bill paid. `online` yahan nahi, wo sirf verified
        Moyasar payment se hoti hai. Bill ke tamam orders served hone chahiye (error.billNotServed).
        Pehle se paid ho to result 'already_processed'. Error: error.billClosed."""
        return await client.post(f"bills/{bill_id}/mark-paid/", {"method": method})

    @mcp.tool()
    async def bill_reopen(bill_id: int) -> Any:
        """Atki hui bill ko unlock (cashier staff): pay_requested -> open taake table dobara order kar sake.
        Paid ke baad refuse (wahan refund use karein)."""
        return await client.post(f"bills/{bill_id}/reopen/")

    @mcp.tool()
    async def bill_release_table(bill_id: int) -> Any:
        """Paid bill ko close karke table release (cashier staff): bill closed, tamam orders closed,
        table 'empty'. Bill paid honi chahiye."""
        return await client.post(f"bills/{bill_id}/release-table/")

    # ============================================================
    # 6. KITCHEN (KDS)
    # ============================================================
    @mcp.tool()
    async def kds_tickets(
        scope: Literal["live", "history", "expo"] | None = None,
        stations: str | None = None,
        table: int | None = None,
        order_type: Literal["dine_in", "takeaway", "qr", "web"] | None = None,
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Kitchen tickets (kitchen staff). `scope`:
        'live' (default): wo orders jin mein koi line fired/preparing/ready hai;
        'history': jis order mein kabhi kuch kitchen ko fire hua, naye pehle;
        'expo': head chef ka PASS - har station ki wo lines jo abhi kitchen mein hain (pak rahi ya ready lekin
        waiter ko handover nahi hui); sirf head chef/owner/manager (baqi ko 403). Order mein `all_ready` true
        hone par order_handover ho sakta hai. `stations` expo par lagu nahi.
        `stations` (live/history): 'mine' | 'all' | station ids comma se ('3,5') aur/ya 'general'
        ('3,general'). Default: kitchen chef ke liye 'mine' (apne stations + General), head chef/owner/manager
        ke liye 'all'. Orders wo aate hain jin ki kam az kam ek line chune hue station par ho.
        Har line mein station, station_name_en/ar, is_mine (chune hue stations mein hai?; baqi greyed dikhane
        ke liye), can_cook (viewer ka station hai?), station_moved, started_by, started_by_name, started_at.
        Baqi filters: `table`, `order_type`, `search` (order number), date_from/date_to (YYYY-MM-DD)."""
        return await client.get(
            "kds/tickets/",
            {
                "scope": scope,
                "stations": stations,
                "table": table,
                "type": order_type,
                "search": search,
                "from": date_from,
                "to": date_to,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def order_item_set_status(order_id: int, item_id: int, status: KitchenItemStatus) -> Any:
        """Kitchen item ko aage badhana (PATCH): fired->preparing->ready->served, ya kisi bhi waqt voided.
        Kitchen chef 'served' set nahi kar sakta (sirf kitchen lead: head chef/owner/manager kar sakte hain);
        waiter sirf apne assigned order par 'served' set kar sakta hai.
        'voided' dispatched item ka stock waste mein badal deta hai. Order ka status items se khud recompute hota hai.
        PREPARING par line us chef ke naam (started_by, started_at) ho jati hai. Jis line ko doosre chef ne Start
        kar diya wo usi ki hai: baqi chef ko 409 error.itemAlreadyStarted (response mein started_by aur
        started_by_name), taake do log ek hi dish na banayen; kitchen lead phir bhi finish kar sakta hai.
        Doosre station ki line par 403 error.notYourStation (All view par bhi).
        Errors: error.itemNotFired, error.itemNotReady, error.itemAlreadyFinal, error.orderClosed."""
        return await client.patch(f"orders/{order_id}/items/{item_id}/status/", {"status": status})

    @mcp.tool()
    async def kds_print_claim(stations: str, since: str) -> Any:
        """Station ki printer screen ke liye lines CLAIM karna (POST kds/print/; kitchen staff). Jo lines
        chune hue stations par fired/preparing hain, `since` ke baad fire hui hain aur abhi chhapi nahi,
        unhein foran 'printed' mark kar deta hai (ek transaction mein, taake ek station ki do screens par
        ek line do dafa na chhapay) aur sirf wahi claimed lines ke saath tickets wapas deta hai:
        {results: [orders]}. SIDE EFFECT: claim wapas nahi hota (ek hi baar milti hai), is liye sirf asli
        print ke liye chalayein, dekhne ke liye nahi (dekhne ke liye kds_tickets).
        `stations`: station ids comma se ('3,5'), 'general' ya 'all' (ek printer wali kitchen); 'mine' ya khali
        nahi chalta (400 error.stationRequired). `since` ISO datetime (jab screen ne auto-print on kiya, taake
        purana backlog na chhape); ghalat par 400 error.dateInvalid."""
        return await client.post("kds/print/", {"stations": stations, "since": since})

    @mcp.tool()
    async def order_item_move_station(order_id: int, item_id: int, station: int | None = None) -> Any:
        """Ek line doosre kitchen station par bhejna (POST orders/<id>/items/<item>/station/; cashier, owner,
        manager ya head chef) - jaise BBQ chef aaj chhutti par ho. `station` = station id, ya None = General.
        Cashier review ke dauran kar sakta hai; line kitchen mein (fired) ho to sirf head chef/owner/manager
        (baqi ko 403 error.forbidden). Sirf jab tak koi chef ne Start nahi kiya (pending ya fired): pakti hui
        line wahin rehti hai (409 error.itemAlreadyStarted). Pending line fire hone par chuna hua station
        rakhti hai. Fired line ho to purana station usay hata deta hai aur naye station ko naya kaam milta hai.
        Errors: error.stationNotFound (400; station isi branch ka aur active ho), error.orderClosed (409).
        Result: updated line."""
        # `station` ko explicit bhejna zaroori hai (None = General), is liye drop_none nahi.
        return await client.post(f"orders/{order_id}/items/{item_id}/station/", {"station": station})

    @mcp.tool()
    async def order_handover(order_id: int) -> Any:
        """Pass par order waiter ko HANDOVER (POST orders/<id>/handover/; head chef, owner ya manager): jab
        order ki kitchen mein maujood SAARI lines ready hon (kds_tickets scope='expo' mein all_ready=true).
        Lines READY hi rehti hain (serve karna waiter ka qadam hai, order_item_set_status) lekin handed_over_at
        lag jata hai, expo screen se hat jati hain aur waiter ko order:handed_over notification milti hai.
        Koi station abhi pakwa raha ho to 409 error.orderNotReady; terminal order par 409 error.orderClosed.
        Result: updated order."""
        return await client.post(f"orders/{order_id}/handover/")

    # ============================================================
    # 7. PUBLIC / CUSTOMER FLOW (login nahi; table QR token se auth)
    # ============================================================
    # NOTE: ye tools customer ban kar kaam karte hain (real orders/bills par asar hota hai).
    @mcp.tool()
    async def public_order_create_qr(
        session_token: str,
        phone: str,
        items: list[OrderLineInput],
        name: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
        location_timestamp: float | None = None,
        request_id: str | None = None,
    ) -> Any:
        """[Customer, public] Table ke QR token se order (ya pehle se khule order mein items add).
        `session_token` bare token, '/t/<token>' ya full URL. `phone` zaroori (customer ki pehchaan).
        Order cashier review ke liye queue hota hai (ya kitchen mein hai to additional items). Wohi dish (same
        size/note/price) dubara aaye to waiting line ki quantity badhti hai, nayi row nahi banti.
        GEOFENCE: table wale order par GPS zaroori hai -> `latitude`, `longitude`, `accuracy_m` (meters) teeno do;
        `location_timestamp` unix seconds (na do to abhi ka). Fix <2 min purani aur distance+accuracy branch ki
        geofence_radius_m ke andar (error.locationStale / error.outsideRestaurantLocation / error.locationNotConfigured).
        IDEMPOTENT via `request_id` (wahi id retry par dein).
        Errors: error.invalidSessionToken, error.phoneRequired, error.itemsRequired, error.tableOccupied
        (table kisi aur phone ke bill par), error.billLocked, error.idempotencyKeyConflict."""
        key = _new_request_id(request_id)
        data = await client.post(
            "orders/qr/",
            drop_none(
                {
                    "session_token": session_token,
                    "phone": phone,
                    "name": name,
                    "items": [dict(i) for i in items],
                    **location_payload(latitude, longitude, accuracy_m, location_timestamp),
                    "request_id": key,
                }
            ),
        )
        return {"request_id": key, "order": data}

    @mcp.tool()
    async def public_order_create_web(
        phone: str,
        items: list[OrderLineInput],
        name: str | None = None,
        branch_id: int | None = None,
        session_token: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
        location_timestamp: float | None = None,
        request_id: str | None = None,
    ) -> Any:
        """[Customer, public] Website se order (POST orders/customer/). `session_token` na ho to table-less
        'web' order bante hain; `branch_id` na do to sabse chhoti id wali branch. Token ho to QR order jaisa.
        Table token dene par GPS (latitude, longitude, accuracy_m) bhi zaroori, QR order jaisa; table-less
        web order mein location nahi lagti. Baqi rules public_order_create_qr jaise (phone, items, request_id)."""
        key = _new_request_id(request_id)
        data = await client.post(
            "orders/customer/",
            drop_none(
                {
                    "session_token": session_token,
                    "branch_id": branch_id,
                    "phone": phone,
                    "name": name,
                    "items": [dict(i) for i in items],
                    **location_payload(latitude, longitude, accuracy_m, location_timestamp),
                    "request_id": key,
                }
            ),
        )
        return {"request_id": key, "order": data}

    @mcp.tool()
    async def public_order_track(order_code: str, phone: str) -> Any:
        """[Public] Order code + customer ka phone se order status (doosre device par fallback lookup).
        Sirf ghalat lookups rate-limited hain. Na milne par 404 error.orderNotFound."""
        return await client.get("orders/track/", {"order_code": order_code, "phone": phone}, auth=False)

    @mcp.tool()
    async def public_order_by_session(session_token: str, order_id: int, phone: str) -> Any:
        """[Public] Table token + order id + phone se ek exact table order. Na milne par 404 error.orderNotFound."""
        return await client.get(
            f"orders/qr/{path_token(session_token)}/{order_id}/", {"phone": phone}, auth=False
        )

    @mcp.tool()
    async def public_bill_get(session_token: str, phone: str, bill_id: int | None = None) -> Any:
        """[Public] Customer ka apna khula bill (server-computed total) + pichle visits ke orders.
        `phone` zaroori. `bill_id` pin karne par agar bill release ho gaya ya table agle customer ke paas
        gaya to 409 error.billSessionEnded. Bill na ho to 404 error.billNotFound (previous_orders ke saath)."""
        return await client.get(
            f"bills/by-session/{path_token(session_token)}/",
            {"phone": phone, "bill_id": bill_id},
            auth=False,
        )

    @mcp.tool()
    async def public_bill_request_pay(
        session_token: str,
        phone: str,
        method: Literal["cash", "online"],
        bill_id: int | None = None,
    ) -> Any:
        """[Customer, public] Payment maangna: bill open -> pay_requested (nayi orders lock ho jati hain).
        method='online' par response mein Moyasar ka publishable_key, callback_url aur description bhi aate hain
        (secret key kabhi nahi). Errors: error.paymentsNotConfigured (503), error.notFound, error.billNotFound.
        Throttled ('bill-pay')."""
        return await client.post(
            "bills/request-pay/",
            drop_none({"session_token": session_token, "phone": phone, "method": method, "bill_id": bill_id}),
            auth=False,
        )

    @mcp.tool()
    async def public_bill_cancel_pay_request(
        session_token: str, phone: str, bill_id: int | None = None
    ) -> Any:
        """[Customer, public] Payment request wapas: pay_requested -> open (galti se Pay dab gaya ya dessert
        add karna hai). Paid ke baad refuse. Throttled ('bill-pay')."""
        return await client.post(
            "bills/cancel-pay-request/",
            drop_none({"session_token": session_token, "phone": phone, "bill_id": bill_id}),
            auth=False,
        )

    @mcp.tool()
    async def public_bill_verify_online(session_token: str, payment_id: str) -> Any:
        """[Public] Moyasar online payment verify: backend payment_id Moyasar API se dobara fetch karke
        paanch conditions check karta hai (status paid, amount, metadata.bill_id, request generation, table)
        tabhi bill paid hoti hai. Result: 'paid'/'failed'/'already_processed' wagera.
        Errors: error.paymentVerificationFailed, error.paymentGatewayUnreachable (502). Throttled ('bill-verify')."""
        return await client.post(
            "bills/verify-online/",
            {"session_token": session_token, "payment_id": payment_id},
            auth=False,
        )
