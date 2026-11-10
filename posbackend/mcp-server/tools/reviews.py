"""
reviews app ke MCP tools: guest reviews ki moderation, admin-added reviews, public website reviews.

Backend routes (reviews/urls.py):
  Staff (owner/manager, branch-scoped):
    GET    reviews/                 list (filters + pagination)
    POST   reviews/                 admin review add (foran 'approved')
    GET    reviews/<id>/            detail
    PATCH  reviews/<id>/            edit (sirf admin-added reviews)
    DELETE reviews/<id>/            delete (soft)
    POST   reviews/<id>/approve/    pending/rejected -> approved
    POST   reviews/<id>/reject/     -> rejected
    GET    reviews/summary/         status ke hisaab se counts
  Public (login nahi):
    GET    reviews/public/                 website par approved reviews
    GET    reviews/qr/eligibility/         kya is bill par review ho sakta hai
    POST   reviews/qr/                     guest review submit (hamesha 'pending')

Review flow:
  Guest review (QR) -> status 'pending' -> owner/manager approve -> website par dikhta hai.
  Admin-added review -> foran 'approved'. Ek bill par ek hi review, aur sirf tab jab bill paid ho aur
  uski saari items served hon.
  Guest review ka text kabhi edit nahi hota (website guest ke naam se wo alfaaz na dikhaye jo usne nahi
  likhe) -> PATCH par 409 error.reviewNotEditable; sirf approve/reject/delete.
  PUT backend mein allowed nahi hai, sirf PATCH.

Pagination: default 25/page, `page_size` max 200. Default ordering: newest posted_at pehle.
"""
from __future__ import annotations

from typing import Any, Literal

from client import POSClient, drop_none

ReviewStatus = Literal["pending", "approved", "rejected"]
ReviewSource = Literal["guest", "admin"]
Rating = Literal[1, 2, 3, 4, 5]


def register(mcp, client: POSClient) -> None:
    # ============================================================ staff
    @mcp.tool()
    async def reviews_list(
        status: ReviewStatus | None = None,
        source: ReviewSource | None = None,
        rating: Rating | None = None,
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Any:
        """Apni branch ke reviews (owner/manager). Filters: `status`, `source` (guest|admin), `rating` (1-5),
        `search` (naam ya comment), `date_from`/`date_to` (YYYY-MM-DD, branch timezone, posted_at par).
        Har row mein bill_code, table_label, customer_phone (guest reviews ke liye), moderated_by_name.
        Moderation queue ke liye status='pending'."""
        return await client.get(
            "reviews/",
            {
                "status": status,
                "source": source,
                "rating": rating,
                "search": search,
                "from": date_from,
                "to": date_to,
                "page": page,
                "page_size": page_size,
            },
        )

    @mcp.tool()
    async def reviews_summary() -> Any:
        """Reviews ki ginti status ke hisaab se: {pending, approved, rejected, all} (owner/manager).
        Sidebar badge / tabs ke liye."""
        return await client.get("reviews/summary/")

    @mcp.tool()
    async def review_get(review_id: int) -> Any:
        """Ek review ki detail (owner/manager)."""
        return await client.get(f"reviews/{review_id}/")

    @mcp.tool()
    async def review_create(
        name: str,
        rating: Rating,
        comment: str | None = None,
    ) -> Any:
        """Admin ki taraf se review add (owner/manager) -- jaise kisi aur platform ka review manually dalna.
        Source 'admin' aur status foran 'approved' hota hai, yani website par TURANT dikhega
        (moderation nahi hoti), is liye sirf asli review hi dalein. `name` zaroori (<=80 chars,
        error.reviewNameRequired), `rating` 1-5, `comment` optional (<=1000 chars)."""
        return await client.post("reviews/", drop_none({"name": name, "rating": rating, "comment": comment}))

    @mcp.tool()
    async def review_update(
        review_id: int,
        name: str | None = None,
        rating: Rating | None = None,
        comment: str | None = None,
    ) -> Any:
        """Review PATCH (owner/manager); sirf ADMIN-added reviews. Guest review par 409
        error.reviewNotEditable (unka text kabhi nahi badalta; approve/reject/delete use karein)."""
        return await client.patch(
            f"reviews/{review_id}/", drop_none({"name": name, "rating": rating, "comment": comment})
        )

    @mcp.tool()
    async def review_delete(review_id: int) -> Any:
        """Review delete (owner/manager, soft-delete); guest ho ya admin. Website se hat jata hai.
        Sirf chhupana ho to review_reject behtar hai."""
        return await client.delete(f"reviews/{review_id}/")

    @mcp.tool()
    async def review_approve(review_id: int) -> Any:
        """Review approve (owner/manager): status 'approved' -- ab customer website par dikhega.
        moderated_by/moderated_at record hote hain. Updated review wapas aata hai."""
        return await client.post(f"reviews/{review_id}/approve/")

    @mcp.tool()
    async def review_reject(review_id: int) -> Any:
        """Review reject (owner/manager): status 'rejected' -- website par nahi dikhega (pehle approved tha
        to wahan se hat jata hai). Updated review wapas aata hai."""
        return await client.post(f"reviews/{review_id}/reject/")

    # ============================================================ public
    @mcp.tool()
    async def public_reviews_list(branch: int | None = None) -> Any:
        """[Public, login nahi] Website par dikhne wale APPROVED reviews, naye pehle (max 60), sirf
        {id, name, rating, comment, source, posted_at} -- phone/bill/staff details nahi. `branch` na do to
        sabse chhoti id wali branch. Error: error.noBranch (404)."""
        return await client.get("reviews/public/", {"branch": branch}, auth=False)

    @mcp.tool()
    async def public_review_eligibility(
        session_token: str, phone: str, bill_id: int | None = None
    ) -> Any:
        """[Public] Kya is customer ke bill par review chhoda ja sakta hai: {can_review: bool, reason: error code
        ya null}. Table ke QR session_token + customer ka phone se; ghalat token par 404 error.notFound.
        Doosre phone ke bill ke baare mein kuch nahi batata."""
        return await client.get(
            "reviews/qr/eligibility/",
            {"session_token": session_token, "phone": phone, "bill_id": bill_id},
            auth=False,
        )

    @mcp.tool()
    async def public_review_submit(
        session_token: str,
        phone: str,
        bill_id: int,
        name: str,
        rating: Rating,
        comment: str | None = None,
    ) -> Any:
        """[Customer, public] Guest review submit. Bill settled (paid) aur poori served honi chahiye, aur ek
        bill par ek hi review (warna 409 ke saath wajah ka error code, pehle public_review_eligibility dekhein).
        Hamesha 'pending' save hota hai aur staff ko 'review:created' realtime event jata hai; website par
        tab dikhega jab owner/manager approve kare. `name` <=80, `comment` <=1000 chars. Throttled
        ('review'). Response: {id, status}."""
        return await client.post(
            "reviews/qr/",
            drop_none(
                {
                    "session_token": session_token,
                    "phone": phone,
                    "bill_id": bill_id,
                    "name": name,
                    "rating": rating,
                    "comment": comment,
                }
            ),
            auth=False,
        )
