"""
XMage Rules Bridge - Python Client
===================================

Communicates with the XMage Java rules engine via JSON-RPC over stdin/stdout.

Usage:
    from xmage_bridge import XMageBridge
    
    async with XMageBridge() as xmage:
        # Look up a card
        card = await xmage.lookup("Lightning Bolt")
        print(card['manaCost'], card['keywords'])
        
        # Check if an action is legal
        result = await xmage.validate_cast("Lightning Bolt", game_state)
        
        # Get triggered abilities for an event
        triggers = await xmage.get_triggers("enters", game_state)
        
        # Resolve combat
        damage = await xmage.resolve_combat(attackers, blockers, game_state)
"""

import asyncio
import json
import subprocess
import os
from typing import Optional, Dict, List, Any, Union
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Permanent:
    """Represents a permanent on the battlefield."""
    name: str
    controller: str = "playerA"
    is_creature: bool = False
    is_legendary: bool = False
    power: int = 0
    toughness: int = 0
    power_modifier: int = 0
    toughness_modifier: int = 0
    plus_counters: int = 0
    minus_counters: int = 0
    damage_marked: int = 0
    tapped: bool = False
    summoning_sick: bool = True
    keywords: List[str] = field(default_factory=list)
    
    def to_json(self) -> dict:
        return {
            "name": self.name,
            "controller": self.controller,
            "isCreature": self.is_creature,
            "isLegendary": self.is_legendary,
            "power": self.power,
            "toughness": self.toughness,
            "powerModifier": self.power_modifier,
            "toughnessModifier": self.toughness_modifier,
            "plusCounters": self.plus_counters,
            "minusCounters": self.minus_counters,
            "damageMarked": self.damage_marked,
            "tapped": self.tapped,
            "summoningSick": self.summoning_sick,
            "keywords": self.keywords
        }


@dataclass
class GameState:
    """Represents the current game state."""
    active_player: str = "playerA"
    phase: str = "main1"  # untap, upkeep, draw, main1, combat, main2, end
    stack_size: int = 0
    player_life: Dict[str, int] = field(default_factory=lambda: {"playerA": 20, "playerB": 20})
    poison_counters: Dict[str, int] = field(default_factory=lambda: {"playerA": 0, "playerB": 0})
    battlefield: List[Permanent] = field(default_factory=list)
    hands: Dict[str, List[str]] = field(default_factory=dict)
    graveyards: Dict[str, List[str]] = field(default_factory=dict)
    untapped_lands: Dict[str, int] = field(default_factory=lambda: {"playerA": 0, "playerB": 0})

    def to_json(self) -> dict:
        return {
            "activePlayer": self.active_player,
            "phase": self.phase,
            "stackSize": self.stack_size,
            "life": self.player_life,
            "poison": self.poison_counters,
            "battlefield": [p.to_json() for p in self.battlefield],
            "hands": self.hands,
            "graveyards": self.graveyards,
            "lands": self.untapped_lands
        }
    
    @classmethod
    def from_json(cls, data: dict) -> 'GameState':
        state = cls()
        state.active_player = data.get("activePlayer", "playerA")
        state.phase = data.get("phase", "main1")
        state.stack_size = data.get("stackSize", 0)
        state.player_life = data.get("life", {"playerA": 20, "playerB": 20})
        state.poison_counters = data.get("poison", {"playerA": 0, "playerB": 0})
        state.hands = data.get("hands", {})
        state.graveyards = data.get("graveyards", {})
        state.untapped_lands = data.get("lands", {"playerA": 0, "playerB": 0})
        
        battlefield = []
        for p_data in data.get("battlefield", []):
            perm = Permanent(
                name=p_data.get("name", ""),
                controller=p_data.get("controller", "playerA"),
                is_creature=p_data.get("isCreature", False),
                is_legendary=p_data.get("isLegendary", False),
                power=p_data.get("power", 0),
                toughness=p_data.get("toughness", 0),
                damage_marked=p_data.get("damageMarked", 0),
                tapped=p_data.get("tapped", False),
                summoning_sick=p_data.get("summoningSick", True),
                keywords=p_data.get("keywords", [])
            )
            battlefield.append(perm)
        state.battlefield = battlefield
        
        return state


