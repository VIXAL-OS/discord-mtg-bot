"""Scryfall's 2027 image migration, including legacy and double-faced data."""

import asyncio
import io

import pytest
from PIL import Image

from scryfall_utils import get_image_url


@pytest.mark.parametrize("old,new", [
    ("small", "thumb"),
    ("normal", "grid"),
    ("large", "display"),
    ("border_crop", "crop"),
    ("art_crop", "art"),
])
def test_new_image_fields_win_and_old_payloads_still_work(old, new):
    modern = f"https://cards.scryfall.io/{new}/front/card.webp"
    legacy = f"https://cards.scryfall.io/{old}/front/card.jpg"
    for size in (old, new):
        assert get_image_url({"image_uris": {new: modern}}, size) == modern
        assert get_image_url({"image_uris": {old: legacy}}, size) == legacy
        assert get_image_url({"image_uris": {new: modern, old: legacy}}, size) == modern


def test_webp_at_a_fallback_size_wins_over_legacy_high_resolution():
    card = {"image_uris": {"grid": "grid.webp", "large": "large.jpg"}}
    assert get_image_url(card, "display", "grid", "thumb") == "grid.webp"


def test_retained_jpeg_key_precedes_the_old_normal_key():
    card = {"image_uris": {"jpeg": "retained.jpg", "normal": "legacy.jpg"}}
    assert get_image_url(card) == "retained.jpg"


def test_double_faced_cards_use_the_front_face():
    card = {"image_uris": {}, "card_faces": [
        {"image_uris": {"display": "front.webp"}},
        {"image_uris": {"display": "back.webp"}},
    ]}
    assert get_image_url(card, "large") == "front.webp"


def test_missing_images_do_not_fall_back_to_png_hotlinks():
    for card in ({}, {"card_faces": []}, {"image_uris": {"png": "scan.png"}}):
        assert get_image_url(card) is None
    assert get_image_url({"image_uris": {"png": "scan.png"}}, "png") == "scan.png"


def test_board_fetches_webp_with_identifying_headers_and_caches_png(monkeypatch, tmp_path):
    import board_visual

    source = io.BytesIO()
    Image.new("RGB", (3, 4), (12, 34, 56)).save(source, "WEBP", lossless=True)
    image_url = "https://cards.scryfall.io/display/front/card.webp"
    requests = []

    class Response:
        status = 200

        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"image_uris": {"display": image_url}}

        async def read(self):
            assert self.url == image_url
            return source.getvalue()

    class Session:
        closed = False

        def __init__(self, *, headers):
            self.headers = headers

        def get(self, url, **kwargs):
            requests.append((url, dict(self.headers)))
            return Response(url)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(board_visual, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(board_visual.aiohttp, "ClientSession", Session)

    async def fetch():
        fetcher = board_visual.CardImageFetcher()
        try:
            image = await fetcher.get_card_image("Mock Card")
            assert image.size == (3, 4)
            assert image.getpixel((0, 0)) == (12, 34, 56)
            with Image.open(fetcher._cache_path("Mock Card")) as cached:
                assert cached.format == "PNG"
            assert (await fetcher.get_card_image("Mock Card")).size == (3, 4)
        finally:
            await fetcher.close()

    asyncio.run(fetch())
    assert len(requests) == 2  # API + WebP download; the second lookup uses disk.
    assert requests[1][0] == image_url
    # Compare against the shared constant rather than a literal: each
    # deployment names its own app in the User-Agent (the public fork ships a
    # different one), and what this pins is that EVERY request carries it.
    from scryfall_utils import SCRYFALL_HEADERS
    assert "/" in SCRYFALL_HEADERS["User-Agent"]  # name/version, per Scryfall
    for _, headers in requests:
        assert headers["User-Agent"] == SCRYFALL_HEADERS["User-Agent"]
        assert "*/*" in headers["Accept"]
