"""
tables app ke MCP tools: physical tables, open/close/needs-bill, QR sessions.

Backend routes (tables/urls.py):
  GET    tables/                        koi bhi staff   (search, min_seats, status, ordering, pagination)
  POST   tables/                        owner/manager
  GET    tables/<id>/                   koi bhi staff
  PATCH  tables/<id>/                   owner/manager
  DELETE tables/<id>/                   owner/manager   (sirf khali table; warna 409 error.tableNotEmpty)
  POST   tables/<id>/open/              POS staff (owner/manager/cashier)
  POST   tables/<id>/close/             table-release staff (backend ka IsTableReleaseStaff)
  POST   tables/<id>/needs-bill/        POS staff
  GET    tables/active-sessions/        koi bhi staff   (occupied tables + QR payload/token)
  GET    tables/by-session/<token>/     public (AllowAny) customer QR lookup

Table status: "empty" | "occupied" | "needs-bill".
Labels: backend save se pehle label_en ko UPPER-CASE kar deta hai aur extra spaces ek space bana deta hai
("table   1 " -> "TABLE 1"); label_ar ke spaces bhi saaf hote hain. Duplicate check har language mein alag
aur case + SAARE spaces ignore karke hota hai ("Table 1", "TABLE1", "table  1" sab ek hi label hain).
Pagination: default 25/page, `page_size` max 200; response mein metadata.in_use
(poori selection mein khali na hone wali tables ki ginti).
PUT (full update) ke liye alag tool nahi; `table_update` (PATCH) kaafi hai.
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none
from tools._utils import path_token

TableStatus = Literal["empty", "occupied", "needs-bill"]

def register(mcp, client: POSClient) -> None:
    # ================================================================ CRUD
    @mcp.tool()
    async def tables_list(
        search: str | None = None,
        min_seats: int | None = None,
        status: TableStatus | None = None,
        ordering: str | None = None,
        language: Literal["en", "ar"] | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ki tables (koi bhi staff). Har row mein label, seats, status, qr_code, session_token.
        `search` label par; `min_seats=8` = kam az kam 8 seats wali tables ("8 log hain, table hai?");
        `status` filter; `ordering` keys: label, status, seats ('-' = descending);
        `language='ar'` ho to label ordering Arabic par. metadata.in_use = occupied/needs-bill tables."""
        return await client.get(
            "tables/",
            {
                "search": search,
                "min_seats": min_seats,
                "status": status,
                "ordering": ordering,
                "language": language,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def table_get(table_id: int) -> Any:
        """Ek table ki detail (koi bhi staff)."""
        return await client.get(f"tables/{table_id}/")

    @mcp.tool()
    async def table_create(
        branch: int,
        label_en: str,
        label_ar: str,
        seats: int | None = None,
    ) -> Any:
        """Nayi table (owner/manager). `branch` apni branch ki id. `seats` 1..100 (default 2).
        Backend khud permanent QR token/payload banata hai; table 'empty' status par banti hai.
        Labels normalise hote hain (label_en UPPER-CASE, extra spaces saaf) aur label_en ya label_ar
        branch mein pehle se kisi table ka ho (case/spaces ignore karke) to
        error.tableLabelExists, error mein wo field jo clash hui."""
        return await client.post(
            "tables/",
            drop_none({"branch": branch, "label_en": label_en, "label_ar": label_ar, "seats": seats}),
        )

    @mcp.tool()
    async def table_update(
        table_id: int,
        label_en: str | None = None,
        label_ar: str | None = None,
        seats: int | None = None,
    ) -> Any:
        """Table PATCH (owner/manager); sirf diye gaye fields badalte hain.
        Status, QR code aur session_token yahan se nahi badalte (sirf open/close/needs-bill se).
        Label rules table_create jaise: label_en UPPER-CASE ban jata hai, duplicate check (case/spaces ignore)
        label_en aur label_ar dono par; clash par error.tableLabelExists."""
        body = drop_none({"label_en": label_en, "label_ar": label_ar, "seats": seats})
        return await client.patch(f"tables/{table_id}/", body)

    @mcp.tool()
    async def table_delete(table_id: int) -> Any:
        """Table delete (owner/manager). Sirf tab jab table 'empty' ho aur us par koi asli open order na ho,
        warna 409 error.tableNotEmpty (kuch delete nahi hota). KHALI orders (koi non-voided line nahi aur koi
        payment nahi) rukawat nahi hain: staff ka bina-customer draft soft-delete ho jata hai, baqi (jaise customer
        ka QR order jis ki saari lines hat gayin) cancel ho jate hain (event 'empty_order_discarded'; ye
        cancelled-orders report mein nahi ginta).
        Poora check ek transaction mein hota hai."""
        return await client.delete(f"tables/{table_id}/")

    # ======================================================== POS actions
    @mcp.tool()
    async def table_open(table_id: int) -> Any:
        """Table ko 'occupied' mark karta hai (POS staff: owner/manager/cashier). Updated table wapas aati hai."""
        return await client.post(f"tables/{table_id}/open/")

    @mcp.tool()
    async def table_needs_bill(table_id: int) -> Any:
        """Table ko 'needs-bill' mark karta hai (POS staff) - customer bill maang raha hai."""
        return await client.post(f"tables/{table_id}/needs-bill/")

    @mcp.tool()
    async def table_close(table_id: int) -> Any:
        """Table ko release karke 'empty' karta hai (table-release permission wale staff).
        Is se us table ke open bills bhi close hote hain.
        Khali orders (koi non-voided line nahi, koi payment nahi) release ko nahi rokte: draft delete aur
        baqi cancel ho jate hain (cancelled report mein nahi ginte). Lekin jis order par abhi kuch baqi hai (items ya payment) wo rokta hai: 409
        error.tableHasOpenOrder, jis ke saath blocking orders ki list (id, order_number, status, opened_at);
        pehle wo orders settle ya cancel karein. Rukne par kuch nahi badalta (rollback)."""
        return await client.post(f"tables/{table_id}/close/")

    # ================================================== QR / customer side
    @mcp.tool()
    async def tables_active_sessions() -> Any:
        """Abhi occupied tables (koi bhi staff), id order mein, QR payload aur session_token ke saath:
        {count, results}. Khoye hue QR label ko dobara print karne ya customer ko manual-entry code
        batane ke kaam aata hai. Closed (empty) tables yahan nahi aati."""
        return await client.get("tables/active-sessions/")

    @mcp.tool()
    async def table_by_session_get(token: str, phone: str | None = None) -> Any:
        """Public customer lookup (login ki zaroorat nahi): QR session token se table aur uska status.
        `token` bare token, '/t/<token>' payload ya full URL - teeno chalte hain.
        Response: table, occupied, can_order, bill_status, bill_id, open_order.
        `phone` dene par (sirf usi customer ko) apna bill/open order dikhta hai; bina phone ke
        can_order/bill/open_order khali (None) rehte hain. Ghalat token par 404 error.notFound."""
        return await client.get(
            f"tables/by-session/{path_token(token)}/",
            {"phone": phone},
            auth=False,
        )
