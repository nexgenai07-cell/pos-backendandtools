"""
marketing app ke MCP tools: campaigns (WhatsApp + Facebook/Instagram), AI draft, Meta connection.

Backend routes (marketing/urls.py), SAB owner/manager, sirf Meta connect/disconnect OWNER-only:
  GET    marketing/config/                          marketing_config_get
  campaigns/            GET list, POST create, GET/PATCH/DELETE <id>   (PUT allowed nahi)
  POST   campaigns/preview/                         audience count + sample (unsaved rule)
  POST   campaigns/ai-draft/                        AI caption/message draft (kuch save nahi hota)
  POST   campaigns/<id>/prepare|reopen|cancel/      WhatsApp lifecycle
  GET    campaigns/<id>/recipients/                 recipients + wa.me links
  POST   campaigns/<id>/mark-sent/                  manual send record
  POST   campaigns/<id>/send/                       Cloud API se bhejna (agar configured)
  POST   campaigns/<id>/retry-failed/               failed -> pending
  POST   campaigns/<id>/publish/                    Facebook/Instagram par POST (public, foran)
  GET    campaigns/<id>/posts/                      posts ka status (+ Instagram video progress)
  POST   campaigns/<id>/posts/<facebook|instagram>/retry/
  GET    marketing/connections/                     (owner/manager) connection status, secrets nahi
  POST   marketing/connections/meta/                (owner) page_id + access_token se connect
  POST   marketing/connections/meta/oauth/{start,pages,connect}/   (owner) Facebook Login flow
  POST   marketing/connections/<id>/verify/         (owner)
  DELETE marketing/connections/<id>/                (owner) disconnect

Campaign lifecycle:
  WhatsApp:  draft --prepare--> ready --(mark-sent | send)--> sending --> sent   (reopen: ready -> draft)
  Facebook/Instagram (WhatsApp ke baghair): draft --publish--> sent (jab har ticked channel post ho jaye)
  Kisi bhi waqt (sent ke ilawa): cancel. Sirf DRAFT campaign edit ho sakta hai (409 error.campaignNotEditable).
  Delete: sending/sent ya live posts wale campaign par 409 error.campaignNotDeletable.

REAL-WORLD ASAR (dhyan se):
  * campaign_send  -> asli customers ko WhatsApp message jata hai (max 100 recipients per call).
  * campaign_publish / campaign_retry_post -> asli Facebook/Instagram page par PUBLIC post.
  * campaign_mark_sent -> sirf record badalta hai (message nahi bhejta; staff wa.me link khud tap karta hai).
  Marketing consent: 'opted_out' customers kabhi audience mein nahi aate.

Pagination: default 25/page, `page_size` max 200.
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none

CampaignStatus = Literal["draft", "ready", "sending", "sent", "cancelled"]
RecipientStatus = Literal["pending", "sent", "failed", "skipped"]
MediaType = Literal["image", "video"]
AudienceConsent = Literal["opted_in", "not_opted_out"]
SocialChannel = Literal["facebook", "instagram"]


def register(mcp, client: POSClient) -> None:
    # ============================================================ config
    @mcp.tool()
    async def marketing_config_get() -> Any:
        """Campaign screen se pehle ki zaroori info (owner/manager): whatsapp_cloud_configured (automated send
        possible?), facebook_connected, instagram_connected, ai_enabled, ai_drafts_left_today,
        default_country_code (bina country code ke number ke liye wa.me link), max_recipients,
        send_batch_limit (campaign_send ek call mein itne)."""
        return await client.get("marketing/config/")

    # ============================================================ campaigns CRUD
    @mcp.tool()
    async def campaigns_list(
        status: CampaignStatus | None = None,
        search: str | None = None,
        ordering: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ke campaigns (owner/manager). `search` naam/message/caption par; `ordering` keys:
        name, status, created_at, sent_at ('-' = descending; default newest pehle). Har row mein
        recipient_counts (pending/sent/failed/skipped/total) aur is_editable."""
        return await client.get(
            "campaigns/",
            {"status": status, "search": search, "ordering": ordering, "page": page, "page_size": page_size},
        )

    @mcp.tool()
    async def campaign_get(campaign_id: int) -> Any:
        """Ek campaign ki detail (owner/manager)."""
        return await client.get(f"campaigns/{campaign_id}/")

    @mcp.tool()
    async def campaign_create(
        name: str,
        send_whatsapp: bool = True,
        post_facebook: bool = False,
        post_instagram: bool = False,
        message_en: str | None = None,
        message_ar: str | None = None,
        fallback_name: str | None = None,
        post_caption: str | None = None,
        media_type: MediaType | None = None,
        media_url: str | None = None,
        audience_consent: AudienceConsent | None = None,
        min_orders: int | None = None,
        ordered_within_days: int | None = None,
        not_ordered_for_days: int | None = None,
        scheduled_for: str | None = None,
    ) -> Any:
        """Naya campaign DRAFT banata hai (owner/manager); kuch bheja ya post nahi hota.
        Channels: kam az kam ek (send_whatsapp / post_facebook / post_instagram), warna error.campaignChannelRequired.
        WhatsApp ke liye message_en ya message_ar zaroori; `fallback_name` jab customer ka naam na ho.
        Facebook: caption ya media; Instagram: media zaroori (error.instagramNeedsMedia), max hashtags ki hadd.
        `media_url` + `media_type` dono ya koi nahi, aur URL apne Cloudinary upload ka hona chahiye
        (error.campaignMediaInvalid). Audience rule (sirf WhatsApp): `audience_consent` 'opted_in' (sirf haan
        kehne wale) ya 'not_opted_out' (default: opt-out ke ilawa sab), `min_orders` (default 1),
        `ordered_within_days` aur `not_ordered_for_days` (not_ordered < within hona chahiye,
        error.impossibleAudienceWindow). `scheduled_for` ISO datetime."""
        body = drop_none(
            {
                "name": name,
                "send_whatsapp": send_whatsapp,
                "post_facebook": post_facebook,
                "post_instagram": post_instagram,
                "message_en": message_en,
                "message_ar": message_ar,
                "fallback_name": fallback_name,
                "post_caption": post_caption,
                "media_type": media_type,
                "media_url": media_url,
                "audience_consent": audience_consent,
                "min_orders": min_orders,
                "ordered_within_days": ordered_within_days,
                "not_ordered_for_days": not_ordered_for_days,
                "scheduled_for": scheduled_for,
            }
        )
        return await client.post("campaigns/", body)

    @mcp.tool()
    async def campaign_update(
        campaign_id: int,
        name: str | None = None,
        send_whatsapp: bool | None = None,
        post_facebook: bool | None = None,
        post_instagram: bool | None = None,
        message_en: str | None = None,
        message_ar: str | None = None,
        fallback_name: str | None = None,
        post_caption: str | None = None,
        media_type: MediaType | None = None,
        media_url: str | None = None,
        audience_consent: AudienceConsent | None = None,
        min_orders: int | None = None,
        ordered_within_days: int | None = None,
        not_ordered_for_days: int | None = None,
        scheduled_for: str | None = None,
    ) -> Any:
        """Campaign PATCH (owner/manager); sirf diye gaye fields. Sirf DRAFT campaign edit hota hai, warna 409
        error.campaignNotEditable (ready ho to pehle campaign_reopen). Validation create jaisi hai (channels,
        media, audience window)."""
        body = drop_none(
            {
                "name": name,
                "send_whatsapp": send_whatsapp,
                "post_facebook": post_facebook,
                "post_instagram": post_instagram,
                "message_en": message_en,
                "message_ar": message_ar,
                "fallback_name": fallback_name,
                "post_caption": post_caption,
                "media_type": media_type,
                "media_url": media_url,
                "audience_consent": audience_consent,
                "min_orders": min_orders,
                "ordered_within_days": ordered_within_days,
                "not_ordered_for_days": not_ordered_for_days,
                "scheduled_for": scheduled_for,
            }
        )
        return await client.patch(f"campaigns/{campaign_id}/", body)

    @mcp.tool()
    async def campaign_delete(campaign_id: int) -> Any:
        """Campaign delete (owner/manager). sending/sent ya jis ke live posts hon, uska 409
        error.campaignNotDeletable. Bhejne se rokna ho to campaign_cancel."""
        return await client.delete(f"campaigns/{campaign_id}/")

    # ========================================================= preview + AI
    @mcp.tool()
    async def campaign_preview_audience(
        audience_consent: AudienceConsent | None = None,
        min_orders: int | None = None,
        ordered_within_days: int | None = None,
        not_ordered_for_days: int | None = None,
        message_en: str | None = None,
        message_ar: str | None = None,
        fallback_name: str | None = None,
        media_url: str | None = None,
    ) -> Any:
        """Unsaved audience rule kitne customers se match karta hai (owner/manager; kuch save/send nahi hota):
        {count, max_recipients, sample (id, name, phone, order_count, last_order, marketing_consent),
        message_preview (pehle customer ke liye message jaisa padha jayega)}. Campaign banane se pehle
        'kitne log?' jaanne ke liye."""
        body = drop_none(
            {
                "audience_consent": audience_consent,
                "min_orders": min_orders,
                "ordered_within_days": ordered_within_days,
                "not_ordered_for_days": not_ordered_for_days,
                "message_en": message_en,
                "message_ar": message_ar,
                "fallback_name": fallback_name,
                "media_url": media_url,
            }
        )
        return await client.post("campaigns/preview/", body)

    @mcp.tool()
    async def campaign_ai_draft(
        product_id: int | None = None,
        notes: str | None = None,
        send_whatsapp: bool = True,
        post_facebook: bool = False,
        post_instagram: bool = False,
    ) -> Any:
        """AI se caption/WhatsApp message ka pehla DRAFT (owner/manager); kuch save/post nahi hota (sirf
        usage log). `product_id` (us ki deal saath aati hai) ya `notes` (<=2000 chars) kam az kam ek zaroori
        (error.aiDraftNeedsSubject). Result mein AI ke likhe numbers jo product data/notes mein nahi the
        `unverified_numbers` mein wapas aate hain -- unhein insaan check kare. Roz ki hadd (branch par) ke
        baad 429 imports.aiDailyLimit; AI configured na ho to 503 imports.aiNotConfigured. `left_today` mein
        baqi drafts."""
        return await client.post(
            "campaigns/ai-draft/",
            drop_none(
                {
                    "product_id": product_id,
                    "notes": notes,
                    "send_whatsapp": send_whatsapp,
                    "post_facebook": post_facebook,
                    "post_instagram": post_instagram,
                }
            ),
        )

    # ============================================= WhatsApp lifecycle
    @mcp.tool()
    async def campaign_prepare(campaign_id: int) -> Any:
        """Draft ka audience recipients mein FREEZE karta hai aur campaign 'ready' (owner/manager). Abhi kuch
        bheja nahi jata. Errors: error.campaignNotDraft, error.campaignNoWhatsapp (WhatsApp ticked nahi),
        error.emptyAudience."""
        return await client.post(f"campaigns/{campaign_id}/prepare/")

    @mcp.tool()
    async def campaign_reopen(campaign_id: int) -> Any:
        """Ready campaign wapas draft (owner/manager): frozen recipient list HARD-delete (snapshot hai, history
        nahi). Sirf jab kuch bheja na gaya ho: error.campaignNotReady / error.campaignAlreadyStarted."""
        return await client.post(f"campaigns/{campaign_id}/reopen/")

    @mcp.tool()
    async def campaign_cancel(campaign_id: int) -> Any:
        """Campaign cancel (owner/manager); sent campaign par 409 error.campaignAlreadySent. Jo message ja
        chuke wo wapas nahi hote."""
        return await client.post(f"campaigns/{campaign_id}/cancel/")

    @mcp.tool()
    async def campaign_recipients(
        campaign_id: int,
        status: RecipientStatus | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Campaign ke recipients (owner/manager), paginated; har row mein customer, status aur wa.me link
        jis se manual send hota hai. `status` filter."""
        return await client.get(
            f"campaigns/{campaign_id}/recipients/", {"status": status, "page": page, "page_size": page_size}
        )

    @mcp.tool()
    async def campaign_mark_sent(
        campaign_id: int,
        recipient_ids: list[int] | None = None,
        all_pending: bool = False,
    ) -> Any:
        """Manual path: "staff ne wa.me link tap karke bhej diya" ka record (owner/manager). Message KHUD nahi
        bhejta. `recipient_ids` do, ya poore pending ke liye `all_pending=True` (jaan-boojh kar explicit,
        mis-click se 'sab ko bhej diya' na ho). Campaign ready/sending hona chahiye
        (error.campaignNotSendable); dono na do to error.recipientsRequired. Result {marked, campaign}; sab bhej
        diye to campaign 'sent'."""
        body: dict[str, Any] = {"all": True} if all_pending else {"recipient_ids": recipient_ids}
        return await client.post(f"campaigns/{campaign_id}/mark-sent/", body)

    @mcp.tool()
    async def campaign_send(campaign_id: int) -> Any:
        """WhatsApp Cloud API se pending recipients ko ASLI message bhejta hai (owner/manager): ek call mein
        max send_batch_limit (100). Result {sent, failed, pending, campaign}; pending > 0 ho to dobara call
        karein. REAL customers ko message jata hai, wapas nahi hota: pehle campaign_preview_audience /
        campaign_recipients dekhein. Cloud API configured na ho to 409 error.whatsappNotConfigured;
        error.campaignNotSendable."""
        return await client.post(f"campaigns/{campaign_id}/send/")

    @mcp.tool()
    async def campaign_retry_failed(campaign_id: int) -> Any:
        """Failed recipients ko wapas pending (owner/manager) taake campaign_send unhein dobara uthaye.
        Result {reset, campaign}. Khud kuch nahi bhejta."""
        return await client.post(f"campaigns/{campaign_id}/retry-failed/")

    # ================================================== Facebook / Instagram
    @mcp.tool()
    async def campaign_publish(campaign_id: int) -> Any:
        """Ticked Facebook/Instagram channels par FORAN, PUBLIC post (owner/manager). Jo channel pehle se
        posted/processing hai use chhoda jata hai (dobara call = sirf failed retry). Result {posts, campaign}.
        Instagram video processing mein waqt leta hai: campaign_posts se poll karein. REAL page par public
        post: pehle caption/media check karein. Errors: error.campaignCancelled, error.campaignNoPosts,
        error.campaignAlreadyPublished, error.metaNotConnected, error.socialEncryptionNotConfigured."""
        return await client.post(f"campaigns/{campaign_id}/publish/")

    @mcp.tool()
    async def campaign_posts(campaign_id: int) -> Any:
        """Campaign ke posts (channel ke hisaab se status: pending/processing/posted/failed) (owner/manager).
        Side-effect: agar koi post 'processing' ho (Instagram video) to har call Meta se ek baar check karke
        taiyar hone par publish bhi kar deta hai -- isi liye admin har kuch second ye poll karta hai."""
        return await client.get(f"campaigns/{campaign_id}/posts/")

    @mcp.tool()
    async def campaign_retry_post(campaign_id: int, channel: SocialChannel) -> Any:
        """EK failed post dobara (owner/manager): public post hota hai. Sirf failed post par, warna 409
        error.postNotFailed; error.campaignCancelled. Result {posts, campaign}."""
        return await client.post(f"campaigns/{campaign_id}/posts/{channel}/retry/")

    # ================================================== Meta connection
    @mcp.tool()
    async def marketing_connections_get() -> Any:
        """Facebook/Instagram connection ka status (owner/manager; token wagera kabhi nahi aata):
        {encryption: {configured, problem}, oauth: {configured}, connections: [...]}."""
        return await client.get("marketing/connections/")

    @mcp.tool()
    async def marketing_connect_meta(page_id: str, access_token: str) -> Any:
        """Facebook Page ko token se connect (OWNER only). `page_id` 5-32 digits. WARNING: `access_token` ek
        SECRET hai -- ise yahan chat mein paste karne se wo conversation mein aa jata hai; behtar hai ke admin
        panel ya OAuth flow (marketing_oauth_*) se connect karein. Backend token ko encrypt karke rakhta hai.
        Errors: error.metaPageIdInvalid, error.socialEncryptionNotConfigured, Meta ki taraf ke errors
        (detail ke saath, 400/502)."""
        return await client.post("marketing/connections/meta/", {"page_id": page_id, "access_token": access_token})

    @mcp.tool()
    async def marketing_oauth_start() -> Any:
        """Facebook Login shuru (OWNER only): {url} wapas -- ye URL BROWSER mein khulna chahiye. Login ke baad
        Facebook admin ke callback par code+state bhejta hai, jo agle step (marketing_oauth_pages) mein dena
        hai. Errors: OAuth configured na ho to 409 (code), error.socialEncryptionNotConfigured."""
        return await client.post("marketing/connections/meta/oauth/start/")

    @mcp.tool()
    async def marketing_oauth_pages(code: str, state: str) -> Any:
        """OAuth ka doosra step (OWNER only): browser callback se mile `code` aur `state` dein; result
        {pages: [...], session}. Phir marketing_oauth_connect mein koi page chun kar `session` dein.
        Errors: error.metaOauthFailed / error.metaOauthExpired."""
        return await client.post("marketing/connections/meta/oauth/pages/", {"code": code, "state": state})

    @mcp.tool()
    async def marketing_oauth_connect(session: str, page_id: str) -> Any:
        """OAuth ka aakhri step (OWNER only): chuna hua Page connect. `session` marketing_oauth_pages se,
        `page_id` 5-32 digits. Result: connection (201). Errors: error.metaPageIdInvalid,
        error.metaOauthExpired."""
        return await client.post("marketing/connections/meta/oauth/connect/", {"session": session, "page_id": page_id})

    @mcp.tool()
    async def marketing_connection_verify(connection_id: int) -> Any:
        """Meta se dobara pooch kar connection ki sehat check (OWNER only): status connected/expired/error
        update hota hai. Updated connection wapas."""
        return await client.post(f"marketing/connections/{connection_id}/verify/")

    @mcp.tool()
    async def marketing_connection_delete(connection_id: int) -> Any:
        """Facebook/Instagram DISCONNECT (OWNER only). Iske baad campaigns post nahi kar sakengi
        (error.metaNotConnected) jab tak dobara connect na karein."""
        return await client.delete(f"marketing/connections/{connection_id}/")