class XMageBridgeError(Exception):
    """Error from XMage bridge."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class XMageBridge:
    """
    Python client for XMage rules engine.
    
    Spawns a Java subprocess and communicates via JSON over stdin/stdout.
    """
    
    def __init__(
        self, 
        jar_path: Optional[str] = None,
        java_path: str = "java",
        xmage_path: Optional[str] = None
    ):
        """
        Initialize the bridge.
        
        Args:
            jar_path: Path to xmage-rules-bridge.jar
            java_path: Path to Java executable
            xmage_path: Path to XMage installation (for card database)
        """
        self.jar_path = jar_path or self._find_jar()
        self.java_path = java_path
        self.xmage_path = xmage_path
        self.process: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._ready = False
    
    def _find_jar(self) -> str:
        """Find the bridge JAR file."""
        # Look in common locations
        locations = [
            Path(__file__).parent / "xmage-bridge" / "target" / "xmage-bridge-1.0.0.jar",
            Path(__file__).parent.parent / "java" / "target" / "xmage-rules-bridge-1.0.0.jar",
            Path.cwd() / "xmage-rules-bridge.jar",
            Path.home() / ".local" / "lib" / "xmage-rules-bridge.jar"
        ]
        
        for loc in locations:
            if loc.exists():
                return str(loc)
        
        raise FileNotFoundError(
            "Could not find xmage-rules-bridge.jar. "
            "Please build it with 'mvn package' or specify jar_path."
        )
    
    async def __aenter__(self) -> 'XMageBridge':
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
    
    async def start(self) -> None:
        """Start the Java bridge process."""
        if self.process is not None:
            return
        
        cmd = [self.java_path, "-jar", self.jar_path]
        
        if self.xmage_path:
            cmd.extend(["-Dxmage.path=" + self.xmage_path])
        
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        
        # Wait for ready signal - may take a moment if card DB is initializing
        # Keep reading lines until we get valid JSON with status=ready
        max_attempts = 30  # Give it up to 30 seconds
        for _ in range(max_attempts):
            ready_line = await asyncio.to_thread(self.process.stdout.readline)
            
            if not ready_line:
                # Process died
                stderr_output = self.process.stderr.read()
                raise XMageBridgeError("startup_failed", f"Bridge died during startup: {stderr_output[:500]}")
            
            ready_line = ready_line.strip()
            if not ready_line:
                continue  # Empty line, keep waiting
            
            # Try to parse as JSON
            try:
                ready = json.loads(ready_line)
                if ready.get("status") == "ready":
                    self._ready = True
                    print(f"XMage Rules Bridge v{ready.get('version', '?')} ready")
                    return
            except json.JSONDecodeError:
                # Not JSON yet (probably stderr leaking), keep waiting
                continue
        
        raise XMageBridgeError("startup_failed", "Bridge timed out waiting for ready signal")
    
    async def stop(self) -> None:
        """Stop the Java bridge process."""
        if self.process is None:
            return
        
        self.process.stdin.close()
        self.process.terminate()
        
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.process.wait),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            self.process.kill()
        
        self.process = None
        self._ready = False
    
    async def _send_command(self, cmd: str, **kwargs) -> dict:
        """Send a command to the bridge and get response."""
        if not self._ready:
            raise XMageBridgeError("not_ready", "Bridge not started")
        
        request = {"cmd": cmd, **kwargs}
        request_json = json.dumps(request)
        
        async with self._lock:
            # Send request
            self.process.stdin.write(request_json + "\n")
            self.process.stdin.flush()
            
            # Read response
            response_line = await asyncio.to_thread(self.process.stdout.readline)
            
            if not response_line:
                raise XMageBridgeError("bridge_died", "Bridge process terminated")
            
            response_line = response_line.strip()
            
            try:
                response = json.loads(response_line)
            except json.JSONDecodeError as e:
                raise XMageBridgeError("parse_error", f"Invalid JSON response: {response_line[:200]}")
        
        if not response.get("success"):
            raise XMageBridgeError(
                response.get("error", "unknown"),
                response.get("message", "Unknown error")
            )
        
        return response.get("data", {})
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    async def ping(self) -> bool:
        """Check if bridge is responsive."""
        try:
            result = await self._send_command("ping")
            return result.get("pong", False)
        except Exception:
            return False
    
    async def lookup(self, card_name: str) -> dict:
        """
        Look up a card by name.
        
        Returns:
            Dict with card properties:
            - name, manaCost, cmc, types, subtypes, supertypes
            - text, power, toughness, colors, keywords, abilities
        """
        return await self._send_command("lookup", card=card_name)
    
    async def get_keywords(self, card_name: str) -> dict:
        """
        Get keyword abilities for a card, categorized.
        
        Returns:
            Dict with:
            - keywords: list of all keywords
            - categorized: dict with combat/evasion/protection/etc
        """
        return await self._send_command("keywords", card=card_name)
    
    async def validate_cast(
        self, 
        card_name: str, 
        state: Union[GameState, dict]
    ) -> dict:
        """
        Check if casting a spell is legal.
        
        Returns:
            Dict with:
            - legal: bool
            - reason: str (if not legal)
        """
        state_json = state.to_json() if isinstance(state, GameState) else state
        return await self._send_command("validate", action="cast", card=card_name, state=state_json)
    
    async def validate_attack(
        self, 
        creature_name: str, 
        state: Union[GameState, dict]
    ) -> dict:
        """Check if a creature can attack."""
        state_json = state.to_json() if isinstance(state, GameState) else state
        return await self._send_command("validate", action="attack", card=creature_name, state=state_json)
    
    async def validate_block(
        self, 
        blocker_name: str, 
        attacker_name: str,
        state: Union[GameState, dict]
    ) -> dict:
        """Check if a creature can block another."""
        state_json = state.to_json() if isinstance(state, GameState) else state
        return await self._send_command(
            "validate", 
            action="block", 
            card=blocker_name, 
            attacker=attacker_name,
            state=state_json
        )
    
    async def validate_activate(
        self,
        card_name: str,
        state: Union[GameState, dict]
    ) -> dict:
        """
        Check if activating an ability on a permanent is legal.

        Returns:
            Dict with:
            - legal: bool
            - reason: str (if not legal)
            - abilities: list of activated abilities with costs and legality
        """
        state_json = state.to_json() if isinstance(state, GameState) else state
        return await self._send_command("validate", action="activate", card=card_name, state=state_json)

    async def get_triggers(
        self,
        event: str,
        state: Union[GameState, dict],
        source: Optional[str] = None
    ) -> List[dict]:
        """
        Get triggered abilities for an event.

        Events: enters, dies, attacks, damage, upkeep, end_step
        
        Returns:
            List of triggers with source, controller, ability, mandatory
        """
        state_json = state.to_json() if isinstance(state, GameState) else state
        kwargs = {"event": event, "state": state_json}
        if source:
            kwargs["source"] = source
        
        result = await self._send_command("triggers", **kwargs)
        return result.get("triggers", [])
    
    async def run_state_based_actions(
        self, 
        state: Union[GameState, dict]
    ) -> tuple[List[dict], GameState]:
        """
        Run state-based actions and return what happened.
        
        Returns:
            Tuple of (actions performed, new game state)
        """
        state_json = state.to_json() if isinstance(state, GameState) else state
        result = await self._send_command("state_based", state=state_json)
        
        new_state = GameState.from_json(result.get("newState", {}))
        actions = result.get("actions", [])
        
        return actions, new_state
    
    async def resolve_combat(
        self,
        attackers: List[str],
        blockers: Dict[str, List[str]],  # attacker -> list of blockers
        state: Union[GameState, dict],
        damage_step: str = "normal"  # "first_strike" or "normal"
    ) -> tuple[List[dict], GameState]:
        """
        Resolve combat damage.
        
        Returns:
            Tuple of (damage events, new game state)
        """
        state_json = state.to_json() if isinstance(state, GameState) else state
        result = await self._send_command(
            "combat",
            attackers=attackers,
            blockers=blockers,
            state=state_json,
            step=damage_step
        )
        
        new_state = GameState.from_json(result.get("newState", {}))
        events = result.get("events", [])
        
        return events, new_state


# =============================================================================
# Standalone XMage Engine (No Java Required)
# =============================================================================

class StandaloneRulesEngine:
    """
    A Python-only rules engine that uses Scryfall data.
    
    Falls back to this if Java bridge isn't available.
    Not as comprehensive but handles common cases.
    """
    
    def __init__(self):
        self.card_cache: Dict[str, dict] = {}
    
    async def lookup(self, card_name: str) -> Optional[dict]:
        """Look up card from Scryfall API."""
        if card_name in self.card_cache:
            return self.card_cache[card_name]
        
        import aiohttp
        
        async with aiohttp.ClientSession() as session:
            url = f"https://api.scryfall.com/cards/named?fuzzy={card_name}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        
        card = {
            "name": data.get("name"),
            "manaCost": data.get("mana_cost", ""),
            "cmc": data.get("cmc", 0),
            "types": data.get("type_line", "").split(" — ")[0].split(),
            "subtypes": data.get("type_line", "").split(" — ")[1].split() if " — " in data.get("type_line", "") else [],
            "text": data.get("oracle_text", ""),
            "power": data.get("power"),
            "toughness": data.get("toughness"),
            "colors": data.get("colors", []),
            "keywords": data.get("keywords", []),
            "producedMana": data.get("produced_mana", [])
        }
        
        self.card_cache[card_name] = card
        return card
    
    def check_blocking(
        self, 
        attacker_keywords: List[str], 
        blocker_keywords: List[str]
    ) -> tuple[bool, Optional[str]]:
        """Check if blocker can block attacker."""
        attacker_set = set(k.lower() for k in attacker_keywords)
        blocker_set = set(k.lower() for k in blocker_keywords)
        
        if "flying" in attacker_set:
            if "flying" not in blocker_set and "reach" not in blocker_set:
                return False, "Can't block flyer without flying or reach"
        
        if "shadow" in attacker_set:
            if "shadow" not in blocker_set:
                return False, "Can't block shadow without shadow"
        
        if "horsemanship" in attacker_set:
            if "horsemanship" not in blocker_set:
                return False, "Can't block horsemanship without horsemanship"
        
        return True, None
    
    def calculate_combat_damage(
        self,
        attacker_power: int,
        attacker_keywords: List[str],
        blockers: List[dict],  # [{toughness, damage, keywords}]
        damage_step: str = "normal"
    ) -> dict:
        """Calculate combat damage assignment."""
        keywords = set(k.lower() for k in attacker_keywords)
        
        has_first = "first strike" in keywords or "double strike" in keywords
        has_double = "double strike" in keywords
        has_deathtouch = "deathtouch" in keywords
        has_trample = "trample" in keywords
        has_lifelink = "lifelink" in keywords
        
        # Check if deals damage this step
        if damage_step == "first_strike" and not has_first:
            return {"damage": [], "player_damage": 0, "lifelink": 0}
        if damage_step == "normal" and has_first and not has_double:
            return {"damage": [], "player_damage": 0, "lifelink": 0}
        
        damage_events = []
        remaining = attacker_power
        total_dealt = 0
        
        if not blockers:
            # Unblocked
            return {
                "damage": [],
                "player_damage": attacker_power,
                "lifelink": attacker_power if has_lifelink else 0
            }
        
        # Assign to blockers
        for blocker in blockers:
            if remaining <= 0:
                break
            
            lethal = 1 if has_deathtouch else max(0, blocker["toughness"] - blocker.get("damage", 0))
            assigned = min(remaining, lethal)
            
            damage_events.append({
                "blocker": blocker.get("name", "blocker"),
                "damage": assigned,
                "lethal": assigned >= lethal
            })
            
            remaining -= assigned
            total_dealt += assigned
        
        # Trample
        player_damage = remaining if has_trample else 0
        total_dealt += player_damage
        
        return {
            "damage": damage_events,
            "player_damage": player_damage,
            "lifelink": total_dealt if has_lifelink else 0
        }


# =============================================================================
# Hybrid Engine - Uses XMage when available, falls back to standalone
# =============================================================================

class HybridRulesEngine:
    """
    Uses XMage Java bridge when available, falls back to standalone Python.
    """
    
    def __init__(self, prefer_xmage: bool = True):
        self.prefer_xmage = prefer_xmage
        self._xmage: Optional[XMageBridge] = None
        self._standalone = StandaloneRulesEngine()
        self._xmage_available = False
        # Apr 30 audit: capture *why* the bridge failed to start so per-game
        # logs can surface the reason instead of leaving "no XMAGE tags" as
        # a silent symptom. Cleared on each start() call.
        self._last_init_error: Optional[str] = None

    async def start(self):
        """Try to start XMage bridge."""
        self._last_init_error = None
        if self.prefer_xmage:
            try:
                self._xmage = XMageBridge()
                await self._xmage.start()
                self._xmage_available = True
                print("Using XMage rules engine")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self._last_init_error = err
                print(f"XMage bridge not available ({err}), using standalone engine")
                self._xmage_available = False
        else:
            self._last_init_error = "prefer_xmage=False"
    
    async def stop(self):
        """Stop XMage bridge if running."""
        if self._xmage:
            await self._xmage.stop()
    
    async def lookup(self, card_name: str) -> Optional[dict]:
        """Look up a card."""
        if self._xmage_available:
            try:
                return await self._xmage.lookup(card_name)
            except XMageBridgeError:
                pass
        
        return await self._standalone.lookup(card_name)
    
    @property
    def using_xmage(self) -> bool:
        return self._xmage_available


# =============================================================================
# Demo / Test
# =============================================================================

async def demo():
    """Demo the rules bridge."""
    print("=== XMage Rules Bridge Demo ===\n")
    
    # Try hybrid engine (will fall back to standalone if XMage not available)
    engine = HybridRulesEngine()
    await engine.start()
    
    # Look up some cards
    cards = ["Lightning Bolt", "Tarmogoyf", "Baneslayer Angel", "Counterspell"]
    
    for card_name in cards:
        print(f"\n--- {card_name} ---")
        card = await engine.lookup(card_name)
        if card:
            print(f"  Mana: {card.get('manaCost', 'N/A')}")
            print(f"  Types: {' '.join(card.get('types', []))}")
            if card.get('power'):
                print(f"  P/T: {card['power']}/{card['toughness']}")
            if card.get('keywords'):
                print(f"  Keywords: {', '.join(card['keywords'])}")
            print(f"  Text: {card.get('text', '')[:100]}...")
        else:
            print("  Not found")
    
    # Test combat calculation (standalone)
    print("\n\n=== Combat Calculation Demo ===")
    
    # Scenario: 5/5 trample attacks, blocked by 2/2
    result = engine._standalone.calculate_combat_damage(
        attacker_power=5,
        attacker_keywords=["Trample"],
        blockers=[{"name": "Bear", "toughness": 2, "damage": 0}]
    )
    print(f"\n5/5 Trample vs 2/2 blocker:")
    print(f"  Damage to blocker: {result['damage']}")
    print(f"  Trample to player: {result['player_damage']}")
    
    # Scenario: 3/3 deathtouch attacks, blocked by two 4/4s
    result = engine._standalone.calculate_combat_damage(
        attacker_power=3,
        attacker_keywords=["Deathtouch"],
        blockers=[
            {"name": "Giant1", "toughness": 4, "damage": 0},
            {"name": "Giant2", "toughness": 4, "damage": 0}
        ]
    )
    print(f"\n3/3 Deathtouch vs two 4/4 blockers:")
    print(f"  Damage assignments: {result['damage']}")
    
    await engine.stop()
    print("\n\nDone!")


if __name__ == "__main__":
    asyncio.run(demo())
