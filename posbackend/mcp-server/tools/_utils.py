"""Tools ke darmiyan shared helpers (is module mein koi MCP tool register nahi hota)."""
from __future__ import annotations

import re
from urllib.parse import quote

# Server ke tables/services.py (_TOKEN_RE) jaisa: secrets.token_urlsafe ka alphabet.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def bare_token(raw: str) -> str:
    """
    QR payload ("/t/<token>"), full URL ya bare token -> bare token.

    Kai public endpoints token ko URL *path segment* mein lete hain, jahan "/" nahi chal sakta,
    is liye "/t/<token>" ko client side par normalise karte hain (server ka parse_qr_payload jaisa).
    """
    value = (raw or "").strip().split("?", 1)[0].split("#", 1)[0]
    marker = value.rfind("/t/")
    if marker != -1:
        value = value[marker + 3:]
    value = value.strip("/")
    if not value or not _TOKEN_RE.match(value):
        raise ValueError("Invalid table session token / QR payload")
    return value


def path_token(raw: str) -> str:
    """bare_token + URL-safe quoting, seedha path mein jorne ke liye."""
    return quote(bare_token(raw), safe="")


def location_payload(
    latitude: float | None,
    longitude: float | None,
    accuracy_m: float | None,
    location_timestamp: float | None = None,
) -> dict:
    """
    GPS fix ko backend ke `validate_branch_location` (common/geofence.py) ke format mein badalta hai.

    Backend ye 4 keys maangta hai: latitude, longitude, accuracy_m, location_timestamp
    (unix epoch SECONDS). Fix 2 minute se purana ya 30s se zyada future ka nahi hona chahiye
    (error.locationStale), aur `distance + accuracy_m` branch radius ke andar hona chahiye
    (error.outsideRestaurantLocation).

    * Teeno (latitude, longitude, accuracy_m) ek saath do, ya koi nahi -> {} (location skip).
    * `location_timestamp` na do to abhi ka waqt lagta hai (fix taaza maana jata hai).
    """
    import time

    given = [latitude, longitude, accuracy_m]
    if all(v is None for v in given):
        return {}
    if any(v is None for v in given):
        raise ValueError("latitude, longitude aur accuracy_m teeno ek saath dena zaroori hain")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_m": accuracy_m,
        "location_timestamp": location_timestamp if location_timestamp is not None else time.time(),
    }


def save_bytes(content: bytes, name: str, save_dir: str | None = None) -> dict:
    """Binary download (PDF/ZIP/CSV/XML) ko disk par save karta hai; chat mein bytes nahi aate.
    `save_dir` na ho to system temp ke 'pos-mcp' folder mein. {path, bytes} wapas."""
    import tempfile
    from pathlib import Path

    folder = Path(save_dir).expanduser() if save_dir else Path(tempfile.gettempdir()) / "pos-mcp"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(content)
    return {"path": str(target), "bytes": len(content)}
