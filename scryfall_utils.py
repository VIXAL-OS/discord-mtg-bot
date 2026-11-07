"""Shared Scryfall request headers and transition-safe image selection."""

from typing import Optional


SCRYFALL_HEADERS = {
    "User-Agent": "discord-mtg-bot/1.0",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}

# Scryfall retires these image keys after January 1, 2027. Accept old size
# arguments too, so existing callers get WebP without changing their API.
_IMAGE_ALIASES = {
    "small": "thumb",
    "normal": "grid",
    "large": "display",
    "border_crop": "crop",
    "art_crop": "art",
}
_IMAGE_FALLBACKS = {
    "thumb": ("small",),
    "grid": ("jpeg", "normal"),
    "display": ("large",),
    "crop": ("border_crop",),
    "art": ("art_crop",),
    "jpeg": ("normal",),
}


def get_image_url(card_data: dict, size: str = "grid", *fallback_sizes: str) -> Optional[str]:
    """Prefer WebP at the requested sizes, then JPEG/legacy fields.

    PNG is only selected when explicitly requested, since Scryfall no longer
    permits PNG hotlinks after the transition.
    """
    faces = card_data.get("card_faces") or []
    uris = card_data.get("image_uris") or (faces[0].get("image_uris") if faces else {}) or {}
    sizes = [_IMAGE_ALIASES.get(value, value) for value in (size, *fallback_sizes)]
    keys = sizes + [key for value in sizes for key in _IMAGE_FALLBACKS.get(value, ())]
    return next((uris[key] for key in keys if uris.get(key)), None)
