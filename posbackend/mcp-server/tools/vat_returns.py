"""
vat_returns app ke MCP tools: VAT return periods, filing, payments/refunds, history, aur invoice archive.

Backend routes (vat_returns/urls.py -> /api/vat/...). Sab owner/manager ke liye hain; jin par (owner) likha hai
unhein sirf OWNER chala sakta hai (baqi ko 403).

  Settings / periods
    GET    vat/settings/                          vat_settings_get
    PATCH  vat/settings/                          vat_settings_update        (owner)
    GET    vat/periods/                           vat_periods_list
    GET    vat/periods/<id>/                      vat_period_get
    GET    vat/periods/<id>/documents/            vat_period_documents       (card ke peeche ke documents)
    GET    vat/periods/<id>/audit/                vat_period_audit
    GET    vat/periods/<id>/pdf/                  vat_period_pdf_download    (PDF disk par)
    GET    vat/reminders/                         vat_reminders_get
  Filing
    POST   vat/periods/<id>/file/                 vat_period_file            (owner)
    POST   vat/periods/<id>/amend/                vat_period_amend           (owner)
    POST   vat/periods/<id>/adjustment/           vat_period_adjustment_set  (owner)
  Payments / refunds
    POST   vat/periods/<id>/payments/             vat_payment_record         (owner)
    POST   vat/periods/<id>/refunds/              vat_refund_record          (owner)
    POST   vat/payments/<id>/void/                vat_payment_void           (owner)
    POST   vat/refunds/<id>/void/                 vat_refund_void            (owner)
  History
    GET    vat/history/                           vat_history_get
    GET    vat/history/csv/                       vat_history_csv_download   (CSV disk par)
    GET    vat/history/pdf/                       vat_history_pdf_download   (PDF disk par)
  Invoice archive (read-only)
    GET    vat/archive/<sales|purchases|expenses>/   vat_archive_list
    GET    vat/archive/sales/<id>/pdf/            vat_archive_sale_pdf_download
    GET    vat/archive/purchases/<id>/xml/        vat_archive_purchase_xml_download
    GET    vat/archive/exports/                   vat_archive_exports_list
    POST   vat/archive/exports/                   vat_archive_export_request
    GET    vat/archive/exports/<id>/              vat_archive_export_get
    GET    vat/archive/exports/<id>/download/     vat_archive_export_download (ZIP disk par)

VAT ka basic nizam:
  * Branch ki current ZATCA environment (Sandbox/Simulation/Production) ke hisaab se periods (monthly ya
    quarterly) khud bante hain; Production mein test periods nahi dikhte. Period ke "cards" aur return "boxes"
    backend nikalta hai (figures.py): output VAT (sales), input VAT (supplier documents + expenses), net due.
  * "Filing" = owner ne ZATCA ke portal par return bhar diya; yahan sirf record hota hai (date, ZATCA ki
    amount, farq ki wajah). Filing period ko LOCK kar deti hai: us period mein supplier document/expense
    banana, badalna, hataana ya void karna 409 error.vatPeriodLocked deta hai jab tak OWNER
    `lock_override_reason` (kam az kam 10 chars) na de (purchasing tools mein ye parameter hai).
  * Period status: open (Not filed) -> filed -> paid, ya credit (ZATCA ki amount <= 0).
  * Payments (SADAD) aur refunds kabhi delete nahi hote, sirf void (wajah ke saath, struck-through rehte hain);
    har action VAT audit log mein darj hota hai.
  * Amounts SAR mein, 2 decimals tak; credit notes negative.

Binary files (PDF/ZIP/CSV/XML) chat mein nahi aati: tool unhein disk par save karke {path, bytes} deta hai
(`save_dir` na do to temp folder ke 'pos-mcp' mein).
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none
from tools._utils import save_bytes

CreditTreatment = Literal["carry_forward", "refund_requested"]
ArchiveSide = Literal["sales", "purchases", "expenses"]
DocKind = Literal["invoice", "credit_note", "debit_note"]
ExpenseCategory = Literal["rent", "utilities", "equipment", "maintenance", "services", "marketing", "other"]
Day = str  # 'YYYY-MM-DD'


def _amount(value: float | None) -> float | None:
    """SAR amount, 2 decimals (backend parse_amount sirf <= 2 decimals leta hai)."""
    return round(value, 2) if value is not None else None


def _archive_filters(**kw: Any) -> dict[str, Any]:
    return drop_none(kw)


def register(mcp, client: POSClient) -> None:
    # ===================================================== settings / periods
    @mcp.tool()
    async def vat_settings_get() -> Any:
        """Branch ki VAT filing settings (owner/manager). Response: frequency, current_frequency (latest period ki),
        frequency_effective_from (scheduled switch), periods_filed (koi period filed hai?), earliest_switch,
        registration_date, tracking_start (is tareekh se returns track), reminder_days, first_period_start,
        environment, is_test."""
        return await client.get("vat/settings/")

    @mcp.tool()
    async def vat_settings_update(
        frequency: Literal["monthly", "quarterly"] | None = None,
        registration_date: Day | None = None,
        clear_registration_date: bool = False,
        tracking_start: Day | None = None,
        clear_tracking_start: bool = False,
        frequency_effective_from: Day | None = None,
        reminder_days: int | None = None,
    ) -> Any:
        """VAT settings PATCH (OWNER only); sirf diye gaye fields badalte hain, har change audit log mein.
        `frequency` 'monthly' ya 'quarterly' (ghalat par error.vatFrequencyInvalid). `registration_date` / `tracking_start` ('YYYY-MM-DD'; tracking_start aaj se
        aage nahi: error.vatTrackingStartInFuture); inhein khali karne ke liye `clear_*=True`.
        `reminder_days` 1-60 (error.vatReminderDaysInvalid).
        Dates ya frequency badalne se periods dobara bante hain, lekin jab koi period FILED ho to 409
        error.vatPeriodsFiled. Filed ke baad frequency badalna `frequency_effective_from` maangta hai: ek
        quarter ki shuruaat jo `earliest_switch` ya usse baad ho (error.vatSwitchDateRequired,
        error.vatSwitchNotQuarterStart, error.vatSwitchTooEarly); purani frequency par wapas jane se scheduled
        switch cancel. Result: updated settings."""
        body = drop_none(
            {
                "frequency": frequency,
                "registration_date": registration_date,
                "tracking_start": tracking_start,
                "frequency_effective_from": frequency_effective_from,
                "reminder_days": reminder_days,
            }
        )
        # null = clear (drop_none se nahi bhej sakte), is liye explicit.
        if clear_registration_date:
            body["registration_date"] = None
        if clear_tracking_start:
            body["tracking_start"] = None
        return await client.patch("vat/settings/", body)

    @mcp.tool()
    async def vat_periods_list() -> Any:
        """Current environment ke saare VAT periods, naye pehle (owner/manager). Response: settings (vat_settings_get
        jaisi), setup_needed (registration/tracking date set nahi), today, current (current period ki id),
        periods -> [{id, environment, is_test, frequency, label, start_date, end_date, due_date, days_left
        (due ke baad negative), has_ended, is_current, status (open|filed|paid|credit), locked,
        opening_credit, opening_credit_reason, correction, correction_reason}]."""
        return await client.get("vat/periods/")

    @mcp.tool()
    async def vat_period_get(period_id: int) -> Any:
        """Ek VAT period ki poori detail (owner/manager): period fields (vat_periods_list jaise) plus
        is_first, figures {cards, boxes (VAT return ke boxes), not_counted, refund, missing_breakdown,
        changed_since_filing (filing ke baad kya badla)}, checklist [{key, ok, count}] (filing se pehle ki jaanch:
        pendingDocuments, rejectedDocuments, purchasesWithoutInvoice, supplierDocumentsFlagged,
        expensesWithoutAttachment, missingBreakdown, previousNotFiled, periodNotEnded), filing (current filing
        ya null), filings (sab, superseded bhi), payments, refunds. 404 agar period is branch ka ya current
        environment ka na ho."""
        return await client.get(f"vat/periods/{period_id}/")

    @mcp.tool()
    async def vat_period_documents(
        period_id: int,
        which: Literal["output", "not_counted", "input", "expenses", "review", "not_claimable"],
        page: int | None = None,
    ) -> Any:
        """Period ke kisi card ke peeche ke documents (owner/manager): `which` = 'output' (sales ki VAT),
        'not_counted' (jo sales abhi gini nahi gayi: pending/rejected), 'input' (supplier documents),
        'expenses', 'review' (VAT roki hui: supplier VAT-registered nahi ya VAT number nahi),
        'not_claimable'. 100 rows per page (`page`), poori list ke totals ke saath; amounts signed (credit notes
        negative) taake rows card ke barabar jama hon. Ghalat `which` par 400 error.vatListInvalid."""
        return await client.get(f"vat/periods/{period_id}/documents/", {"list": which, "page": page})

    @mcp.tool()
    async def vat_period_audit(period_id: int) -> Any:
        """Period ki activity (owner/manager), naye pehle, max 200: filing, amendment, lock overrides,
        payments, voids, exports. Rows: {id, action, summary, data, actor, created_at}."""
        return await client.get(f"vat/periods/{period_id}/audit/")

    @mcp.tool()
    async def vat_period_pdf_download(period_id: int, save_dir: str | None = None) -> Any:
        """Ek period ka PDF (owner/manager): cards, return boxes, filing, payments. Disk par save hota hai:
        {path, bytes}. Ye export VAT audit log mein 'period_pdf_exported' darj karta hai."""
        content = await client.get_bytes(f"vat/periods/{period_id}/pdf/")
        return save_bytes(content, f"vat-return-period-{period_id}.pdf", save_dir)

    @mcp.tool()
    async def vat_reminders_get() -> Any:
        """Kin periods ki filing/payment due hai ya late (owner/manager); sirf Production mein (test environment
        mein khali). Rows urgent pehle: {id, label, name, due_date, days_left, level, status, balance}.
        Jo periods reminder_days ke andar due hon ya overdue, aur filed nahi ya poori paid nahi."""
        return await client.get("vat/reminders/")

    # ===================================================== filing (OWNER)
    @mcp.tool()
    async def vat_period_file(
        period_id: int,
        filed_on: Day,
        zatca_amount: float,
        difference_reason: str | None = None,
        credit_treatment: CreditTreatment | None = None,
        attachment_url: str | None = None,
        attachment_name: str | None = None,
        confirm_warnings: bool = False,
    ) -> Any:
        """Period ko FILED mark karna (OWNER only): jo owner ne ZATCA portal par bhara uska record. IRREVERSIBLE
        asar: figures ka snapshot save hota hai aur period LOCK ho jata hai (is mein supplier document/expense
        badalne ke liye owner override chahiye). `filed_on` period ki shuruaat se pehle aur aaj se aage nahi
        (error.vatFiledOnBeforePeriod / error.vatFiledOnInFuture); `zatca_amount` wo net VAT jo ZATCA ne
        confirm kiya (signed, 2 decimals; <= 0 ka matlab credit, status 'credit'). Agar ye hamari calculated
        figure se alag ho to `difference_reason` zaroori (error.vatDifferenceReasonRequired). Credit par
        `credit_treatment` ('carry_forward' default | 'refund_requested'). `attachment_url` https:// hona
        chahiye. Checklist mein koi warning khuli ho to 409 error.vatChecklistOpen (response mein warnings ki
        list); sab dekh kar `confirm_warnings=True` se dobara. Pehle se filed par 409 error.vatPeriodAlreadyFiled.
        Pehle vat_period_get se checklist aur figures dekhein. Result: updated period (HTTP 201)."""
        body = drop_none(
            {
                "filed_on": filed_on,
                "zatca_amount": _amount(zatca_amount),
                "difference_reason": difference_reason,
                "credit_treatment": credit_treatment,
                "attachment_url": attachment_url,
                "attachment_name": attachment_name,
                "confirm_warnings": True if confirm_warnings else None,
            }
        )
        return await client.post(f"vat/periods/{period_id}/file/", body)

    @mcp.tool()
    async def vat_period_amend(
        period_id: int,
        filed_on: Day,
        zatca_amount: float,
        amendment_reason: str,
        difference_reason: str | None = None,
        credit_treatment: CreditTreatment | None = None,
        attachment_url: str | None = None,
        attachment_name: str | None = None,
        confirm_warnings: bool = False,
    ) -> Any:
        """AMENDED return record karna (OWNER only): portal par dobara bhara gaya return. Nayi filing purani
        (current) ko supersede karti hai; purani list mein rehti hai; payments/refunds period ke saath rehte
        hain. Fields vat_period_file jaise plus `amendment_reason` (kam az kam 10 chars,
        error.vatAmendmentReasonShort); `filed_on` pichli filing se pehle nahi (error.vatAmendedBeforeFiling).
        Period filed na ho to 409 error.vatPeriodNotFiled. Result: updated period (201)."""
        body = drop_none(
            {
                "filed_on": filed_on,
                "zatca_amount": _amount(zatca_amount),
                "amendment_reason": amendment_reason,
                "difference_reason": difference_reason,
                "credit_treatment": credit_treatment,
                "attachment_url": attachment_url,
                "attachment_name": attachment_name,
                "confirm_warnings": True if confirm_warnings else None,
            }
        )
        return await client.post(f"vat/periods/{period_id}/amend/", body)

    @mcp.tool()
    async def vat_period_adjustment_set(
        period_id: int,
        kind: Literal["opening_credit", "correction"],
        amount: float,
        reason: str,
    ) -> Any:
        """Period par owner ki adjustment (OWNER only), filing se pehle: 'opening_credit' = is system se pehle ka
        VAT credit, sirf PEHLE period par aur negative nahi (error.vatOpeningCreditFirstOnly,
        error.vatOpeningCreditNegative); 'correction' = box 14, pichle periods ki corrections, SAR +-5,000
        tak (zyada par ZATCA ko voluntary disclosure chahiye: error.vatCorrectionLimit); positive VAT due badhata,
        negative kam karta hai. `reason` kam az kam 10 chars (error.vatAdjustmentReasonShort). `amount=0` se
        adjustment hat jati hai (wajah phir bhi likhein). Filed period par 409 error.vatAdjustmentPeriodFiled.
        Audit log mein darj. Result: updated period."""
        return await client.post(
            f"vat/periods/{period_id}/adjustment/", {"kind": kind, "amount": _amount(amount), "reason": reason}
        )

    # ===================================================== payments / refunds (OWNER)
    @mcp.tool()
    async def vat_payment_record(
        period_id: int,
        amount: float,
        paid_on: Day,
        sadad_reference: str,
        receipt_url: str | None = None,
        receipt_name: str | None = None,
    ) -> Any:
        """SADAD payment record karna (OWNER only) - sirf us period ke liye jo FILED ho aur ZATCA amount > 0
        ho (error.vatPeriodNotFiled, error.vatNothingToPay). `amount` > 0 (error.vatPositiveAmount), `paid_on`
        period ki shuruaat se pehle ya aaj se aage nahi (error.vatDateTooEarly / error.vatDateInFuture),
        `sadad_reference` zaroori (<=64 chars, error.vatReferenceRequired), `receipt_url` https://. Zyada ada
        karna allowed hai (balance overpaid dikhta hai): jo bank ne kiya wahi likhein. Payments poori hon to
        period 'paid' ho jata hai. Result: updated period (201)."""
        body = drop_none(
            {
                "amount": _amount(amount),
                "paid_on": paid_on,
                "sadad_reference": sadad_reference,
                "receipt_url": receipt_url,
                "receipt_name": receipt_name,
            }
        )
        return await client.post(f"vat/periods/{period_id}/payments/", body)

    @mcp.tool()
    async def vat_refund_record(
        period_id: int,
        amount: float,
        received_on: Day,
        reference: str,
        attachment_url: str | None = None,
        attachment_name: str | None = None,
    ) -> Any:
        """ZATCA ka refund record karna (OWNER only) - sirf us period par jo credit (ZATCA amount < 0) ke saath
        filed ho aur credit_treatment 'refund_requested' ho (warna 409 error.vatNoRefundRequested). `amount` > 0,
        `received_on` filing ki date se pehle ya aaj se aage nahi, `reference` zaroori (<=64 chars).
        Result: updated period (201)."""
        body = drop_none(
            {
                "amount": _amount(amount),
                "received_on": received_on,
                "reference": reference,
                "attachment_url": attachment_url,
                "attachment_name": attachment_name,
            }
        )
        return await client.post(f"vat/periods/{period_id}/refunds/", body)

    @mcp.tool()
    async def vat_payment_void(payment_id: int, reason: str) -> Any:
        """Payment VOID karna (OWNER only): delete nahi, list mein struck-through rehti hai aur gini nahi jati;
        period 'paid' se wapas 'filed' ho sakta hai. `payment_id` vat_period_get ke payments se. `reason` kam az
        kam 10 chars (400 error.vatVoidReasonShort); pehle se void par 409 error.vatAlreadyVoided.
        Result: updated period."""
        return await client.post(f"vat/payments/{payment_id}/void/", {"reason": reason})

    @mcp.tool()
    async def vat_refund_void(refund_id: int, reason: str) -> Any:
        """Refund VOID karna (OWNER only): payment void jaisa (kam az kam 10 chars wajah, 409
        error.vatAlreadyVoided). Result: updated period."""
        return await client.post(f"vat/refunds/{refund_id}/void/", {"reason": reason})

    # ===================================================== history
    @mcp.tool()
    async def vat_history_get() -> Any:
        """Har period ki ek row, naye pehle, wahi figures jo period ke cards mein hain (owner/manager).
        Response: {settings, rows}. Accountant ya trend ke liye."""
        return await client.get("vat/history/")

    @mcp.tool()
    async def vat_history_csv_download(save_dir: str | None = None) -> Any:
        """History CSV (UTF-8 BOM) accountant ke liye, disk par: {path, bytes} (owner/manager). VAT audit log mein
        'history_exported' darj hota hai."""
        content = await client.get_bytes("vat/history/csv/")
        return save_bytes(content, "vat-history.csv", save_dir)

    @mcp.tool()
    async def vat_history_pdf_download(save_dir: str | None = None) -> Any:
        """History ek table wale PDF mein, disk par: {path, bytes} (owner/manager). Audit log mein darj."""
        content = await client.get_bytes("vat/history/pdf/")
        return save_bytes(content, "vat-history.pdf", save_dir)

    # ===================================================== invoice archive (read-only)
    @mcp.tool()
    async def vat_archive_list(
        side: ArchiveSide,
        date_from: Day | None = None,
        date_to: Day | None = None,
        period: int | None = None,
        kind: DocKind | None = None,
        status: str | None = None,
        channel: Literal["b2c", "b2b"] | None = None,
        customer: int | None = None,
        supplier: int | None = None,
        number: str | None = None,
        amount_min: float | None = None,
        amount_max: float | None = None,
        q: str | None = None,
        category: ExpenseCategory | None = None,
        capital_asset: bool | None = None,
        page: int | None = None,
    ) -> Any:
        """Invoice archive (read-only, owner/manager): `side` 'sales' (POS ke ZATCA documents: invoices, credit/debit
        notes, B2C/B2B; self-tests nahi), 'purchases' (non-deleted POs ke supplier documents), 'expenses'
        (non-voided business expenses). 50 rows per page (`page`), count aur poori filtered list ke signed
        totals ke saath (credit notes negative). Filters (jo side par lagu nahi wo ignore): `date_from`/`date_to`,
        `period` (VAT period id: wahi jo VAT return count karta hai), `kind` (sales: invoice|credit_note|
        debit_note; purchases/expenses: invoice|credit_note), `status` (sales ZATCA document status),
        `channel` (sales), `customer` (sales id), `supplier` (purchases/expenses id), `number`,
        `amount_min`/`amount_max` (total par), `q` (free text), `category` + `capital_asset` (expenses).
        Ghalat filter value par 400 jis mein har galat key ka error: error.dateInvalid, error.archiveDateRange,
        error.archivePeriodInvalid, error.archiveKindInvalid, error.archiveStatusInvalid,
        error.archiveChannelInvalid, error.archiveIdInvalid, error.archiveCategoryInvalid,
        error.archiveCapitalAssetInvalid, error.vatAmountInvalid."""
        return await client.get(
            f"vat/archive/{side}/",
            _archive_filters(
                date_from=date_from, date_to=date_to, period=period, kind=kind, status=status, channel=channel,
                customer=customer, supplier=supplier, number=number, amount_min=amount_min,
                amount_max=amount_max, q=q, category=category, capital_asset=capital_asset, page=page,
            ),
        )

    @mcp.tool()
    async def vat_archive_sale_pdf_download(document_id: int, save_dir: str | None = None) -> Any:
        """Sales document ka A4 tax invoice PDF (owner/manager), signing ke waqt ke frozen snapshot se; disk par:
        {path, bytes}. `document_id` vat_archive_list(side='sales') ke row ki id. Arabic font/libraries server par
        na hon to 503 error.archivePdfUnavailable."""
        content = await client.get_bytes(f"vat/archive/sales/{document_id}/pdf/")
        return save_bytes(content, f"sale-{document_id}.pdf", save_dir)

    @mcp.tool()
    async def vat_archive_purchase_xml_download(document_id: int, save_dir: str | None = None) -> Any:
        """Supplier document ka XML jaisa key kiya gaya tha (owner/manager), disk par: {path, bytes}.
        XML na ho to 404. Iska PDF stored `pdf_url` hai jo seedha kholte hain."""
        content = await client.get_bytes(f"vat/archive/purchases/{document_id}/xml/")
        return save_bytes(content, f"supplier-document-{document_id}.xml", save_dir)

    @mcp.tool()
    async def vat_archive_exports_list() -> Any:
        """Branch ke latest 20 ZIP exports, naye pehle (owner/manager): {exports: [{id, status
        (queued|running|ready|failed|expired), sides, filters, period, description, sales_count, purchases_count,
        expenses_count, missing_count, file_name, size, error, requested_by, created_at, finished_at,
        expires_at}]}."""
        return await client.get("vat/archive/exports/")

    @mcp.tool()
    async def vat_archive_export_request(
        sides: list[ArchiveSide] | None = None,
        date_from: Day | None = None,
        date_to: Day | None = None,
        period: int | None = None,
        kind: DocKind | None = None,
        status: str | None = None,
        channel: Literal["b2c", "b2b"] | None = None,
        customer: int | None = None,
        supplier: int | None = None,
        number: str | None = None,
        amount_min: float | None = None,
        amount_max: float | None = None,
        q: str | None = None,
        category: ExpenseCategory | None = None,
        capital_asset: bool | None = None,
    ) -> Any:
        """Documents ka ZIP export mangwana (owner/manager): `sides` na do to teeno (sales, purchases, expenses);
        filters vat_archive_list jaise (har side par apne liye lagu hote hain). ZIP BACKGROUND mein banta hai:
        response foran 'queued' (HTTP 201) hota hai; phir vat_archive_export_get se status poll karein jab tak
        'ready', phir vat_archive_export_download. Audit log mein 'archive_requested'. Errors:
        error.archiveSidesInvalid aur filter errors (vat_archive_list jaise)."""
        body = _archive_filters(
            sides=sides, date_from=date_from, date_to=date_to, period=period, kind=kind, status=status,
            channel=channel, customer=customer, supplier=supplier, number=number, amount_min=amount_min,
            amount_max=amount_max, q=q, category=category, capital_asset=capital_asset,
        )
        return await client.post("vat/archive/exports/", body)

    @mcp.tool()
    async def vat_archive_export_get(export_id: int) -> Any:
        """Ek ZIP export ka status (owner/manager): poll karne ke liye. status queued -> running -> ready
        (ya failed / expired); ready par expires_at (retention ke baad hat jata hai)."""
        return await client.get(f"vat/archive/exports/{export_id}/")

    @mcp.tool()
    async def vat_archive_export_download(export_id: int, save_dir: str | None = None) -> Any:
        """Tayyar ZIP download karke disk par save (owner/manager): {path, bytes}. Taiyar na ho (queued/running/
        failed/expired) to 409 error.archiveExportNotReady (response mein status). Har download audit log
        mein 'archive_downloaded' darj hota hai."""
        content = await client.get_bytes(f"vat/archive/exports/{export_id}/download/")
        return save_bytes(content, f"invoice-archive-{export_id}.zip", save_dir)
