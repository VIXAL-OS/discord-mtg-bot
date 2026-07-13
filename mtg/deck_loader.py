"""DeckLoader — fetches and parses decks from various sources.

Supports Archidekt URLs, JSON deck format, and plain text decklists.
Caches Scryfall card data to disk to avoid hammering the API on every
deck load. Handles modal/double-faced cards (DFCs), adventure split,
and standard split layouts.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

import asyncio
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

from mtg.constants import MDFC_PATHWAYS
from mtg.models import Card


# =============================================================================
# DECK LOADER
# =============================================================================

class DeckLoader:
    """Load decks from various sources."""
    
    SCRYFALL_API = "https://api.scryfall.com"
    ARCHIDEKT_API = "https://archidekt.com/api/decks"
    # Global rate limiter: serialized with 100ms minimum gap (Scryfall limit is 10/s)
    _scryfall_lock = None       # asyncio.Lock — serialize all requests
    _scryfall_last_req = 0.0    # time.monotonic() of last request
    _scryfall_session = None
    _disk_cache_lock = None
    
    # NOTE: __file__ is mtg/deck_loader.py after the Phase 1 OSS-readability
    # split, so we go up TWO levels (mtg/deck_loader.py -> mtg/ -> project root)
    # to reach the data/ directory. Same fix applied across all `__file__`-based
    # path references in mtg/cog.py.
    CARD_DATA_CACHE_PATH = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "card_data_cache.json"
    )

    def __init__(self):
        self.card_cache: Dict[str, Dict] = {}
        self._load_disk_cache()

    def _load_disk_cache(self):
        """Load card data cache from disk if it exists."""
        try:
            if os.path.exists(self.CARD_DATA_CACHE_PATH):
                import json as _json
                with open(self.CARD_DATA_CACHE_PATH, 'r', encoding='utf-8') as f:
                    disk_data = _json.load(f)
                self.card_cache.update(disk_data)
                print(f"[SCRYFALL] Loaded {len(disk_data)} cards from disk cache")
        except Exception as e:
            print(f"[SCRYFALL] Disk cache load error (will re-fetch): {e}")

    def _save_disk_cache(self):
        """Save card data cache to disk."""
        try:
            import json as _json
            os.makedirs(os.path.dirname(self.CARD_DATA_CACHE_PATH), exist_ok=True)
            with open(self.CARD_DATA_CACHE_PATH, 'w', encoding='utf-8') as f:
                _json.dump(self.card_cache, f, ensure_ascii=False, indent=1)
            print(f"[SCRYFALL] Saved {len(self.card_cache)} cards to disk cache")
        except Exception as e:
            print(f"[SCRYFALL] Disk cache save error: {e}")

    @classmethod
    def _write_disk_cache_snapshot(cls, snapshot: Dict[str, Dict]):
        """Blocking JSON write used only behind ``asyncio.to_thread``."""
        import json as _json
        os.makedirs(os.path.dirname(cls.CARD_DATA_CACHE_PATH), exist_ok=True)
        with open(cls.CARD_DATA_CACHE_PATH, 'w', encoding='utf-8') as f:
            _json.dump(snapshot, f, ensure_ascii=False, indent=1)

    async def _save_disk_cache_async(self):
        """Serialize cache writes without blocking the Discord event loop."""
        if DeckLoader._disk_cache_lock is None:
            DeckLoader._disk_cache_lock = asyncio.Lock()
        async with DeckLoader._disk_cache_lock:
            snapshot = dict(self.card_cache)
            try:
                await asyncio.to_thread(self._write_disk_cache_snapshot, snapshot)
                print(f"[SCRYFALL] Saved {len(snapshot)} cards to disk cache")
            except (OSError, TypeError, ValueError) as e:
                print(f"[SCRYFALL] Disk cache save error: {e}")
    
    async def load_from_archidekt(self, deck_id: str) -> Tuple[List[Card], str, Optional[Card], Optional[Card]]:
        """
        Load deck from Archidekt.
        Returns (cards, deck_name, commander or None, signature_spell or None)
        """
        async with aiohttp.ClientSession() as session:
            url = f"{self.ARCHIDEKT_API}/{deck_id}/"
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ValueError(f"Failed to load deck from Archidekt: {resp.status}")
                data = await resp.json()

        deck_name = data.get("name", "Unknown Deck")
        cards = []
        commander = None
        signature_spell = None

        for card_entry in data.get("cards", []):
            card_data = card_entry.get("card", {})
            quantity = card_entry.get("quantity", 1)
            categories = card_entry.get("categories", [])

            card_name = card_data.get("oracleCard", {}).get("name", "Unknown")

            # Fetch full card data from Scryfall
            scryfall_data = await self.fetch_card_data(card_name)

            for _ in range(quantity):
                card = Card(
                    name=card_name,
                    mana_cost=scryfall_data.get("mana_cost", ""),
                    type_line=scryfall_data.get("type_line", ""),
                    oracle_text=scryfall_data.get("oracle_text", ""),
                    power=scryfall_data.get("power"),
                    toughness=scryfall_data.get("toughness"),
                    loyalty=scryfall_data.get("loyalty"),
                    keywords=scryfall_data.get("keywords", []),
                    # MDFC / transform / split fix: Scryfall's `color_identity`
                    # is the union across all card faces (CR 903.4 — for
                    # commander legality, color identity is computed from
                    # both faces of an MDFC like Jorn // Kaldring). Without
                    # this, FormatValidator.get_color_identity falls back
                    # to parsing the front face's mana cost only, which
                    # broke 62 things in the snow deck (Jorn read as
                    # mono-G instead of Sultai). Scryfall returns this
                    # field as a list of single-letter color codes.
                    color_identity=list(scryfall_data.get("color_identity", []) or []),
                )
                # `colors` is read via getattr() in mtg/models.py:2702 for
                # the layers engine (color-qualified anthems). Set it as
                # an instance attribute since Card doesn't declare it.
                card.colors = list(scryfall_data.get("colors", []) or [])
                # Extract adventure/split/transform data from Scryfall card_faces
                self._extract_adventure_data(card, scryfall_data)
                self._extract_split_data(card, scryfall_data)
                self._extract_transform_data(card, scryfall_data)
                cards.append(card)

                # Check if commander
                if "Commander" in categories:
                    commander = card
                # Check if signature spell (Oathbreaker format)
                if "Signature Spell" in categories:
                    signature_spell = card
        
        return cards, deck_name, commander, signature_spell

    async def load_from_json(self, json_data: Dict) -> Tuple[List[Card], str, Optional[Card], Optional[Card]]:
        """
        Load deck from JSON format.

        Expected format:
        {
            "name": "Deck Name",
            "format": "commander",
            "commander": "Card Name",  // optional
            "signature_spell": "Spell Name",  // optional, for oathbreaker
            "cards": [
                {"name": "Island", "quantity": 35},
                {"name": "Counterspell", "quantity": 1}
            ]
        }
        """
        deck_name = json_data.get("name", "Unknown Deck")
        cards = []
        commander = None
        signature_spell = None
        commander_name = json_data.get("commander")
        signature_spell_name = json_data.get("signature_spell")
        
        for card_entry in json_data.get("cards", []):
            card_name = card_entry.get("name", "Unknown")
            quantity = card_entry.get("quantity", 1)
            
            scryfall_data = await self.fetch_card_data(card_name)
            
            for _ in range(quantity):
                card = Card(
                    name=card_name,
                    mana_cost=scryfall_data.get("mana_cost", ""),
                    type_line=scryfall_data.get("type_line", ""),
                    oracle_text=scryfall_data.get("oracle_text", ""),
                    power=scryfall_data.get("power"),
                    toughness=scryfall_data.get("toughness"),
                    loyalty=scryfall_data.get("loyalty"),
                    keywords=scryfall_data.get("keywords", []),
                    # MDFC / transform / split fix: Scryfall's `color_identity`
                    # is the union across all card faces (CR 903.4 — for
                    # commander legality, color identity is computed from
                    # both faces of an MDFC like Jorn // Kaldring). Without
                    # this, FormatValidator.get_color_identity falls back
                    # to parsing the front face's mana cost only, which
                    # broke 62 things in the snow deck (Jorn read as
                    # mono-G instead of Sultai). Scryfall returns this
                    # field as a list of single-letter color codes.
                    color_identity=list(scryfall_data.get("color_identity", []) or []),
                )
                # `colors` is read via getattr() in mtg/models.py:2702 for
                # the layers engine (color-qualified anthems). Set it as
                # an instance attribute since Card doesn't declare it.
                card.colors = list(scryfall_data.get("colors", []) or [])
                # Extract adventure/split/transform data from Scryfall card_faces
                self._extract_adventure_data(card, scryfall_data)
                self._extract_split_data(card, scryfall_data)
                self._extract_transform_data(card, scryfall_data)
                cards.append(card)

                if commander_name and card_name.lower() == commander_name.lower():
                    commander = card
                if signature_spell_name and card_name.lower() == signature_spell_name.lower():
                    signature_spell = card

        # If commander was declared but not found in the cards list, create it separately.
        # This handles deck formats where the commander is listed only in the "commander"
        # field and not duplicated in the "cards" array (which is the normal convention).
        if commander_name and not commander:
            print(f"[COMMANDER] Commander '{commander_name}' not in cards list, fetching from Scryfall...")
            scryfall_data = await self.fetch_card_data(commander_name)
            if scryfall_data and scryfall_data.get("type_line", "") != "":
                commander = Card(
                    name=commander_name,
                    mana_cost=scryfall_data.get("mana_cost", ""),
                    type_line=scryfall_data.get("type_line", ""),
                    oracle_text=scryfall_data.get("oracle_text", ""),
                    power=scryfall_data.get("power"),
                    toughness=scryfall_data.get("toughness"),
                    loyalty=scryfall_data.get("loyalty"),
                    keywords=scryfall_data.get("keywords", []),
                )
                self._extract_adventure_data(commander, scryfall_data)
                self._extract_split_data(commander, scryfall_data)
                self._extract_transform_data(commander, scryfall_data)
                cards.append(commander)
                print(f"[COMMANDER] Created {commander_name} and added to deck")
            else:
                print(f"[COMMANDER] WARNING: Could not fetch '{commander_name}' from Scryfall!")

        # Same for signature spell (oathbreaker format)
        if signature_spell_name and not signature_spell:
            print(f"[OATHBREAKER] Signature spell '{signature_spell_name}' not in cards list, fetching from Scryfall...")
            scryfall_data = await self.fetch_card_data(signature_spell_name)
            if scryfall_data and scryfall_data.get("type_line", "") != "":
                signature_spell = Card(
                    name=signature_spell_name,
                    mana_cost=scryfall_data.get("mana_cost", ""),
                    type_line=scryfall_data.get("type_line", ""),
                    oracle_text=scryfall_data.get("oracle_text", ""),
                    power=scryfall_data.get("power"),
                    toughness=scryfall_data.get("toughness"),
                    loyalty=scryfall_data.get("loyalty"),
                    keywords=scryfall_data.get("keywords", []),
                )
                self._extract_adventure_data(signature_spell, scryfall_data)
                self._extract_split_data(signature_spell, scryfall_data)
                self._extract_transform_data(signature_spell, scryfall_data)
                cards.append(signature_spell)
                print(f"[OATHBREAKER] Created {signature_spell_name} and added to deck")
            else:
                print(f"[OATHBREAKER] WARNING: Could not fetch '{signature_spell_name}' from Scryfall!")

        # Save disk cache after loading a deck (catches any new fetches)
        await self._save_disk_cache_async()

        return cards, deck_name, commander, signature_spell

    async def load_from_text(self, text: str, deck_name: str = "Text Deck") -> Tuple[List[Card], str, Optional[Card], Optional[Card]]:
        """
        Load deck from text format (one card per line, optional quantity prefix).
        
        Example:
        4 Lightning Bolt
        4 Counterspell
        20 Island
        """
        cards = []
        
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            
            # Parse "4 Lightning Bolt" or just "Lightning Bolt"
            match = re.match(r'^(\d+)\s+(.+)$', line)
            if match:
                quantity = int(match.group(1))
                card_name = match.group(2)
            else:
                quantity = 1
                card_name = line
            
            scryfall_data = await self.fetch_card_data(card_name)
            
            for _ in range(quantity):
                card = Card(
                    name=card_name,
                    mana_cost=scryfall_data.get("mana_cost", ""),
                    type_line=scryfall_data.get("type_line", ""),
                    oracle_text=scryfall_data.get("oracle_text", ""),
                    power=scryfall_data.get("power"),
                    toughness=scryfall_data.get("toughness"),
                    loyalty=scryfall_data.get("loyalty"),
                    keywords=scryfall_data.get("keywords", []),
                    # MDFC / transform / split fix: Scryfall's `color_identity`
                    # is the union across all card faces (CR 903.4 — for
                    # commander legality, color identity is computed from
                    # both faces of an MDFC like Jorn // Kaldring). Without
                    # this, FormatValidator.get_color_identity falls back
                    # to parsing the front face's mana cost only, which
                    # broke 62 things in the snow deck (Jorn read as
                    # mono-G instead of Sultai). Scryfall returns this
                    # field as a list of single-letter color codes.
                    color_identity=list(scryfall_data.get("color_identity", []) or []),
                )
                # `colors` is read via getattr() in mtg/models.py:2702 for
                # the layers engine (color-qualified anthems). Set it as
                # an instance attribute since Card doesn't declare it.
                card.colors = list(scryfall_data.get("colors", []) or [])
                cards.append(card)
        
        return cards, deck_name, None, None

    def _extract_adventure_data(self, card: Card, scryfall_data: Dict):
        """Extract adventure face data from Scryfall response if the card has one."""
        if scryfall_data.get("layout") != "adventure":
            return
        card_faces = scryfall_data.get("card_faces", [])
        if len(card_faces) < 2:
            return
        # Face 0 is the creature, face 1 is the adventure.
        creature_face = card_faces[0]
        adventure_face = card_faces[1]

        # [FIX-9] Guard against adventure misclassification.
        # Scryfall fuzzy search can return "Grave Researcher // Reanimate" when
        # the deck requests "Reanimate" (the classic 1993 sorcery). In that case,
        # card.name == "Reanimate" matches the adventure face name, not the creature
        # face name — this is the wrong card. Only apply adventure data when the
        # card we loaded matches the creature face (face 0).
        creature_face_name = creature_face.get("name", "")
        if creature_face_name and card.name.lower() != creature_face_name.lower():
            # card.name matches the adventure half, not the creature half.
            # This means Scryfall returned a different card (e.g., Grave Researcher for Reanimate).
            # Do NOT set adventure properties on this card.
            print(f"[ADVENTURE] Skipping adventure data for '{card.name}' — Scryfall returned "
                  f"'{creature_face_name} // {adventure_face.get('name', '')}' (wrong card via fuzzy match)")
            return

        card.adventure_name = adventure_face.get("name", "")
        card.adventure_cost = adventure_face.get("mana_cost", "")
        card.adventure_text = adventure_face.get("oracle_text", "")
        card.adventure_type = adventure_face.get("type_line", "")
        print(f"[ADVENTURE] Loaded adventure for {card.name}: {card.adventure_name}")

    def _extract_split_data(self, card: Card, scryfall_data: Dict):
        """Extract split card face data from Scryfall response (Commit // Memory, etc.)."""
        if scryfall_data.get("layout") != "split":
            return
        card_faces = scryfall_data.get("card_faces", [])
        if len(card_faces) < 2:
            return
        card.split_names = [f.get("name", "") for f in card_faces]
        card.split_costs = [f.get("mana_cost", "") for f in card_faces]
        card.split_types = [f.get("type_line", "") for f in card_faces]
        card.split_texts = [f.get("oracle_text", "") for f in card_faces]
        print(f"[SPLIT] Loaded split card: {' // '.join(card.split_names)} ({' / '.join(card.split_types)})")

    def _extract_transform_data(self, card: Card, scryfall_data: Dict):
        """Extract transform DFC back face data from Scryfall response.

        Handles layout: "transform" (Innistrad werewolves, flip walkers, etc.)
        and layout: "modal_dfc" that are NOT pathway lands (e.g., Valki // Tibalt).
        Pathway MDFCs are handled separately via MDFC_PATHWAYS dict.
        """
        layout = scryfall_data.get("layout", "")
        if layout not in ("transform", "modal_dfc"):
            return
        card_faces = scryfall_data.get("card_faces", [])
        if len(card_faces) < 2:
            return
        # Skip pathway MDFCs — those are handled by the MDFC_PATHWAYS system
        if layout == "modal_dfc" and card.name.lower() in MDFC_PATHWAYS:
            return
        back_face = card_faces[1]
        card.has_transform = True
        card.front_face_name = card.name
        card.back_face_name = back_face.get("name", "")
        card.back_face_type_line = back_face.get("type_line", "")
        card.back_face_oracle_text = back_face.get("oracle_text", "")
        card.back_face_power = back_face.get("power", "")
        card.back_face_toughness = back_face.get("toughness", "")
        card.back_face_mana_cost = back_face.get("mana_cost", "")
        # Defensive fallback: if the Scryfall top-level color_identity didn't
        # populate (older cache entries, partial responses, etc.), union in
        # the back face's mana symbols so MDFCs like Jorn // Kaldring don't
        # report as mono-front-face for color identity. Scryfall normally
        # provides color_identity unioned at the top level, but the cache
        # might predate this fix.
        import re as _re
        existing_ci = set(card.color_identity or [])
        back_cost = back_face.get("mana_cost", "") or ""
        back_oracle = back_face.get("oracle_text", "") or ""
        for sym in _re.findall(r'\{([^}]+)\}', back_cost + back_oracle):
            for part in sym.split('/'):
                part = part.replace('P', '')
                if part in 'WUBRG':
                    existing_ci.add(part)
        # Also union the per-face `color_identity` if Scryfall provides it
        for face in card_faces:
            for c in (face.get("color_identity") or []):
                if c in 'WUBRG':
                    existing_ci.add(c)
        card.color_identity = sorted(existing_ci)
        print(f"[TRANSFORM] Loaded DFC: {card.name} // {card.back_face_name} (layout: {layout}, identity: {{{','.join(card.color_identity) or '∅'}}})")

    async def fetch_card_data(self, card_name: str) -> Dict:
        """Fetch card data from Scryfall with caching, rate limiting, and retry.

        Scryfall rate limits at ~10 req/s. Concurrent deck loading (100+ games)
        can easily exceed this. Uses a semaphore to cap concurrent requests at 5
        and a shared session to avoid connection overhead.
        """
        cache_key = card_name.lower()
        if cache_key in self.card_cache:
            cached = self.card_cache[cache_key]
            cached_name = cached.get("name", "")
            # Reject stale fuzzy-match cache entries: if the cached card is a
            # split/adventure/DFC ("X // Y") and the requested name doesn't
            # match the front face, the cache was poisoned by an old fuzzy
            # match (e.g., "Reanimate" → "Grave Researcher // Reanimate").
            if "//" in cached_name:
                front_name = cached_name.split("//")[0].strip().lower()
                if cache_key != cached_name.lower() and cache_key != front_name:
                    print(f"[SCRYFALL] Discarding stale fuzzy-cache entry for '{card_name}' "
                          f"(cached as '{cached_name}') — refetching with exact match")
                    del self.card_cache[cache_key]
                else:
                    return cached
            else:
                return cached

        # Initialize lock and shared session lazily (must be in async context)
        if DeckLoader._scryfall_lock is None:
            DeckLoader._scryfall_lock = asyncio.Lock()
        if DeckLoader._scryfall_session is None or DeckLoader._scryfall_session.closed:
            DeckLoader._scryfall_session = aiohttp.ClientSession()

        max_retries = 7
        for attempt in range(max_retries):
            status = None
            try:
                # Serialize all Scryfall requests with minimum 110ms gap
                async with DeckLoader._scryfall_lock:
                    import time
                    elapsed = time.monotonic() - DeckLoader._scryfall_last_req
                    if elapsed < 0.15:
                        await asyncio.sleep(0.15 - elapsed)
                    DeckLoader._scryfall_last_req = time.monotonic()

                    session = DeckLoader._scryfall_session
                    url = f"{self.SCRYFALL_API}/cards/named"
                    # Try exact first — fuzzy can pick wrong card (e.g.
                    # "Reanimate" → "Grave Researcher // Reanimate" adventure).
                    # On 404, fall back to fuzzy.
                    data = None
                    async with session.get(url, params={"exact": card_name}) as resp:
                        status = resp.status
                        if status == 200:
                            data = await resp.json()
                        elif status not in (404, 429) and status < 500:
                            error_text = await resp.text()
                            print(f"[SCRYFALL] {status} for {card_name} (exact): {error_text}")

                    if data is None and status == 404:
                        async with session.get(url, params={"fuzzy": card_name}) as resp:
                            status = resp.status
                            if status == 200:
                                data = await resp.json()
                            elif status not in (429,) and status < 500:
                                error_text = await resp.text()
                                print(f"[SCRYFALL] {status} for {card_name} (fuzzy): {error_text}")
                                break

                    if data is not None:
                        # Handle double-faced / modal cards (Vivien, MDFCs, etc.)
                        card_faces = data.get("card_faces", [])
                        if card_faces:
                            front_face = card_faces[0]
                            if not data.get("mana_cost") and front_face.get("mana_cost"):
                                data["mana_cost"] = front_face["mana_cost"]
                                print(f"[SCRYFALL] Merged front face mana_cost for {card_name}: {front_face['mana_cost']}")
                            if not data.get("oracle_text") and front_face.get("oracle_text"):
                                data["oracle_text"] = front_face["oracle_text"]
                            # ALWAYS use front face type_line for DFCs — the combined
                            # "Enchantment // Land" breaks is_land() classification
                            if front_face.get("type_line"):
                                top_type = data.get("type_line", "")
                                if '//' in top_type and front_face["type_line"] != top_type:
                                    data["type_line"] = front_face["type_line"]
                                    print(f"[SCRYFALL] DFC type_line override for {card_name}: '{top_type}' → '{front_face['type_line']}'")
                                elif not top_type:
                                    data["type_line"] = front_face["type_line"]
                            if not data.get("power") and front_face.get("power"):
                                data["power"] = front_face["power"]
                            if not data.get("toughness") and front_face.get("toughness"):
                                data["toughness"] = front_face["toughness"]
                            if not data.get("loyalty") and front_face.get("loyalty"):
                                data["loyalty"] = front_face["loyalty"]
                            # May 13 audit: DFCs (Arlinn Kord etc.) have empty
                            # top-level `colors` — the color info lives on
                            # card_faces[0]. Without this fallback, the cache
                            # stored `colors=[]` for ~30 DFCs, breaking
                            # color-identity / color-anthem / protection-from-
                            # color checks. Same for `keywords`.
                            if not data.get("colors") and front_face.get("colors"):
                                data["colors"] = front_face["colors"]
                            if not data.get("keywords") and front_face.get("keywords"):
                                data["keywords"] = front_face["keywords"]

                        self.card_cache[cache_key] = data
                        # Periodically save to disk (every 50 new fetches)
                        if len(self.card_cache) % 50 == 0:
                            await self._save_disk_cache_async()
                        return data
                    elif status == 429 or (status is not None and status >= 500):
                        pass  # Will retry after releasing lock
            except Exception as e:
                print(f"Scryfall fetch error for {card_name}: {e}")

            # Backoff OUTSIDE the lock so other requests can proceed
            if status == 429 or status is None or (status is not None and status >= 500):
                delay = min(2.0 * (2 ** attempt), 60.0)  # 2s, 4s, 8s, 16s, 32s, max 60s
                if status:
                    print(f"[SCRYFALL] {status} for {card_name}, retry {attempt + 1}/{max_retries} in {delay}s")
                await asyncio.sleep(delay)

        # Return minimal data if fetch fails
        print(f"[SCRYFALL] WARNING: Failed to fetch {card_name} after {max_retries} attempts — type_line will be empty!")
        return {"name": card_name}

    async def fetch_card_data_bulk(self, card_names: List[str]) -> int:
        """Bulk-fetch card data using Scryfall's /cards/collection endpoint.

        Fetches up to 75 cards per request (Scryfall's limit).
        Automatically filters out cards already in cache.
        Returns count of newly fetched cards.

        POST https://api.scryfall.com/cards/collection
        Body: {"identifiers": [{"name": "Lightning Bolt"}, ...]}
        """
        # Filter to uncached cards only
        uncached = []
        for name in card_names:
            if name and name.lower() not in self.card_cache:
                uncached.append(name)
        if not uncached:
            return 0

        # Deduplicate
        seen = set()
        unique_uncached = []
        for name in uncached:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                unique_uncached.append(name)

        # Initialize session
        if DeckLoader._scryfall_lock is None:
            DeckLoader._scryfall_lock = asyncio.Lock()
        if DeckLoader._scryfall_session is None or DeckLoader._scryfall_session.closed:
            DeckLoader._scryfall_session = aiohttp.ClientSession()

        fetched = 0
        # Process in chunks of 75 (Scryfall collection limit)
        for chunk_start in range(0, len(unique_uncached), 75):
            chunk = unique_uncached[chunk_start:chunk_start + 75]
            identifiers = [{"name": name} for name in chunk]

            for attempt in range(5):
                try:
                    async with DeckLoader._scryfall_lock:
                        import time
                        elapsed = time.monotonic() - DeckLoader._scryfall_last_req
                        if elapsed < 0.15:
                            await asyncio.sleep(0.15 - elapsed)
                        DeckLoader._scryfall_last_req = time.monotonic()

                        url = f"{self.SCRYFALL_API}/cards/collection"
                        async with DeckLoader._scryfall_session.post(
                            url,
                            json={"identifiers": identifiers},
                            headers={"Content-Type": "application/json"}
                        ) as resp:
                            if resp.status == 200:
                                result = await resp.json()
                                cards = result.get("data", [])
                                not_found = result.get("not_found", [])

                                for data in cards:
                                    card_name = data.get("name", "")
                                    cache_key = card_name.lower()

                                    # Handle DFC: merge front face data
                                    card_faces = data.get("card_faces", [])
                                    if card_faces:
                                        front_face = card_faces[0]
                                        if not data.get("mana_cost") and front_face.get("mana_cost"):
                                            data["mana_cost"] = front_face["mana_cost"]
                                        if not data.get("oracle_text") and front_face.get("oracle_text"):
                                            data["oracle_text"] = front_face["oracle_text"]
                                        if front_face.get("type_line"):
                                            top_type = data.get("type_line", "")
                                            if '//' in top_type and front_face["type_line"] != top_type:
                                                data["type_line"] = front_face["type_line"]
                                            elif not top_type:
                                                data["type_line"] = front_face["type_line"]
                                        if not data.get("power") and front_face.get("power"):
                                            data["power"] = front_face["power"]
                                        if not data.get("toughness") and front_face.get("toughness"):
                                            data["toughness"] = front_face["toughness"]
                                        if not data.get("loyalty") and front_face.get("loyalty"):
                                            data["loyalty"] = front_face["loyalty"]

                                    self.card_cache[cache_key] = data
                                    fetched += 1

                                if not_found:
                                    nf_names = [nf.get("name", "?") for nf in not_found]
                                    print(f"[SCRYFALL-BULK] {len(not_found)} cards not found: {', '.join(nf_names[:10])}")

                                print(f"[SCRYFALL-BULK] Fetched {len(cards)}/{len(chunk)} cards in one request")
                                break  # Success — exit retry loop

                            elif resp.status == 429:
                                delay = min(2.0 * (2 ** attempt), 60.0)
                                print(f"[SCRYFALL-BULK] 429 rate limited, retry {attempt+1}/5 in {delay}s")
                                await asyncio.sleep(delay)
                            else:
                                error_text = await resp.text()
                                print(f"[SCRYFALL-BULK] {resp.status}: {error_text[:200]}")
                                break  # Non-retryable error

                except Exception as e:
                    print(f"[SCRYFALL-BULK] Error: {e}")
                    delay = min(2.0 * (2 ** attempt), 60.0)
                    await asyncio.sleep(delay)

            # Brief pause between chunks to be polite
            if chunk_start + 75 < len(unique_uncached):
                await asyncio.sleep(0.5)

        # Save cache to disk after bulk load
        if fetched > 0:
            await self._save_disk_cache_async()
            print(f"[SCRYFALL-BULK] Saved {len(self.card_cache)} cards to disk cache")

        return fetched
