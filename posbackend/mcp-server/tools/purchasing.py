"""
purchasing app ke MCP tools: suppliers, purchase orders (PO), supplier tax documents, input VAT.

Backend routes (purchasing/urls.py):
  suppliers/            CRUD     read: koi bhi staff | write: owner/manager   (NOT branch-scoped)
  purchases/            CRUD     read: koi bhi staff | write: owner/manager   (branch-scoped)
  GET  purchases/summary/                      owner/manager
  POST purchases/<id>/set-status/              owner/manager  (draft|ordered|received)
  POST purchases/<id>/receive/                 owner/manager (write action)
  GET  purchases/<id>/pdf/                     owner/manager
  GET/PUT/DELETE purchases/<id>/supplier-invoice/        owner/manager (custom action: sirf list/retrieve
  GET/POST       purchases/<id>/supplier-credit-notes/    baqi staff ke liye khula hai, baqi sab owner/manager)
  PATCH/DELETE   purchases/<id>/supplier-credit-notes/<doc_id>/
  POST purchases/qr-check/                     supplier QR decode (kuch save nahi hota)
  GET  purchases/input-vat/                    input VAT report (PO supplier documents + expenses)
  GET  purchases/input-vat/csv/                accountant ke liye CSV
  expenses/             GET list, POST create, GET/PATCH <id>   owner/manager (DELETE nahi, void hota hai)
  POST expenses/<id>/void/                     owner/manager

PO status: draft -> ordered -> received.  Stock SIRF received hone par badalta hai.

Stock-sensitive behaviour (backend ke views.py se):
  * receive / set_status('received') : har line ka stock barhta hai (ledger 'purchase'), cost_per_unit update.
  * received PO ko EDIT (purchase_update) : pehle stock reverse, edit, phir dobara apply (ek transaction).
  * received PO ko DELETE : pehle uska stock wapas nikalta hai.
  * received se wapas draft/ordered (set_status) : stock reverse.
  Isliye received POs par update/delete/set_status ko DESTRUCTIVE samjhein.

VAT period lock (backend ka `vat_returns` app, vat_returns tools dekhein): jis VAT period ka return file ho chuka ho us
mein koi supplier document / expense banana, badalna, hataana ya void karna 409 error.vatPeriodLocked deta hai.
Sirf OWNER `lock_override_reason` (kam az kam 10 characters, request body mein; DELETE par query string mein) bhej
kar ise bypass kar sakta hai (non-owner ko 403 error.vatOverrideOwnerOnly, choti wajah par 400
error.vatOverrideReasonShort; har override audit log mein jata hai); jo tools document/expense
likhte hain unmein ye parameter hai. Filed period mein dated document by default sabse purane KHULE period ki
pehli tareekh par claim hota hai (`claim_date`/`claim_period`; `issue_period` asal date ka period);
owner `keep_in_filed_period=True` se asal period mein claim kara sakta hai (override chahiye).

VAT snapshot: document par supplier ki VAT details (registered, VAT number, CRN) save ke waqt COPY ho jati hain
aur `claimable_vat` usi copy se nikalta hai (stored figure). Supplier ko baad mein theek karne se uske documents
khud refresh hote hain (filed period wale jaise filed the waise rehte hain). Claimable sirf tab jab supplier
VAT-registered ho AND VAT number ho; credit note par negative.

Notes:
  * Quantity 3 decimals, money 2 decimals par round hoti hai.
  * Dates 'YYYY-MM-DD'. List/summary ke date filters branch timezone mein inclusive.
  * Pagination: default 25/page, `page_size` max 200.
  * Supplier ki VAT number 15 digits (Saudi format) aur CRN 10 digits backend validate karta hai.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

from client import POSClient, drop_none
from tools.reports import DateFrom, DateTo, Days, Preset

PurchaseStatus = Literal["draft", "ordered", "received"]
VatCategory = Literal["standard", "zero", "exempt"]  # VAT return ka purchases box; VAT 0 par default 'zero'
ExpenseCategory = Literal["rent", "utilities", "equipment", "maintenance", "services", "marketing", "other"]
NotClaimableReason = Literal[
    "personal_use", "entertainment", "gifts", "passenger_car", "staff_benefits", "other"
]
DocKind = Literal["invoice", "credit_note"]
EditableStatus = Literal["draft", "ordered"]  # create/update par 'received' allowed nahi


class PurchaseLine(TypedDict):
    """PO ki ek line."""

    inventory_item: int  # isi branch ki inventory item id
    quantity: float  # >= 0.001, 3 decimals
    unit_cost: float  # >= 0, 2 decimals


class SupplierDocInput(TypedDict):
    """Supplier ki tax invoice / credit note, jaisa paper ya PDF par chhapa hai."""

    number: str  # supplier ka document number (max 64)
    issue_date: str  # YYYY-MM-DD
    net: float
    vat: float  # supplier VAT-registered na ho to 0 hona chahiye
    total: float
    vat_category: NotRequired[VatCategory]  # zero/exempt ke saath vat 0 hona chahiye (error.vatCategoryNeedsZeroVat)
    keep_in_filed_period: NotRequired[bool]  # sirf owner + lock_override_reason; filed period mein hi claim
    reason: NotRequired[str]  # credit note ke liye zaroori
    qr_text: NotRequired[str]  # supplier QR ka base64 text; har save par decode/check hota hai
    xml: NotRequired[str]
    pdf_url: NotRequired[str]


class ReturnedLine(TypedDict):
    purchase_item: int  # PO line (purchase item) ki id
    quantity: float  # <= us line ki quantity


def _qty(v: float) -> float:
    return round(v, 3)


def _money(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


def _doc(d: SupplierDocInput) -> dict[str, Any]:
    """SupplierDocInput -> API body (amounts round)."""
    body = dict(d)
    for k in ("net", "vat", "total"):
        if k in body:
            body[k] = _money(body[k])
    return body


def _lines(lines: list[PurchaseLine]) -> list[dict[str, Any]]:
    return [
        {
            "inventory_item": l["inventory_item"],
            "quantity": _qty(l["quantity"]),
            "unit_cost": _money(l["unit_cost"]),
        }
        for l in lines
    ]


def register(mcp, client: POSClient) -> None:
    # ============================================================ suppliers
    @mcp.tool()
    async def suppliers_list(
        search: str | None = None,
        has_contact: bool | None = None,
        ordering: str | None = None,
        language: Literal["en", "ar"] | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Suppliers (koi bhi staff; branch-scoped nahi, sab branches ke liye common).
        `search` naam (en/ar) ya contact_info par; `has_contact` True/False; `ordering` keys: name, contact
        ('-' = descending); `language='ar'` ho to naam ki ordering Arabic par."""
        return await client.get(
            "suppliers/",
            {
                "search": search,
                "has_contact": has_contact,
                "ordering": ordering,
                "language": language,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def supplier_get(supplier_id: int) -> Any:
        """Ek supplier ki detail (koi bhi staff)."""
        return await client.get(f"suppliers/{supplier_id}/")

    @mcp.tool()
    async def supplier_create(
        name_en: str,
        name_ar: str,
        contact_info: str | None = None,
        vat_registered: bool = True,
        vat_number: str | None = None,
        crn: str | None = None,
        address: str | None = None,
    ) -> Any:
        """Naya supplier (owner/manager). `vat_registered=True` (default) ho to input-VAT claim ke liye
        `vat_number` (15 digits, Saudi format) bharna chahiye. `crn` commercial registration number (10 digits).
        Galat format par backend ke validation errors (jaise vat_number ke)."""
        body = drop_none(
            {
                "name_en": name_en,
                "name_ar": name_ar,
                "contact_info": contact_info,
                "vat_registered": vat_registered,
                "vat_number": vat_number,
                "crn": crn,
                "address": address,
            }
        )
        return await client.post("suppliers/", body)

    @mcp.tool()
    async def supplier_update(
        supplier_id: int,
        name_en: str | None = None,
        name_ar: str | None = None,
        contact_info: str | None = None,
        vat_registered: bool | None = None,
        vat_number: str | None = None,
        crn: str | None = None,
        address: str | None = None,
    ) -> Any:
        """Supplier PATCH (owner/manager); sirf diye gaye fields badalte hain.
        Supplier ki VAT details (vat_registered, vat_number, crn) badalne se uske saare supplier documents aur
        (non-voided) expenses par copy dobara lagti hai, taake jaise VAT number bharne se unki VAT claimable ho
        jaye; filed VAT period wale documents jaise filed the waise rehte hain."""
        body = drop_none(
            {
                "name_en": name_en,
                "name_ar": name_ar,
                "contact_info": contact_info,
                "vat_registered": vat_registered,
                "vat_number": vat_number,
                "crn": crn,
                "address": address,
            }
        )
        return await client.patch(f"suppliers/{supplier_id}/", body)

    @mcp.tool()
    async def supplier_delete(supplier_id: int) -> Any:
        """Supplier delete (owner/manager, soft-delete). Jis supplier ke koi purchases YA expenses hon wo delete
        nahi hota: 409 error.supplierInUse."""
        return await client.delete(f"suppliers/{supplier_id}/")

    # ============================================================ purchases
    @mcp.tool()
    async def purchases_list(
        supplier: int | None = None,
        status: PurchaseStatus | None = None,
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ke purchase orders (koi bhi staff). Filters: `supplier` id, `status`, `search`
        (supplier naam ya PO number), `date_from`/`date_to` (ordered_at par, YYYY-MM-DD).
        `ordering` keys: supplier, status, ordered_at, received_at, total ('-' = descending; default newest pehle)."""
        return await client.get(
            "purchases/",
            {
                "supplier": supplier,
                "status": status,
                "search": search,
                "from": date_from,
                "to": date_to,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def purchase_get(purchase_id: int) -> Any:
        """Ek PO ki poori detail (koi bhi staff): items (stock_before/added/after received hone ke baad),
        subtotal, supplier_invoice, supplier_credit_notes."""
        return await client.get(f"purchases/{purchase_id}/")

    @mcp.tool()
    async def purchase_create(
        branch: int,
        supplier: int,
        items: list[PurchaseLine],
        status: EditableStatus = "ordered",
    ) -> Any:
        """Naya PO (owner/manager). `branch` apni branch, `supplier` supplier id, `items` kam az kam ek line
        (error.purchaseItemsRequired); har inventory_item isi branch ka (error.inventoryItemInvalid).
        `status` 'draft' ya 'ordered' (default); 'received' seedha nahi, purchase_receive use karein
        (error.usePurchaseReceiveEndpoint). Stock yahan nahi badalta."""
        return await client.post(
            "purchases/",
            {"branch": branch, "supplier": supplier, "status": status, "items": _lines(items)},
        )

    @mcp.tool()
    async def purchase_update(
        purchase_id: int,
        supplier: int | None = None,
        status: EditableStatus | None = None,
        items: list[PurchaseLine] | None = None,
    ) -> Any:
        """PO PATCH (owner/manager). `items` dene par lines poori REPLACE hoti hain. Status yahan sirf
        draft/ordered; received ke liye purchase_set_status.
        DHYAN: RECEIVED PO par edit ho to backend pehle stock reverse karta hai, edit lagata hai, phir naya
        stock apply karta hai (20 KG -> 15 KG = stock mein +15 hi rehta hai). Sab ek transaction mein; fail
        ho to stock waisa hi rehta hai."""
        body = drop_none(
            {"supplier": supplier, "status": status, "items": _lines(items) if items is not None else None}
        )
        return await client.patch(f"purchases/{purchase_id}/", body)

    @mcp.tool()
    async def purchase_delete(purchase_id: int) -> Any:
        """PO delete (owner/manager). DESTRUCTIVE: received PO ho to pehle uska add kiya hua stock wapas
        nikalta hai (ledger mein entry ke saath), phir delete. Jis PO par supplier invoice/credit note lagi ho
        wo delete NAHI hota (tax record hain): 409 error.purchaseHasSupplierDocuments, pehle documents hataein."""
        return await client.delete(f"purchases/{purchase_id}/")

    @mcp.tool()
    async def purchase_set_status(purchase_id: int, status: PurchaseStatus) -> Any:
        """PO ka status kisi bhi waqt badalna (owner/manager): draft | ordered | received.
        -> received: lines stock mein add (aur received_at set); received se bahar: stock wapas nikalta hai
        (galti se received mark hone ka correction); baqi: sirf label, stock nahi. Ek transaction mein.
        Error: error.purchaseStatusInvalid."""
        return await client.post(f"purchases/{purchase_id}/set-status/", {"status": status})

    @mcp.tool()
    async def purchase_receive(
        purchase_id: int,
        supplier_invoice: SupplierDocInput | None = None,
        lock_override_reason: str | None = None,
    ) -> Any:
        """PO receive karna: har line ka stock barhta hai aur InventoryItem.cost_per_unit asal ada ki hui
        qeemat se update hoti hai; status 'received' (owner/manager). Optional `supplier_invoice` isi call
        mein save ho jati hai (QR check ke saath, supplier VAT snapshot aur claim_date ke saath). Do dafa
        receive nahi hota: 409 error.purchaseAlreadyReceived. Invoice ghalat ho to poora call fail
        (stock nahi badalta). Invoice ki date filed VAT period mein ho to 409 error.vatPeriodLocked, jab tak
        OWNER `lock_override_reason` na de."""
        body: dict[str, Any] = {"supplier_invoice": _doc(supplier_invoice)} if supplier_invoice else {}
        if lock_override_reason:
            body["lock_override_reason"] = lock_override_reason
        return await client.post(f"purchases/{purchase_id}/receive/", body)

    @mcp.tool()
    async def purchase_pdf_download(purchase_id: int, save_dir: str | None = None) -> Any:
        """PO ka PDF (owner/manager) server se download karke LOCAL file mein save karta hai (binary hone ki wajah se
        chat mein nahi aa sakta). `save_dir` na do to system temp folder ke 'pos-mcp' mein.
        Result: {path, bytes}. Ye MCP server wali machine ka path hai. Error: 503 error.pdfNotConfigured."""
        content = await client.get_bytes(f"purchases/{purchase_id}/pdf/")
        folder = Path(save_dir) if save_dir else Path(tempfile.gettempdir()) / "pos-mcp"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"PO-{purchase_id}.pdf"
        target.write_bytes(content)
        return {"path": str(target), "bytes": len(content)}

    @mcp.tool()
    async def purchases_summary(
        status: PurchaseStatus | None = None,
        supplier: int | None = None,
        search: str | None = None,
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
    ) -> Any:
        """Khareedari ka summary (owner/manager). Date range: `preset` | `days` | `date_from`+`date_to`
        (reports jaisi: preset > date_from > days; default aakhri 30 din, branch timezone). `status`,
        `supplier` id, `search` (supplier naam/PO number) filters.
        Response: from, to, purchase_order_count, received_order_count, total_purchase_cost,
        average_purchase_value, pending_purchase_value, ingredients_purchased_by_unit ({unit: quantity}),
        top_purchased_ingredients [{inventory_item_id, name, unit, quantity, spend}] (top 10 by spend),
        supplier_spend [{supplier_id, supplier_name, total}]."""
        return await client.get(
            "purchases/summary/",
            {
                "status": status,
                "supplier": supplier,
                "search": search,
                "preset": preset,
                "days": days,
                "from": date_from,
                "to": date_to,
            },
        )

    # ============================================= supplier tax invoice (<=1 per PO)
    @mcp.tool()
    async def purchase_supplier_invoice_get(purchase_id: int) -> Any:
        """PO ki supplier tax invoice (owner/manager; custom actions par backend sirf owner/manager allow karta hai). Na ho to null.
        Response mein qr_check (QR decode aur mismatch flags), vat_category, supplier ka saved snapshot
        (supplier_vat_registered, supplier_vat_number, supplier_crn), claimable_vat, claim_date, claim_period aur
        issue_period (None jab claim period asal date wala hi ho) bhi."""
        return await client.get(f"purchases/{purchase_id}/supplier-invoice/")

    @mcp.tool()
    async def purchase_supplier_invoice_set(
        purchase_id: int, invoice: SupplierDocInput, lock_override_reason: str | None = None
    ) -> Any:
        """Supplier invoice create ya REPLACE (PUT; owner/manager). Har PO par ek hi. QR har save par dobara
        check hoti hai. Supplier VAT-registered na ho to vat 0 hona chahiye (error.supplierNotVatRegistered).
        VAT 0 par vat_category default 'zero' hai (owner 'exempt' kar sakta hai); zero/exempt ke saath vat > 0 par
        error.vatCategoryNeedsZeroVat. Filed VAT period mein ho to 409 error.vatPeriodLocked jab tak OWNER
        `lock_override_reason` na de. 201 = nayi, 200 = replace."""
        body = _doc(invoice)
        if lock_override_reason:
            body["lock_override_reason"] = lock_override_reason
        return await client.put(f"purchases/{purchase_id}/supplier-invoice/", body)

    @mcp.tool()
    async def purchase_supplier_invoice_delete(
        purchase_id: int, lock_override_reason: str | None = None
    ) -> Any:
        """Supplier invoice hata do (owner/manager; sirf keying ki ghalti ke liye). Filed VAT period ki invoice
        409 error.vatPeriodLocked deti hai jab tak OWNER `lock_override_reason` (query string mein; backend DELETE par wahin se padhta hai) na de."""
        return await client.delete(
            f"purchases/{purchase_id}/supplier-invoice/", {"lock_override_reason": lock_override_reason}
        )

    # ================================================== supplier credit notes
    @mcp.tool()
    async def purchase_credit_notes_list(purchase_id: int) -> Any:
        """PO par supplier ke credit notes (returns ke liye), owner/manager."""
        return await client.get(f"purchases/{purchase_id}/supplier-credit-notes/")

    @mcp.tool()
    async def purchase_credit_note_create(
        purchase_id: int,
        note: SupplierDocInput,
        returned_lines: list[ReturnedLine] | None = None,
        lock_override_reason: str | None = None,
    ) -> Any:
        """Supplier credit note record (owner/manager). `note.reason` zaroori
        (error.supplierCreditReasonRequired). `returned_lines` dene par wapas kiya hua maal stock se nikalta
        hai ('adjustment' movement, PO se linked; baad mein PO edit se wapas nahi aata). Wo sirf RECEIVED PO par
        (409 error.supplierReturnNotReceived); quantity line ki quantity se zyada nahi
        (error.supplierReturnTooMuch); stock negative ho to 409. Credit note aur stock removal ek transaction.
        Bina returned_lines ke sirf document save hota hai. Credit note ki claimable_vat NEGATIVE hoti hai.
        Filed VAT period mein ho to 409 error.vatPeriodLocked jab tak OWNER `lock_override_reason` na de."""
        body = _doc(note)
        if returned_lines:
            body["returned_lines"] = [
                {"purchase_item": l["purchase_item"], "quantity": _qty(l["quantity"])} for l in returned_lines
            ]
        if lock_override_reason:
            body["lock_override_reason"] = lock_override_reason
        return await client.post(f"purchases/{purchase_id}/supplier-credit-notes/", body)

    @mcp.tool()
    async def purchase_credit_note_update(
        purchase_id: int,
        document_id: int,
        number: str | None = None,
        issue_date: str | None = None,
        net: float | None = None,
        vat: float | None = None,
        total: float | None = None,
        reason: str | None = None,
        qr_text: str | None = None,
        xml: str | None = None,
        pdf_url: str | None = None,
        vat_category: VatCategory | None = None,
        lock_override_reason: str | None = None,
    ) -> Any:
        """Credit note ki keying theek karna (PATCH; owner/manager); sirf diye gaye fields. Ye stock nahi
        badalta. Na milne par 404 error.notFound. Filed VAT period ka note 409 error.vatPeriodLocked deta hai jab
        tak OWNER `lock_override_reason` na de. VAT badalne par claimable_vat khud dobara nikalta hai."""
        body = drop_none(
            {
                "number": number,
                "issue_date": issue_date,
                "net": _money(net),
                "vat": _money(vat),
                "total": _money(total),
                "reason": reason,
                "qr_text": qr_text,
                "xml": xml,
                "pdf_url": pdf_url,
                "vat_category": vat_category,
                "lock_override_reason": lock_override_reason,
            }
        )
        return await client.patch(f"purchases/{purchase_id}/supplier-credit-notes/{document_id}/", body)

    @mcp.tool()
    async def purchase_credit_note_delete(
        purchase_id: int, document_id: int, lock_override_reason: str | None = None
    ) -> Any:
        """Credit note hata do (owner/manager). Wapas kiya hua maal stock mein WAPAS NAHI aata (wo alag
        adjustment hai, jaan-boojh kar). Na milne par 404. Filed VAT period ka note 409 error.vatPeriodLocked
        deta hai jab tak OWNER `lock_override_reason` (query string mein; backend DELETE par wahin se padhta hai) na de."""
        return await client.delete(
            f"purchases/{purchase_id}/supplier-credit-notes/{document_id}/",
            {"lock_override_reason": lock_override_reason},
        )

    # =========================================================== VAT helpers
    @mcp.tool()
    async def purchases_qr_check(
        qr_text: str,
        supplier: int | None = None,
        issue_date: str | None = None,
        total: float | None = None,
        vat: float | None = None,
    ) -> Any:
        """Supplier invoice ka QR decode karke dekhna (kuch save nahi hota; form ka 'Check QR' button).
        `supplier`/`issue_date`/`total`/`vat` dene par QR ki fields un se compare hoti hain; mismatch flags
        (vatNumber, total, vat, date, supplierVatMissing) wapas aate hain. Tolerance 0.01."""
        return await client.post(
            "purchases/qr-check/",
            drop_none(
                {
                    "qr_text": qr_text,
                    "supplier": supplier,
                    "issue_date": issue_date,
                    "total": _money(total),
                    "vat": _money(vat),
                }
            ),
        )

    @mcp.tool()
    async def purchases_input_vat(
        preset: Preset = None, days: Days = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> Any:
        """Input VAT report (owner/manager). Date range: `preset` | `days` | `date_from`+`date_to` (reports jaisi: preset > date_from > days;
        default aakhri 30 din). Documents unki
        APNI issue_date par chunay jate hain: PO ke supplier documents (deleted PO ke nahi) aur non-voided
        expenses, ek saath (`source`: 'purchase' | 'expense'). claimable_vat stored figure hai (kuch dobara
        calculate nahi hota). Credit notes ki net/vat/total aur claimable_vat negative.
        Response: from, to,
        rows -> [{source, id, kind, purchase (None for expense), supplier, supplier_name_en/ar,
                  supplier_vat_number, supplier_crn, vat_registered (document par saved snapshot), number,
                  issue_date, vat_category, net, vat, total, vat_rate (% of net), claimable_vat, flags,
                  needs_review, category, capital_asset, not_claimable_reason}],
        missing_invoices -> period mein received PO jin ki supplier invoice nahi (flags noInvoice, notRegistered),
        totals -> {net, vat, total, claimable_vat},
        review -> {count, vat}: wo documents jin ki VAT roki gayi (supplier VAT-registered nahi ya VAT number
        missing: flags notRegistered / supplierVatMissing) - "VAT needing review", claim 0 jab tak supplier
        theek na ho. Baqi flags: noQr, qrUnreadable, qrMismatch, totalsDontAdd, vatNot15 (VAT net ke 15% se
        0.05 se zyada door), netDiffersFromPurchase, notClaimable, noAttachment, possibleDuplicate."""
        return await client.get(
            "purchases/input-vat/", {"preset": preset, "days": days, "from": date_from, "to": date_to}
        )

    @mcp.tool()
    async def purchases_input_vat_csv(
        preset: Preset = None,
        days: Days = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
        save_dir: str | None = None,
    ) -> Any:
        """Wohi input VAT report accountant ke liye CSV file mein (owner/manager): UTF-8 BOM ke saath, upar
        restaurant/VAT number/period ka header block, credit notes negative, aakhir mein TOTALS row. Screen aur
        file ek hi function se banti hain, is liye kabhi alag nahi hongi. Disk par save hoti hai
        (`save_dir` na do to temp folder ke 'pos-mcp' mein): {path, bytes}. Date range purchases_input_vat jaisi (preset | days | date_from+date_to; default aakhri 30 din)."""
        content = await client.get_bytes(
            "purchases/input-vat/csv/", {"preset": preset, "days": days, "from": date_from, "to": date_to}
        )
        folder = Path(save_dir) if save_dir else Path(tempfile.gettempdir()) / "pos-mcp"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = f"{date_from or preset or (str(days) + 'd' if days else 'last30d')}"
        target = folder / f"input-vat_{stamp}_{date_to or 'today'}.csv"
        target.write_bytes(content)
        return {"path": str(target), "bytes": len(content)}

    # ============================================================ expenses
    @mcp.tool()
    async def expenses_list(
        voided: Literal["all"] | None = None,
        supplier: int | None = None,
        category: ExpenseCategory | None = None,
        capital_asset: bool | None = None,
        claim: Literal["claimable", "not_claimable", "review"] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        search: str | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Business expenses with VAT (rent, utilities, equipment, ... PO ke bahar) - owner/manager only.
        Voided expenses by default chhupe hote hain; `voided='all'` se dikhte hain (struck-through, claim 0).
        Filters: `supplier` id, `category`, `capital_asset`, `claim`: 'claimable' (claimable VAT != 0),
        'not_claimable' (owner ne not-claimable mark kiya), 'review' (VAT hai lekin supplier VAT-registered nahi ya
        VAT number nahi), `date_from`/`date_to` (issue_date, YYYY-MM-DD), `search` (number, description, supplier
        naam). `ordering` keys: issue_date, supplier, category, net, vat, claimable_vat, total ('-' =
        descending; default '-issue_date'). Paginated. Har row mein flags aur why_not_claimed."""
        return await client.get(
            "expenses/",
            {
                "voided": voided,
                "supplier": supplier,
                "category": category,
                "capital_asset": capital_asset,
                "claim": claim,
                "from": date_from,
                "to": date_to,
                "search": search,
                "ordering": ordering,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def expense_get(expense_id: int) -> Any:
        """Ek expense ki poori detail (owner/manager): supplier snapshot (supplier_vat_registered,
        supplier_vat_number, supplier_crn), claimable_vat, claim_date, claim_period, issue_period, qr_check,
        flags (supplier-document wale VAT flags + notClaimable, noAttachment, possibleDuplicate), why_not_claimed
        (voided | notRegistered | supplierVatMissing | notClaimable | None), voided details, created_by_name."""
        return await client.get(f"expenses/{expense_id}/")

    @mcp.tool()
    async def expense_create(
        supplier: int,
        number: str,
        issue_date: str,
        category: ExpenseCategory,
        net: float,
        vat: float,
        total: float,
        kind: DocKind = "invoice",
        description: str | None = None,
        vat_category: VatCategory | None = None,
        not_claimable: bool | None = None,
        not_claimable_reason: NotClaimableReason | None = None,
        not_claimable_note: str | None = None,
        capital_asset: bool | None = None,
        qr_text: str | None = None,
        attachment_url: str | None = None,
        attachment_name: str | None = None,
        keep_in_filed_period: bool | None = None,
        lock_override_reason: str | None = None,
    ) -> Any:
        """Naya business expense (owner/manager): supplier ki tax invoice/credit note jo PO ke bahar hai, paper,
        PDF ya photo se keyed. Same VAT rules supplier documents ke: supplier VAT-registered na ho to vat 0
        (error.supplierNotVatRegistered); claim tab jab supplier ka VAT number ho; QR har save par check; credit
        note par claimable negative. `category='other'` ho to `description` zaroori
        (error.expenseDescriptionRequired). `not_claimable=True` (blocked input VAT) ho to
        `not_claimable_reason` zaroori, aur reason 'other' ho to `not_claimable_note` bhi; VAT record rehti hai
        lekin claim 0. `capital_asset` na do to category 'equipment' par True. `attachment_url` https:// hona
        chahiye (error.attachmentUrlInvalid). Errors: error.expenseSupplierRequired,
        error.expenseCategoryRequired, error.supplierInvoiceNumberRequired, error.dateInvalid,
        error.vatCategoryNeedsZeroVat. Filed VAT period mein ho to 409 error.vatPeriodLocked jab tak OWNER
        `lock_override_reason` na de."""
        body = drop_none(
            {
                "supplier": supplier,
                "number": number,
                "issue_date": issue_date,
                "category": category,
                "kind": kind,
                "description": description,
                "net": _money(net),
                "vat": _money(vat),
                "total": _money(total),
                "vat_category": vat_category,
                "not_claimable": not_claimable,
                "not_claimable_reason": not_claimable_reason,
                "not_claimable_note": not_claimable_note,
                "capital_asset": capital_asset,
                "qr_text": qr_text,
                "attachment_url": attachment_url,
                "attachment_name": attachment_name,
                "keep_in_filed_period": keep_in_filed_period,
                "lock_override_reason": lock_override_reason,
            }
        )
        return await client.post("expenses/", body)

    @mcp.tool()
    async def expense_update(
        expense_id: int,
        supplier: int | None = None,
        number: str | None = None,
        issue_date: str | None = None,
        category: ExpenseCategory | None = None,
        net: float | None = None,
        vat: float | None = None,
        total: float | None = None,
        kind: DocKind | None = None,
        description: str | None = None,
        vat_category: VatCategory | None = None,
        not_claimable: bool | None = None,
        not_claimable_reason: NotClaimableReason | None = None,
        not_claimable_note: str | None = None,
        capital_asset: bool | None = None,
        qr_text: str | None = None,
        attachment_url: str | None = None,
        attachment_name: str | None = None,
        keep_in_filed_period: bool | None = None,
        lock_override_reason: str | None = None,
    ) -> Any:
        """Expense PATCH (owner/manager); sirf diye gaye fields badalte hain. Voided expense par 409
        error.expenseVoided. Validation create jaisi; `not_claimable=False` dene se reason/note khud saaf. VAT 0
        se badh kar > 0 ho aur category na di ho to vat_category 'standard' ban jati hai. Date na badle to claim
        date (aur period) wahi rehti hai. Filed VAT period ka expense 409 error.vatPeriodLocked jab tak OWNER
        `lock_override_reason` na de. Expense DELETE nahi hota: ghalat ho to expense_void."""
        body = drop_none(
            {
                "supplier": supplier,
                "number": number,
                "issue_date": issue_date,
                "category": category,
                "kind": kind,
                "description": description,
                "net": _money(net),
                "vat": _money(vat),
                "total": _money(total),
                "vat_category": vat_category,
                "not_claimable": not_claimable,
                "not_claimable_reason": not_claimable_reason,
                "not_claimable_note": not_claimable_note,
                "capital_asset": capital_asset,
                "qr_text": qr_text,
                "attachment_url": attachment_url,
                "attachment_name": attachment_name,
                "keep_in_filed_period": keep_in_filed_period,
                "lock_override_reason": lock_override_reason,
            }
        )
        return await client.patch(f"expenses/{expense_id}/", body)

    @mcp.tool()
    async def expense_void(expense_id: int, reason: str, lock_override_reason: str | None = None) -> Any:
        """Ghalat expense VOID karna (owner/manager) - delete ka rasta nahi, expense list mein rehta hai
        (struck-through) lekin kahin gina nahi jata (claimable_vat 0) aur VAT audit log mein 'expense_voided'
        darj hota hai. `reason` kam az kam 10 characters (400 error.vatVoidReasonShort, field `reason`); pehle se
        voided par 409 error.vatAlreadyVoided. Filed VAT period ka expense 409 error.vatPeriodLocked jab tak OWNER
        `lock_override_reason` na de. Updated expense wapas aata hai."""
        body = drop_none({"reason": reason, "lock_override_reason": lock_override_reason})
        return await client.post(f"expenses/{expense_id}/void/", body)
