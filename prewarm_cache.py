#!/usr/bin/env python3
"""Pre-warm the Scryfall card data cache for every discovered deck fixture.

Run this ONCE offline to populate data/card_data_cache.json with card data
from every JSON deck in data/. After this, the bot loads from disk on startup
and does not need to hit Scryfall for already-cached cards during autoplay.

Usage: python prewarm_cache.py
"""

import asyncio
import json
import os
import sys
import time

# Add the project root to the import path.
sys.path.insert(0, os.path.dirname(__file__))


async def main():
    # Collect all unique card names from all deck files
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    all_cards = set()

    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith('.json'):
            continue
        if fname in ('api_costs.json', 'memories.json', 'card_data_cache.json'):
            continue
        fpath = os.path.join(data_dir, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                deck = json.load(f)
            if not isinstance(deck, dict) or 'cards' not in deck:
                continue
            for c in deck['cards']:
                name = c.get('name', '') if isinstance(c, dict) else str(c)
                if name:
                    all_cards.add(name)
        except Exception:
            pass

    print(f"Found {len(all_cards)} unique cards across all decks")

    # Load existing cache
    cache_path = os.path.join(data_dir, "card_data_cache.json")
    existing = {}
    if os.path.exists(cache_path):
        with open(cache_path, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        print(f"Existing cache: {len(existing)} cards")

    # Find missing cards
    missing = [name for name in sorted(all_cards) if name.lower() not in existing]
    print(f"Missing from cache: {len(missing)} cards")

    if not missing:
        print("Cache is complete! No Scryfall requests needed.")
        return

    # Fetch missing cards from Scryfall with rate limiting
    import aiohttp
    session = aiohttp.ClientSession()
    fetched = 0
    failed = []

    try:
        for i, card_name in enumerate(missing):
            # Rate limit: 150ms between requests (~6.5/s, well under Scryfall's 10/s)
            if i > 0:
                await asyncio.sleep(0.15)

            cache_key = card_name.lower()
            success = False

            for attempt in range(5):
                try:
                    url = "https://api.scryfall.com/cards/named"
                    params = {"fuzzy": card_name}
                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Handle DFCs
                            card_faces = data.get("card_faces", [])
                            if card_faces:
                                front = card_faces[0]
                                if not data.get("mana_cost") and front.get("mana_cost"):
                                    data["mana_cost"] = front["mana_cost"]
                                if not data.get("oracle_text") and front.get("oracle_text"):
                                    data["oracle_text"] = front["oracle_text"]
                                if front.get("type_line"):
                                    top_type = data.get("type_line", "")
                                    if '//' in top_type:
                                        data["type_line"] = front["type_line"]
                                if not data.get("power") and front.get("power"):
                                    data["power"] = front["power"]
                                if not data.get("toughness") and front.get("toughness"):
                                    data["toughness"] = front["toughness"]
                                if not data.get("loyalty") and front.get("loyalty"):
                                    data["loyalty"] = front["loyalty"]

                            existing[cache_key] = data
                            fetched += 1
                            success = True
                            if fetched % 10 == 0 or fetched <= 3:
                                print(f"  [{fetched}/{len(missing)}] {card_name} OK")
                            break
                        elif resp.status == 429:
                            delay = 2.0 * (2 ** attempt)
                            print(f"  429 for {card_name}, waiting {delay}s...")
                            await asyncio.sleep(delay)
                        else:
                            print(f"  {resp.status} for {card_name}: {await resp.text()}")
                            break
                except Exception as e:
                    print(f"  Error fetching {card_name}: {e}")
                    await asyncio.sleep(2.0)

            if not success:
                failed.append(card_name)
    finally:
        await session.close()

    # Save to disk
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=1)

    print(f"\nDone! Fetched {fetched} cards, {len(failed)} failed")
    print(f"Total cache: {len(existing)} cards saved to {cache_path}")
    if failed:
        print(f"Failed cards: {', '.join(failed)}")


if __name__ == "__main__":
    asyncio.run(main())
