"""
POS backend MCP server (FastMCP, stdio).

Chalane ke liye:      python server.py
Inspector se test:    fastmcp dev server.py     (ya: npx @modelcontextprotocol/inspector python server.py)
Claude Desktop/Code:  command = python, args = ["<path>/mcp-server/server.py"], env mein
                      API_BASE_URL / STAFF_ID / PIN (ya .env file).

Naya app add karna ho to: tools/<app>.py banao (register(mcp, client)), upar import karo aur
TOOL_MODULES mein ek line add karo. Baqi sab (client, auth, errors) khud sambhalta hai.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from client import POSClient
from tools import (
    accounts,
    branches,
    catalog,
    customers,
    imports,
    inventory,
    kitchen,
    marketing,
    orders,
    purchasing,
    reports,
    reviews,
    tables,
    uploads,
    vat_returns,
    zatca,
)

# stdout MCP protocol ka hai (stdio transport) -> logs hamesha stderr par.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pos-mcp")

client = POSClient()

INSTRUCTIONS = """\
Restaurant POS backend (Django REST) ke tools. Server aik staff account (STAFF_ID/PIN) se login hota hai,
is liye har tool ki permission usi account ke role (owner/manager/cashier/waiter/kitchen) par depend karti hai;
403 aaye to role kam hai. Sab data us staff ki apni branch tak scoped hai.

Usool:
- Backend errors codes mein aate hain (jaise error.orderClosed); unka matlab tool ke docstring mein hai.
- Paison, stock, customers ko message, ya public posts par asar daalne wale tools (order_cancel, order_refund,
  purchase_receive/delete, campaign_send/publish, inventory adjust, imports_commit wagera) pehle preview/summary tool se check
  karke, tab chalayein.
- `public_*` tools customer ban kar chalte hain (login nahi); asli orders/bills par asar hota hai.
- Lists paginated hain (default 25, `page_size` max 200).
"""


@asynccontextmanager
async def lifespan(_server: FastMCP):
    """Startup par config ki jaanch aur shutdown par HTTP client band."""
    if not client.staff_id or not client.pin:
        log.warning("STAFF_ID / PIN set nahi hain -- login wale tools fail honge (.env ya MCP config ka env block dekhein)")
    log.info("POS MCP server ready -> %s", client.base_url)
    try:
        yield
    finally:
        await client.aclose()


mcp = FastMCP("posbackend", instructions=INSTRUCTIONS, lifespan=lifespan)

# --- tool modules: ek app = ek module ---
TOOL_MODULES = [
    accounts,    # auth, staff, shifts, shift templates, attendance settings
    branches,    # public/active branch, branch update, location
    catalog,     # public menu, categories, products, deals, variants
    tables,      # tables, open/close/needs-bill, QR sessions
    kitchen,     # kitchen stations, routing settings (orders/catalog/accounts tools use station ids)
    orders,      # orders, items, workflow, payments, refunds, bills, KDS, customer QR flow
    inventory,   # ingredients, stock adjust, ledger, recipes
    purchasing,  # suppliers, purchase orders, supplier invoices/credit notes, expenses, input VAT
    vat_returns, # VAT periods, filing, SADAD payments/refunds, history, invoice archive
    customers,   # customer directory, marketing consent
    reviews,     # review moderation, public reviews
    reports,     # read-only business reports
    marketing,   # campaigns, AI drafts, Facebook/Instagram connection
    uploads,     # Cloudinary signed uploads (images/documents/campaign media)
    imports,     # Excel bulk import (stock/menu/recipes), AI workbook draft
    zatca,       # ZATCA e-invoicing: settings, devices, go-live wizard, documents, receipts, B2B buyers
    # realtime: REST API nahi hai, isliye tool nahi
]

for module in TOOL_MODULES:
    module.register(mcp, client)


if __name__ == "__main__":
    mcp.run(transport="stdio")
