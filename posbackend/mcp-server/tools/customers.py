"""
customers app ke MCP tools: customer directory aur marketing consent.

Backend routes (customers/urls.py) -- sab branch-scoped:
  GET    customers/                    owner/manager  (filters + pagination)
  POST   customers/                    POS staff      (FIND-OR-CREATE by phone, 200 existing / 201 new)
  GET    customers/<id>/               owner/manager
  PATCH  customers/<id>/               owner/manager  (name, phone)
  DELETE customers/<id>/               owner/manager  (soft-delete)
  POST   customers/<id>/consent/       owner/manager  (marketing consent + timestamp)

Design notes:
  * Customer ki pehchaan phone hai (password nahi). Branch ke andar phone unique
    (unique_phone_per_branch).
  * Marketing consent teen-state hai: 'unknown' (kabhi pucha nahi) / 'opted_in' / 'opted_out'.
    'opted_out' kisi campaign mein target nahi ho sakta. Consent badalne ka WAHID tareeqa
    `customer_set_consent` hai (PATCH se nahi badalta), taake "kab se" ka timestamp hamesha saath move kare
    (PDPL compliance).
  * `last_order_at` read-only hai: order link karne se khud set hota hai.
  * PUT (full update) ke liye alag tool nahi; `customer_update` (PATCH) kaafi hai.
  * Pagination: default 25/page, `page_size` max 200.
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none

Consent = Literal["unknown", "opted_in", "opted_out"]


def register(mcp, client: POSClient) -> None:
    @mcp.tool()
    async def customers_list(
        search: str | None = None,
        phone: str | None = None,
        marketing_consent: Consent | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ke customers (owner/manager). `search` naam ya phone par (partial);
        `phone` EXACT match (jaisa store hai, normalise nahi hota); `marketing_consent` filter.
        `ordering` keys: name, phone, last_order_at, created_at ('-' = descending);
        default: sabse recent order wale pehle (jinhon ne kabhi order nahi kiya wo aakhir mein)."""
        return await client.get(
            "customers/",
            {
                "search": search,
                "phone": phone,
                "marketing_consent": marketing_consent,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def customer_get(customer_id: int) -> Any:
        """Ek customer ki detail (owner/manager): phone, name, last_order_at, marketing_consent,
        consent_changed_at."""
        return await client.get(f"customers/{customer_id}/")

    @mcp.tool()
    async def customer_find_or_create(phone: str, name: str | None = None) -> Any:
        """Phone se customer dhoondo, na mile to naya banao (POS staff: owner/manager/cashier).
        Matching smart hai: Arabic/Persian digits ASCII ban jate hain, aur aakhri 9 digits compare hote
        hain, is liye '+966 50 123 4567' wahi customer dhoondta hai jisne '0501234567' likha tha.
        Soft-deleted customer mil jaye to restore ho jata hai (duplicate nahi banta).
        Naya customer wahi phone rakhta hai jaisa diya gaya. `name` sirf NAYE customer par lagta hai.
        Backend 200 (pehle se tha) / 201 (naya) deta hai; response mein sirf customer ki detail aati hai.
        Error: error.phoneRequired."""
        return await client.post("customers/", drop_none({"phone": phone, "name": name}))

    @mcp.tool()
    async def customer_update(
        customer_id: int,
        name: str | None = None,
        phone: str | None = None,
    ) -> Any:
        """Customer PATCH (owner/manager): sirf name/phone. Phone branch mein pehle se kisi aur ka ho to
        error.phoneTaken (yahan exact match hota hai, find_or_create wali smart matching nahi).
        Marketing consent yahan nahi badalta -- customer_set_consent use karein."""
        return await client.patch(f"customers/{customer_id}/", drop_none({"name": name, "phone": phone}))

    @mcp.tool()
    async def customer_delete(customer_id: int) -> Any:
        """Customer delete (owner/manager, soft-delete). Wahi phone dobara find_or_create par customer ko
        restore kar deta hai."""
        return await client.delete(f"customers/{customer_id}/")

    @mcp.tool()
    async def customer_set_consent(customer_id: int, marketing_consent: Consent) -> Any:
        """Marketing consent record karna (owner/manager). 'opted_in' / 'opted_out' par consent_changed_at
        abhi ka waqt ban jata hai; 'unknown' par clear (None). Sirf tab call karein jab customer ne wakai
        haan/na kaha ho -- ye PDPL ke liye audit record hai, aur campaigns isi par audience filter karti hain.
        Error: error.invalidConsent."""
        return await client.post(
            f"customers/{customer_id}/consent/", {"marketing_consent": marketing_consent}
        )
