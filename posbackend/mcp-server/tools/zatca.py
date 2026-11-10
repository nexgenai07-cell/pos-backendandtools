"""
zatca app ke MCP tools: Saudi e-invoicing (ZATCA/Fatoora): seller settings, devices, go-live wizard,
documents, receipts, B2B buyers.

Backend routes (zatca/urls.py -> /api/zatca/...). Role ka matlab: (owner) = sirf OWNER, (owner/manager),
(cashier) = cashier/owner/manager, (public) = login nahi.

  Settings / devices
    GET   zatca/settings/                         zatca_settings_get            (owner/manager)
    PATCH zatca/settings/                         zatca_settings_update         (owner)
    GET   zatca/devices/                          zatca_devices_get             (owner/manager)
    POST  zatca/devices/<id>/compliance-checks/   zatca_device_recheck          (owner)
  Go-live wizard (owner acts, manager watches)
    GET    zatca/go-live/                         zatca_go_live_get
    POST   zatca/go-live/                         zatca_go_live_start
    PATCH  zatca/go-live/                         zatca_go_live_update_details
    DELETE zatca/go-live/                         zatca_go_live_cancel
    POST   zatca/go-live/keys/                    zatca_go_live_generate_keys
    POST   zatca/go-live/compliance-csid/         zatca_go_live_compliance_csid
    POST   zatca/go-live/checks/                  zatca_go_live_run_checks
    POST   zatca/go-live/production-csid/         zatca_go_live_production_csid
    POST   zatca/go-live/finish/                  zatca_go_live_finish
  Documents (invoices / credit & debit notes)
    GET   zatca/documents/                        zatca_documents_list          (owner/manager)
    POST  zatca/documents/<id>/retry/             zatca_document_retry          (owner/manager)
    GET   zatca/documents/<id>/xml/               zatca_document_xml_download    (owner/manager)
    POST  zatca/documents/<id>/debit-note/        zatca_debit_note_create       (owner)
  Till / receipts
    GET   zatca/pos-status/                       zatca_pos_status              (cashier)
    GET   zatca/receipts/                         zatca_receipts_get            (cashier)
    POST  zatca/receipts/<id>/printed/            zatca_receipt_mark_printed    (cashier)
    GET   zatca/public/receipts/<uuid>/           zatca_public_receipt_get      (public)
  B2B buyers (standard/company invoices)
    GET/POST zatca/business-customers/            zatca_business_customers_search / _save   (cashier)
    GET/POST zatca/orders/<id>/buyer/             zatca_order_buyer_get / _set              (cashier)
    GET/POST zatca/bills/<id>/buyer/              zatca_bill_buyer_get / _set               (cashier)

ZATCA ka basic nizam:
  * Teen environments: sandbox (practice) -> simulation -> production. Sirf production ke invoices asli aur
    qanooni hote hain (seller ke VAT number par ZATCA ko report). Environment sirf go-live wizard se badalta hai
    (settings PATCH se nahi); wizard har dafa NAYI device banata hai (apni ICV chain ke saath) aur purani device
    `finish` tak chalti rehti hai.
  * Device = seller identity se bandha hua certificate (CSID). Private keys/secrets kabhi API mein nahi aate:
    sirf "set hai ya nahi" aur kab.
  * Document status: pending (ZATCA ko bhejna baqi) -> reported (simplified B2C) / cleared (standard B2B) ya
    rejected. Pending documents retry queue se khud jate hain; `zatca_document_retry` foran bhejta hai.
  * Invoice types: '1000' sirf standard (B2B), '0100' sirf simplified (B2C), '1100' dono.
  * Seller VAT number 15 digits, '3' se shuru aur '3' par khatam; building_no 4 digits, additional_no 4 digits,
    postal_code 5 digits. VAT-group ke VAT number par org_unit 10-digit TIN honi chahiye.
  * Errors codes mein aate hain; field errors {"field": "error.code"} ya {"error": code, "fields": {...}}.
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none
from tools._utils import save_bytes

InvoiceTypes = Literal["1000", "0100", "1100"]
SellerIdScheme = Literal["CRN", "MOM", "MLS", "SAG", "700", "OTH"]
DocStatus = Literal["pending", "reported", "cleared", "rejected"]
GoLiveEnvironment = Literal["sandbox", "simulation", "production"]


def register(mcp, client: POSClient) -> None:
    # ============================================================ settings
    @mcp.tool()
    async def zatca_settings_get() -> Any:
        """Branch ki ZATCA seller details (owner/manager) - jo har tax invoice mein seller ke baare mein likha
        hota hai. Pehli GET sandbox test identity ke saath row bana deti hai. Response: environment, is_test, is_live,
        enabled, seller_name_en/ar, vat_number, other_id, other_id_scheme, street, building_no, additional_no,
        district, city, postal_code, country, org_unit, business_category, invoice_types, default_vat_rate,
        prices_include_vat, receipt_footer_en/ar, locked_fields (jo fields ab badal nahi sakte: identity jab device
        ban chuki), is_vat_group, enabled_at, updated_at."""
        return await client.get("zatca/settings/")

    @mcp.tool()
    async def zatca_settings_update(
        enabled: bool | None = None,
        seller_name_en: str | None = None,
        seller_name_ar: str | None = None,
        vat_number: str | None = None,
        other_id: str | None = None,
        other_id_scheme: SellerIdScheme | None = None,
        street: str | None = None,
        building_no: str | None = None,
        additional_no: str | None = None,
        district: str | None = None,
        city: str | None = None,
        postal_code: str | None = None,
        org_unit: str | None = None,
        business_category: str | None = None,
        invoice_types: InvoiceTypes | None = None,
        default_vat_rate: float | None = None,
        prices_include_vat: bool | None = None,
        receipt_footer_en: str | None = None,
        receipt_footer_ar: str | None = None,
    ) -> Any:
        """ZATCA seller details PATCH (OWNER only); sirf diye gaye fields badalte hain. Ye har invoice par seller ki
        pehchan hai. `enabled=True` ZATCA invoicing shuru karta hai (us waqt se bani sales invoice hoti hain; pehle
        ki nahi). Identity fields (vat_number, other_id, other_id_scheme) device ban jane ke baad LOCK hain
        (error.zatcaFieldLocked): unhein badalne ka wahid tareeqa go-live wizard hai. Validation errors field ke
        saath: error.zatcaFieldRequired, error.zatcaFieldTooLong, error.zatcaChoiceInvalid,
        error.zatcaVatRateInvalid (0-100, 2 decimals), VAT/building/additional/postal/other-id format errors,
        error.zatcaVatGroupOrgUnit (VAT group par org_unit 10-digit TIN). Updated settings wapas."""
        body = drop_none(
            {
                "enabled": enabled,
                "seller_name_en": seller_name_en,
                "seller_name_ar": seller_name_ar,
                "vat_number": vat_number,
                "other_id": other_id,
                "other_id_scheme": other_id_scheme,
                "street": street,
                "building_no": building_no,
                "additional_no": additional_no,
                "district": district,
                "city": city,
                "postal_code": postal_code,
                "org_unit": org_unit,
                "business_category": business_category,
                "invoice_types": invoice_types,
                "default_vat_rate": round(default_vat_rate, 2) if default_vat_rate is not None else None,
                "prices_include_vat": prices_include_vat,
                "receipt_footer_en": receipt_footer_en,
                "receipt_footer_ar": receipt_footer_ar,
            }
        )
        return await client.patch("zatca/settings/", body)

    # ============================================================ devices
    @mcp.tool()
    async def zatca_devices_get() -> Any:
        """Branch ki ZATCA devices (owner/manager), active pehle phir archived; secrets se paak. Response:
        environment, encryption {configured, problem} (ZATCA_ENCRYPTION_KEY kaam karti hai ya nahi, uski value
        nahi), devices -> [{id, environment, is_active, status (not_started|keys_ready|compliance_csid|
        compliance_passed|ready|active|needs_attention), egs_serial, common_name, location, invoice_types, last_icv,
        last_hash, credentials [{name, set, set_at}], certificates {compliance, production} (subject, issuer,
        not_before, not_after), signing_certificate, warnings [{code: certExpired | certExpiringSoon (30 din) |
        certUnreadable | vatMismatch | environmentMismatch, ...}], created_at, archived_at}]."""
        return await client.get("zatca/devices/")

    @mcp.tool()
    async def zatca_device_recheck(device_id: int) -> Any:
        """Chalti (live) device ke compliance checks dobara chalana (OWNER only), apne compliance CSID se (OTP
        nahi chahiye); device mein kuch nahi badalta. Result: {device, checks [{label, kind, simplified, passed,
        http_status, status, warnings, errors}], passed}. ZATCA ke server ko test documents jate hain. Fail par 409
        error.zatcaStepFailed (detail mein ZATCA ka message)."""
        return await client.post(f"zatca/devices/{device_id}/compliance-checks/")

    # ============================================================ go-live wizard
    @mcp.tool()
    async def zatca_go_live_get() -> Any:
        """Go-live wizard ki halat (owner/manager). Response: environment, is_test, active_device, back_to_sandbox_block
        (error code agar sandbox par wapas jana mana hai, warna null), current (khula wizard ya null) aur last
        (aakhri khatam hua). Wizard payload: {id, environment, practice (sandbox run), status, step (details | otp |
        checks | production | activate | done), details, fixed_fields, device, checks, requires_real_identity,
        needs_legal_confirmation, legal_warning, legal_confirmed_at, default_otp (sirf sandbox ka public OTP),
        activated, created_at, finished_at, documents (nayi device ke aakhri 10 test documents)}."""
        return await client.get("zatca/go-live/")

    @mcp.tool()
    async def zatca_go_live_start(
        environment: GoLiveEnvironment, confirm_back_to_sandbox: bool = False
    ) -> Any:
        """Naya go-live wizard shuru (OWNER only): naye environment ke liye nayi device onboard karne ka rasta.
        Tarteeb: start -> details -> keys -> compliance-csid (OTP) -> checks -> production-csid -> finish.
        Simulation/Production ke liye asli (test nahi) VAT number aur Fatoora portal se asli OTP chahiye.
        Live environment se 'sandbox' par wapas jane ke liye `confirm_back_to_sandbox=True` (warna 400
        error.zatcaBackToSandboxConfirm), aur agar chalti device ne production document jari kiya ho to mana
        (409 error.zatcaBackToSandboxProduction). Pehle se ek wizard khula ho to 409
        error.zatcaGoLiveInProgress. Details sandbox ke liye sandbox identity se, live ke liye maujooda
        (asli) details ya branch ke naam/address se pre-fill hoti hain. Result: wizard payload (201)."""
        return await client.post(
            "zatca/go-live/", {"environment": environment, "confirm_back_to_sandbox": confirm_back_to_sandbox}
        )

    @mcp.tool()
    async def zatca_go_live_update_details(
        seller_name_en: str | None = None,
        seller_name_ar: str | None = None,
        vat_number: str | None = None,
        other_id: str | None = None,
        other_id_scheme: SellerIdScheme | None = None,
        street: str | None = None,
        building_no: str | None = None,
        additional_no: str | None = None,
        district: str | None = None,
        city: str | None = None,
        postal_code: str | None = None,
        org_unit: str | None = None,
        business_category: str | None = None,
        invoice_types: InvoiceTypes | None = None,
    ) -> Any:
        """Wizard ki business details save (OWNER only); khule wizard mein sirf diye gaye fields badalte hain. Ye
        details settings mein SIRF `finish` par jati hain (taake settings aur device hamesha mel khayein).
        Keys banne ke baad details LOCK (409 error.zatcaGoLiveDetailsLocked). Practice (sandbox) run mein
        vat_number, other_id, other_id_scheme sandbox identity par fixed (error.zatcaPracticeIdentityFixed);
        live environment mein test VAT number mana (error.zatcaTestVatNotAllowed). Galat details par 400
        error.zatcaFixDetails jis ke `fields` mein har field ka error. Wizard khula na ho to 404
        error.zatcaGoLiveNotOpen. Result: wizard payload."""
        body = drop_none(
            {
                "seller_name_en": seller_name_en,
                "seller_name_ar": seller_name_ar,
                "vat_number": vat_number,
                "other_id": other_id,
                "other_id_scheme": other_id_scheme,
                "street": street,
                "building_no": building_no,
                "additional_no": additional_no,
                "district": district,
                "city": city,
                "postal_code": postal_code,
                "org_unit": org_unit,
                "business_category": business_category,
                "invoice_types": invoice_types,
            }
        )
        return await client.patch("zatca/go-live/", body)

    @mcp.tool()
    async def zatca_go_live_cancel() -> Any:
        """Khula wizard cancel (OWNER only): jo device is run ne banai (aur istemal nahi hui) archive ho jati hai;
        chalti device ko kuch nahi hota. Khula wizard na ho to 404 error.zatcaGoLiveNotOpen."""
        return await client.delete("zatca/go-live/")

    @mcp.tool()
    async def zatca_go_live_generate_keys() -> Any:
        """Step: private key aur CSR server par banana (OWNER only), wizard ki details se. Key kabhi bahar nahi
        aati (encrypted store hoti hai; ZATCA_ENCRYPTION_KEY chahiye). Pehle se keys ban chuki hon aur abhi
        CSID na mila ho to purani archive ho kar nayi banti hain; aage barh chuki ho to 409
        error.zatcaGoLiveKeysUsed. Details galat ho to 400 error.zatcaFixDetails. Result: wizard payload (step
        'otp')."""
        return await client.post("zatca/go-live/keys/")

    @mcp.tool()
    async def zatca_go_live_compliance_csid(otp: str = "", confirm_legal: bool = False) -> Any:
        """Step: ZATCA se compliance CSID lena (OWNER only). `otp` 6-digit Fatoora portal ka code (sandbox mein khali
        chhor dein, public sandbox OTP khud lagta hai). WARNING: OTP ek ek-martaba secret hai; chat mein dene se wo
        conversation mein aa jata hai (server usay na store karta hai na log). Simulation/Production mein asli OTP
        (6 digits, sandbox wala nahi: error.zatcaOtpInvalid; khali: error.zatcaOtpRequired). PRODUCTION mein
        `confirm_legal=True` zaroori hai (error.zatcaLegalConfirmRequired): 'production invoices qanooni taur par
        asli hain aur aap ke VAT number par ZATCA ko report hote hain'. ZATCA ne mana kiya to 400
        error.zatcaStepFailed (detail mein ZATCA ka message). Result: wizard payload (step 'checks')."""
        return await client.post(
            "zatca/go-live/compliance-csid/", {"otp": otp, "confirm_legal": confirm_legal}
        )

    @mcp.tool()
    async def zatca_go_live_run_checks() -> Any:
        """Step: ZATCA ke saare compliance checks chalana (OWNER only); dohraya ja sakta hai (kisi check ke fail
        hone ke baad wohi CSID). ZATCA ko test invoices/notes jate hain. Fail par 400 error.zatcaChecksFailed, jis ke
        saath har check ka natija (`checks`: label, passed, errors, warnings). Sab pass par device 'compliance
        passed' (step 'production'). Result: wizard payload."""
        return await client.post("zatca/go-live/checks/")

    @mcp.tool()
    async def zatca_go_live_production_csid() -> Any:
        """Step: production CSID lena (OWNER only), jab saare checks pass ho chuke hon (warna 409
        error.zatcaGoLiveWrongStep). Device 'ready' ho jati hai (step 'activate'). Fail par 400
        error.zatcaStepFailed. Result: wizard payload."""
        return await client.post("zatca/go-live/production-csid/")

    @mcp.tool()
    async def zatca_go_live_finish(activate: bool) -> Any:
        """Aakhri step (OWNER only): `activate=True` nayi device ko branch ki device bana deta hai AUR wizard ki
        details aur environment settings mein lagata hai (ek transaction mein): us waqt se naye invoices naye
        environment/identity par jayenge. PRODUCTION mein ye asli, qanooni invoicing shuru karna hai, aur Production
        se wapas sirf wizard ke zariye (sandbox back-to-sandbox production documents ke baad mana). Sirf practice
        (sandbox) run `activate=False` kar sakta hai (device 'ready' rehti hai, kuch switch nahi hota); live run mein
        400 error.zatcaGoLiveMustActivate. Device 'ready' na ho to 409 error.zatcaGoLiveWrongStep. Result: wizard
        payload (status 'completed'). REAL ASAR: activate karne se pehle details aur `zatca_go_live_get` ka
        checklist dekhein."""
        return await client.post("zatca/go-live/finish/", {"activate": activate})

    # ============================================================ documents
    @mcp.tool()
    async def zatca_documents_list(status: DocStatus | None = None) -> Any:
        """Branch ke ZATCA documents (owner/manager), naye pehle (ICV ke hisaab se), max 200, ZATCA ke jawab ke saath
        (XML nahi). `status` filter. Response: {counts {pending, reported, cleared, rejected} (HAMESHA poori branch
        ke), documents [{id, number, kind (invoice|credit_note|debit_note), simplified, environment, icv, status,
        zatca_status, http_status, warnings, errors, last_error, attempts, last_attempt_at, accepted_at, issued_at,
        total, vat_total, net_total, vat_breakdown, order_numbers, original_number (credit/debit note kis invoice par),
        buyer_name, source}]}."""
        return await client.get("zatca/documents/", {"status": status})

    @mcp.tool()
    async def zatca_document_retry(document_id: int) -> Any:
        """Ek PENDING document foran ZATCA ko bhejna (owner/manager); retry queue ka intezar nahi. Jo pending
        nahi (reported/cleared/rejected) usay kuch nahi hota, sirf document wapas milta hai. ZATCA ko REAL
        submission hoti hai (production mein qanooni). Result: updated document."""
        return await client.post(f"zatca/documents/{document_id}/retry/")

    @mcp.tool()
    async def zatca_document_xml_download(
        document_id: int, which: Literal["signed", "cleared"] = "signed", save_dir: str | None = None
    ) -> Any:
        """Document ka XML (owner/manager), disk par save: {path, bytes}. `which='signed'` wo XML jo is POS ne
        sign kiya (har document ke liye); 'cleared' wo copy jo ZATCA ne cleared B2B invoice ke liye wapas bheji
        (na ho to 404). Ghalat `which` par 400 error.xmlKindInvalid."""
        content = await client.get_bytes(f"zatca/documents/{document_id}/xml/", {"which": which})
        suffix = "_cleared" if which == "cleared" else ""
        return save_bytes(content, f"zatca-document-{document_id}{suffix}.xml", save_dir)

    @mcp.tool()
    async def zatca_debit_note_create(
        document_id: int, description: str, amount: float, reason: str
    ) -> Any:
        """Invoice par DEBIT NOTE (383) banana, under-charge ke liye (OWNER only): `amount` wo raqam jo customer ke
        zimme baqi hai, VAT SAMET (0.01 se 1,000,000). `description` aur `reason` zaroori (<=255 chars). Note
        ZATCA ko foran bheja jata hai (REAL, qanooni asar) aur 201 ke saath document wapas aata hai. Sirf INVOICE par
        aur rejected par nahi (409 error.zatcaDebitNeedsInvoice); active device na ho to 409
        error.zatcaNoActiveDevice. Errors: error.zatcaDebitAmountInvalid, error.zatcaFieldRequired."""
        return await client.post(
            f"zatca/documents/{document_id}/debit-note/",
            {"description": description, "amount": round(amount, 2), "reason": reason},
        )

    # ============================================================ till / receipts
    @mcp.tool()
    async def zatca_pos_status() -> Any:
        """Till ko kya jaanna hai (cashier/owner/manager): {enabled (invoices ban rahe hain: ZATCA on aur active device),
        is_test (test environment), b2b (company/standard invoices offer hon)}. Aur kuch nahi."""
        return await client.get("zatca/pos-status/")

    @mcp.tool()
    async def zatca_receipts_get(
        order: int | None = None, bill: int | None = None, document: int | None = None
    ) -> Any:
        """Chhapne layak tax invoices/credit notes (cashier). Kam az kam ek do: `order` (order id), `bill` (bill id)
        ya `document` (sirf wohi ek document, jaise invoices page se reprint). ZATCA band ho ya kuch invoice na hua ho
        to khali (POS tab apni aam receipt chhapta hai). Koi na dene par 400 error.zatcaOrderOrBillRequired; ghalat
        `document` par 404. Reprint se pehle zatca_receipt_mark_printed chalayein."""
        return await client.get("zatca/receipts/", {"order": order, "bill": bill, "document": document})

    @mcp.tool()
    async def zatca_receipt_mark_printed(document_id: int) -> Any:
        """Chhapne SE PEHLE call karein (cashier): print_count 1 badhata hai. Pehli print ke baad har print
        'Copy' wali reprint hai. Result: {print_count, copy}. Side effect (counter), dekhne ke liye
        zatca_receipts_get."""
        return await client.post(f"zatca/receipts/{document_id}/printed/")

    @mcp.tool()
    async def zatca_public_receipt_get(uuid: str) -> Any:
        """[Public, login nahi] Customer ki apni simplified invoice uski UUID (v4, andaza nahi lagta) se, Moyasar
        success page ke download/share link ke liye. Kabhi standard (B2B) invoice nahi: unmein company ki details
        hoti hain. Na milne par 404."""
        return await client.get(f"zatca/public/receipts/{uuid}/", auth=False)

    # ============================================================ B2B buyers
    @mcp.tool()
    async def zatca_business_customers_search(search: str | None = None) -> Any:
        """Company (B2B) buyers (cashier), max 20: `search` legal_name par (partial) ya vat_number ke shuru se.
        Har buyer: {id, legal_name, vat_number, crn, street, building_no, additional_no, district, city,
        postal_code, country}."""
        return await client.get("zatca/business-customers/", {"search": search})

    @mcp.tool()
    async def zatca_business_customer_save(
        legal_name: str,
        vat_number: str,
        street: str,
        district: str,
        city: str,
        crn: str | None = None,
        building_no: str | None = None,
        additional_no: str | None = None,
        postal_code: str | None = None,
    ) -> Any:
        """Company buyer add, ya update agar yeh VAT number is branch mein pehle se maloom ho (cashier): UPSERT
        by vat_number; hamesha HTTP 201 (naya aur update mein farq nahi). legal_name, vat_number, street,
        district, city zaroori (error.zatcaFieldRequired); VAT number 15-digit format, building_no/additional_no/
        postal_code (4/4/5 digits) aur crn ka format check hota hai. Result: buyer."""
        body = drop_none(
            {
                "legal_name": legal_name,
                "vat_number": vat_number,
                "crn": crn,
                "street": street,
                "building_no": building_no,
                "additional_no": additional_no,
                "district": district,
                "city": city,
                "postal_code": postal_code,
            }
        )
        return await client.post("zatca/business-customers/", body)

    @mcp.tool()
    async def zatca_order_buyer_get(order_id: int) -> Any:
        """Order ki aane wali tax invoice kis company ko jayegi (cashier): {business_customer: buyer ya null (B2C)}."""
        return await client.get(f"zatca/orders/{order_id}/buyer/")

    @mcp.tool()
    async def zatca_order_buyer_set(order_id: int, business_customer: int | None = None) -> Any:
        """Order ki invoice ko company par point karna, ya `business_customer=None` se wapas B2C (cashier). Us
        payment se PEHLE jo order settle karti hai: invoice ban chuki ho to 409 error.zatcaAlreadyInvoiced.
        Buyer isi branch ka hona chahiye (404). Result: {business_customer}."""
        # None explicit bhejna zaroori hai (B2C par wapas), is liye drop_none nahi.
        return await client.post(f"zatca/orders/{order_id}/buyer/", {"business_customer": business_customer})

    @mcp.tool()
    async def zatca_bill_buyer_get(bill_id: int) -> Any:
        """Table bill ki aane wali invoice ka buyer (cashier): bill ke un orders ka jo cancelled nahi aur abhi paid
        nahi. Result: {business_customer: buyer ya null}."""
        return await client.get(f"zatca/bills/{bill_id}/buyer/")

    @mcp.tool()
    async def zatca_bill_buyer_set(bill_id: int, business_customer: int | None = None) -> Any:
        """Table bill ke orders (jo cancelled/paid nahi) ka invoice buyer set karna, ya `None` se B2C (cashier).
        Koi order pehle se invoiced ho to 409 error.zatcaAlreadyInvoiced. Result: {business_customer}."""
        return await client.post(f"zatca/bills/{bill_id}/buyer/", {"business_customer": business_customer})
