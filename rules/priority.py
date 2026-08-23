"""
MTG Priority System
====================

Event-driven priority system designed for Discord integration.
Silence is passing - messages are actions.

Key insight: Discord messages ARE priority actions. We don't poll,
we respond to events. Auto-pass timer runs in background.

Usage:
    priority = PrioritySystem(players=["Alice", "Bob"], auto_pass_seconds=30)
    
    # Player takes an action
    await priority.player_action("Alice", PriorityAction("cast", card="Lightning Bolt"))
    
    # Or passes
    await priority.player_action("Alice", PriorityAction("pass"))
    
    # F6 mode - auto-pass until condition
    await priority.set_auto_pass("Alice", until="end_of_turn")
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Callable, Awaitable, List, Dict, Set, Any
from datetime import datetime, timedelta


class Phase(Enum):
    """Game phases and steps."""
    UNTAP = auto()
    UPKEEP = auto()
    DRAW = auto()
    MAIN1 = auto()
    COMBAT_BEGIN = auto()
    COMBAT_ATTACKERS = auto()
    COMBAT_BLOCKERS = auto()
    COMBAT_DAMAGE = auto()
    COMBAT_END = auto()
    MAIN2 = auto()
    END_STEP = auto()
    CLEANUP = auto()
    
    @property
    def is_main_phase(self) -> bool:
        return self in (Phase.MAIN1, Phase.MAIN2)
    
    @property
    def is_combat(self) -> bool:
        return self.name.startswith("COMBAT")
    
    @classmethod
    def next_phase(cls, current: 'Phase') -> 'Phase':
        """Get the next phase in turn order."""
        phases = list(cls)
        idx = phases.index(current)
        return phases[(idx + 1) % len(phases)]


class ActionType(Enum):
    """Types of priority actions."""
    PASS = auto()
    CAST = auto()
    ACTIVATE = auto()
    PLAY_LAND = auto()
    SPECIAL = auto()  # Morph, suspend, etc.
    HOLD = auto()  # "Wait, I'm thinking"


@dataclass
class PriorityAction:
    """An action a player takes when they have priority."""
    action_type: ActionType
    card_name: Optional[str] = None
    ability_index: Optional[int] = None
    targets: List[Any] = field(default_factory=list)
    mana_payment: Optional[Dict[str, int]] = None
    additional_costs: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def pass_priority(cls) -> 'PriorityAction':
        return cls(ActionType.PASS)
    
    @classmethod
    def cast(cls, card_name: str, targets: List[Any] = None, **kwargs) -> 'PriorityAction':
        return cls(ActionType.CAST, card_name=card_name, targets=targets or [], **kwargs)
    
    @classmethod
    def activate(cls, card_name: str, ability_index: int = 0, targets: List[Any] = None, **kwargs) -> 'PriorityAction':
        return cls(ActionType.ACTIVATE, card_name=card_name, ability_index=ability_index, targets=targets or [], **kwargs)
    
    @classmethod
    def hold(cls) -> 'PriorityAction':
        return cls(ActionType.HOLD)


@dataclass
class StackObject:
    """Something on the stack."""
    id: str
    controller: str
    name: str
    is_spell: bool  # vs activated/triggered ability
    targets: List[Any] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    
    # For triggered abilities
    trigger_source: Optional[str] = None
    trigger_event: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe mirror state for save/undo and reconnect recovery."""
        return {
            "id": self.id,
            "controller": self.controller,
            "name": self.name,
            "is_spell": self.is_spell,
            "targets": list(self.targets),
            "timestamp": self.timestamp.isoformat(),
            "trigger_source": self.trigger_source,
            "trigger_event": self.trigger_event,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StackObject':
        timestamp = data.get("timestamp")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp) if timestamp else datetime.now()
        except (TypeError, ValueError):
            parsed_timestamp = datetime.now()
        return cls(
            id=str(data.get("id", "")),
            controller=str(data.get("controller", "")),
            name=str(data.get("name", "Unknown")),
            is_spell=bool(data.get("is_spell", True)),
            targets=list(data.get("targets", []) or []),
            timestamp=parsed_timestamp,
            trigger_source=data.get("trigger_source"),
            trigger_event=data.get("trigger_event"),
        )


@dataclass
class AutoPassConfig:
    """Configuration for auto-passing."""
    enabled: bool = False
    until_phase: Optional[Phase] = None
    until_stack_empty: bool = False
    until_my_turn: bool = False
    break_on_opponent_action: bool = True
    break_on_targeting_me: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "until_phase": self.until_phase.name if self.until_phase else None,
            "until_stack_empty": self.until_stack_empty,
            "until_my_turn": self.until_my_turn,
            "break_on_opponent_action": self.break_on_opponent_action,
            "break_on_targeting_me": self.break_on_targeting_me,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AutoPassConfig':
        phase_name = data.get("until_phase")
        try:
            phase = Phase[phase_name] if phase_name else None
        except KeyError:
            phase = None
        return cls(
            enabled=bool(data.get("enabled", False)),
            until_phase=phase,
            until_stack_empty=bool(data.get("until_stack_empty", False)),
            until_my_turn=bool(data.get("until_my_turn", False)),
            break_on_opponent_action=bool(
                data.get("break_on_opponent_action", True)),
            break_on_targeting_me=bool(data.get("break_on_targeting_me", True)),
        )


class PrioritySystem:
    """
    Manages priority passing in a Magic game.
    
    Design principles:
    - Event-driven, not poll-driven
    - Async-first for Discord integration
    - Auto-pass timer for casual play feel
    - F6-style shortcuts for experienced players
    """
    
    def __init__(
        self,
        players: List[str],
        auto_pass_seconds: float = 30.0,
        on_priority_change: Optional[Callable[[str], Awaitable[None]]] = None,
        on_stack_resolve: Optional[Callable[[StackObject], Awaitable[None]]] = None,
        on_phase_change: Optional[Callable[[Phase], Awaitable[None]]] = None,
        on_combat_done: Optional[Callable[[], Awaitable[None]]] = None,
        reconnect_grace_seconds: float = 120.0,
        display_names: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize priority system.

        Args:
            players: List of player identifiers (in turn order, active player first)
            auto_pass_seconds: Time before auto-passing (0 to disable)
            on_priority_change: Callback when priority changes
            on_stack_resolve: Callback when stack object resolves
            on_phase_change: Callback when phase changes
            on_combat_done: Callback when combat priority window completes (all pass on empty stack)
        """
        self.players = players
        self.display_names = {
            player: (display_names or {}).get(player, player)
            for player in players
        }
        self.auto_pass_seconds = auto_pass_seconds
        self.reconnect_grace_seconds = reconnect_grace_seconds

        # Callbacks
        self._on_priority_change = on_priority_change
        self._on_stack_resolve = on_stack_resolve
        self._on_phase_change = on_phase_change
        self._on_combat_done = on_combat_done

        # Game state
        self.active_player: str = players[0]
        self.priority_holder: Optional[str] = players[0]
        self.phase: Phase = Phase.MAIN1
        self.turn_number: int = 1

        # Stack
        self.stack: List[StackObject] = []
        self._stack_id_counter: int = 0

        # Pass tracking (for "all players pass in succession")
        self._passes_in_succession: List[str] = []

        # Auto-pass timer
        self._pass_timer: Optional[asyncio.Task] = None
        self._pass_deadline: Optional[datetime] = None
        self._timer_player: Optional[str] = None
        self._holds: Set[str] = set()  # Players who said "hold"

        # Discord presence is deliberately separate from game liveness. A
        # disconnected seat remains in the APNAP ring and receives a bounded
        # reconnect grace period before silence becomes a pass.
        self._connected: Dict[str, bool] = {p: True for p in players}

        # Auto-pass configs per player (F6 mode)
        self._auto_pass_configs: Dict[str, AutoPassConfig] = {
            p: AutoPassConfig() for p in players
        }

        # Combat priority window mode — when True, all-pass on empty stack
        # signals _on_combat_done instead of advancing the phase
        self.combat_window: bool = False

        # Lock for state modifications
        self._lock = asyncio.Lock()

    def display_name(self, player: Optional[str]) -> str:
        """Presentation label for a stable internal player identifier."""
        if player is None:
            return "none"
        return self.display_names.get(player, player)
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    async def player_action(self, player: str, action: PriorityAction) -> Dict[str, Any]:
        """
        Handle a player taking an action.
        
        Returns dict with:
            - success: bool
            - message: str
            - priority_holder: str (who has priority now)
            - stack_size: int
        """
        async with self._lock:
            # Validate it's their turn to act
            if player != self.priority_holder:
                return {
                    "success": False,
                    "message": ("It's not your priority. "
                                f"{self.display_name(self.priority_holder)} "
                                "has priority."),
                    "priority_holder": self.priority_holder,
                    "stack_size": len(self.stack)
                }
            
            # Cancel any pending auto-pass
            self._cancel_pass_timer()
            
            # Remove from holds
            self._holds.discard(player)
            
            # Handle the action
            if action.action_type == ActionType.PASS:
                return await self._handle_pass(player)
            elif action.action_type == ActionType.HOLD:
                return await self._handle_hold(player)
            elif action.action_type in (ActionType.CAST, ActionType.ACTIVATE):
                return await self._handle_stack_action(player, action)
            elif action.action_type == ActionType.PLAY_LAND:
                return await self._handle_play_land(player, action)
            else:
                return {
                    "success": False,
                    "message": f"Unknown action type: {action.action_type}",
                    "priority_holder": self.priority_holder,
                    "stack_size": len(self.stack)
                }
    
    async def set_auto_pass(
        self, 
        player: str, 
        until: str = "end_of_turn",
        break_on_opponent_action: bool = True,
        break_on_targeting: bool = True
    ) -> Dict[str, Any]:
        """
        Set up auto-pass mode (F6 style).
        
        until options:
            - "end_of_turn": Pass until end of current turn
            - "my_turn": Pass until it's your turn again
            - "stack_empty": Pass until stack is empty
            - "never": Disable auto-pass
            - Phase name: Pass until that phase (e.g., "COMBAT_ATTACKERS")
        """
        config = AutoPassConfig(enabled=True)
        
        if until == "never":
            config.enabled = False
        elif until == "end_of_turn":
            config.until_phase = Phase.CLEANUP
        elif until == "my_turn":
            config.until_my_turn = True
        elif until == "stack_empty":
            config.until_stack_empty = True
        else:
            # Try to parse as phase name
            try:
                config.until_phase = Phase[until.upper()]
            except KeyError:
                return {"success": False, "message": f"Unknown phase: {until}"}
        
        config.break_on_opponent_action = break_on_opponent_action
        config.break_on_targeting_me = break_on_targeting
        
        self._auto_pass_configs[player] = config
        
        # If this player has priority and should auto-pass, do it
        if player == self.priority_holder and self._should_auto_pass(player):
            await self.player_action(player, PriorityAction.pass_priority())
        
        return {
            "success": True,
            "message": f"Auto-pass enabled until {until}",
            "config": {
                "until_phase": config.until_phase.name if config.until_phase else None,
                "until_my_turn": config.until_my_turn,
                "until_stack_empty": config.until_stack_empty,
            }
        }
    
    async def cancel_auto_pass(self, player: str) -> Dict[str, Any]:
        """Cancel auto-pass mode for a player."""
        self._auto_pass_configs[player] = AutoPassConfig(enabled=False)
        return {"success": True, "message": "Auto-pass disabled"}
    
    def get_state(self) -> Dict[str, Any]:
        """Get current priority state for display."""
        stack = []
        for obj in reversed(self.stack):
            payload = obj.to_dict()
            payload["controller_id"] = payload.get("controller")
            payload["controller"] = self.display_name(payload.get("controller"))
            stack.append(payload)
        return {
            "active_player": self.display_name(self.active_player),
            "active_player_id": self.active_player,
            "priority_holder": (self.display_name(self.priority_holder)
                                if self.priority_holder else None),
            "priority_holder_id": self.priority_holder,
            "phase": self.phase.name,
            "turn": self.turn_number,
            "stack": stack,
            "waiting_for": (self.display_name(self.priority_holder)
                            if self.priority_holder else None),
            "passes_in_succession": list(self._passes_in_succession),
            "connected": {
                self.display_name(player): connected
                for player, connected in self._connected.items()
            },
            "deadline": (self._pass_deadline.isoformat()
                         if self._pass_deadline else None),
            "auto_pass_active": {
                p: self._auto_pass_configs[p].enabled 
                for p in self.players
            }
        }

    def to_dict(self) -> Dict[str, Any]:
        """Persist the exact priority window without asyncio runtime objects."""
        return {
            "version": 2,
            "players": list(self.players),
            "active_player": self.active_player,
            "priority_holder": self.priority_holder,
            "phase": self.phase.name,
            "turn_number": self.turn_number,
            "stack": [obj.to_dict() for obj in self.stack],
            "stack_id_counter": self._stack_id_counter,
            "passes_in_succession": list(self._passes_in_succession),
            "holds": sorted(self._holds),
            "auto_pass_configs": {
                player: config.to_dict()
                for player, config in self._auto_pass_configs.items()
            },
            "connected": dict(self._connected),
            "combat_window": self.combat_window,
            "pass_deadline": (self._pass_deadline.isoformat()
                              if self._pass_deadline else None),
            "display_names": dict(self.display_names),
        }

    def restore_state(self, data: Optional[Dict[str, Any]],
                      living_players: Optional[List[str]] = None) -> None:
        """Restore a saved priority window while retaining live callbacks."""
        if not data:
            return
        allowed = list(
            living_players if living_players is not None else self.players)
        saved_players = [p for p in data.get("players", []) if p in allowed]
        self.players = saved_players + [p for p in allowed if p not in saved_players]
        saved_display_names = data.get("display_names", {})
        self.display_names = {
            player: saved_display_names.get(
                player, self.display_names.get(player, player))
            for player in self.players
        }
        if not self.players:
            self.priority_holder = None
            self.stack = []
            return

        active = data.get("active_player")
        holder = data.get("priority_holder")
        self.active_player = active if active in self.players else self.players[0]
        self.priority_holder = holder if holder in self.players else self.active_player
        try:
            self.phase = Phase[str(data.get("phase", "MAIN1"))]
        except KeyError:
            self.phase = Phase.MAIN1
        self.turn_number = int(data.get("turn_number", 1))
        self.stack = [
            StackObject.from_dict(item) for item in data.get("stack", [])
            if isinstance(item, dict)
        ]
        self._stack_id_counter = max(
            int(data.get("stack_id_counter", 0)),
            max((int(obj.id.rsplit("_", 1)[-1])
                 for obj in self.stack
                 if obj.id.rsplit("_", 1)[-1].isdigit()), default=0),
        )
        self._passes_in_succession = [
            p for p in data.get("passes_in_succession", []) if p in self.players
        ]
        self._holds = {p for p in data.get("holds", []) if p in self.players}
        saved_configs = data.get("auto_pass_configs", {})
        self._auto_pass_configs = {
            p: AutoPassConfig.from_dict(saved_configs.get(p, {}))
            for p in self.players
        }
        saved_connected = data.get("connected", {})
        self._connected = {
            p: bool(saved_connected.get(p, True)) for p in self.players
        }
        self.combat_window = bool(data.get("combat_window", False))
        deadline = data.get("pass_deadline")
        try:
            self._pass_deadline = datetime.fromisoformat(deadline) if deadline else None
        except (TypeError, ValueError):
            self._pass_deadline = None
        self._timer_player = self.priority_holder if self._pass_deadline else None

    async def resume(self) -> Dict[str, Any]:
        """Re-issue the current window and restart its remaining timer."""
        if not self.priority_holder:
            return self.get_state()
        if self._pass_deadline:
            remaining = max(
                0.0, (self._pass_deadline - datetime.now()).total_seconds())
            self._start_pass_timer(seconds=remaining)
        else:
            self._start_pass_timer()
        await self._notify_priority_change()
        return self.get_state()

    def mark_disconnected(self, player: str) -> Dict[str, Any]:
        """Keep a seat live while granting a bounded reconnect window."""
        if player not in self.players:
            return {"success": False, "message": "Unknown priority seat."}
        self._connected[player] = False
        if self.priority_holder == player:
            self._start_pass_timer(seconds=self.reconnect_grace_seconds)
        return {
            "success": True,
            "message": (f"{self.display_name(player)} disconnected; "
                        "reconnect grace started."),
            "deadline": (self._pass_deadline.isoformat()
                         if self._pass_deadline else None),
        }

    async def mark_reconnected(self, player: str) -> Dict[str, Any]:
        """Rebind a seat to the live window without changing APNAP order."""
        if player not in self.players:
            return {"success": False, "message": "Unknown priority seat."}
        self._connected[player] = True
        if self.priority_holder == player:
            self._start_pass_timer()
            await self._notify_priority_change()
        return {
            "success": True,
            "message": f"{self.display_name(player)} reconnected.",
            "state": self.get_state(),
        }
    
    # =========================================================================
    # Phase Management
    # =========================================================================
    
    async def advance_phase(self) -> Phase:
        """Advance to the next phase (public API, acquires lock)."""
        async with self._lock:
            return await self._advance_phase_unlocked()

    async def _advance_phase_unlocked(self) -> Phase:
        """Advance to the next phase (internal, caller must hold lock)."""
        old_phase = self.phase
        self.phase = Phase.next_phase(self.phase)

        # Handle turn rollover
        if self.phase == Phase.UNTAP:
            self._advance_turn()

        # Reset pass tracking
        self._passes_in_succession = []

        # Active player gets priority (except untap/cleanup)
        if self.phase not in (Phase.UNTAP, Phase.CLEANUP):
            self.priority_holder = self.active_player
            await self._notify_priority_change()
        else:
            self.priority_holder = None

        # Notify phase change
        if self._on_phase_change:
            await self._on_phase_change(self.phase)

        # Check for auto-pass conditions
        for player in self.players:
            config = self._auto_pass_configs[player]
            if config.enabled and config.until_phase == old_phase:
                config.enabled = False

        return self.phase
    
    def _advance_turn(self):
        """Move to the next player's turn."""
        idx = self.players.index(self.active_player)
        self.active_player = self.players[(idx + 1) % len(self.players)]
        self.turn_number += 1
        
        # Check auto-pass "until my turn" conditions
        for player in self.players:
            config = self._auto_pass_configs[player]
            if config.enabled and config.until_my_turn and player == self.active_player:
                config.enabled = False
    
    # =========================================================================
    # Internal Handlers
    # =========================================================================
    
    async def _handle_pass(self, player: str) -> Dict[str, Any]:
        """Handle a player passing priority."""
        self._passes_in_succession.append(player)

        # Check if all players have passed in succession
        if self._all_players_passed():
            if self.stack:
                # Resolve top of stack
                return await self._resolve_top_of_stack()
            elif self.combat_window:
                # Combat priority window — all passed on empty stack.
                # Signal the caller that the window is done instead of advancing phase.
                self.combat_window = False
                if self._on_combat_done:
                    await self._on_combat_done()
                return {
                    "success": True,
                    "message": "Combat priority window passed.",
                    "priority_holder": self.priority_holder,
                    "stack_size": 0,
                }
            else:
                # Empty stack, all passed - advance phase
                # Use unlocked version since we're already inside the lock
                await self._advance_phase_unlocked()
                return {
                    "success": True,
                    "message": f"All players passed. Moving to {self.phase.name}.",
                    "priority_holder": self.priority_holder,
                    "stack_size": 0,
                    "phase": self.phase.name
                }
        else:
            # Pass to next player
            self._advance_priority()
            await self._notify_priority_change()
            self._start_pass_timer()
            
            return {
                "success": True,
                "message": f"{player} passes. {self.priority_holder} has priority.",
                "priority_holder": self.priority_holder,
                "stack_size": len(self.stack)
            }
    
    async def _handle_hold(self, player: str) -> Dict[str, Any]:
        """Handle a player requesting to hold (thinking)."""
        self._holds.add(player)
        self._cancel_pass_timer()
        
        return {
            "success": True,
            "message": f"{player} is thinking...",
            "priority_holder": self.priority_holder,
            "stack_size": len(self.stack)
        }
    
    async def _handle_stack_action(self, player: str, action: PriorityAction) -> Dict[str, Any]:
        """Handle casting a spell or activating an ability."""
        # Create stack object
        self._stack_id_counter += 1
        stack_obj = StackObject(
            id=f"stack_{self._stack_id_counter}",
            controller=player,
            name=action.card_name or f"Ability {action.ability_index}",
            is_spell=(action.action_type == ActionType.CAST),
            targets=action.targets
        )
        
        self.stack.append(stack_obj)
        
        # Reset pass tracking - action was taken
        self._passes_in_succession = []
        
        # Break auto-pass for opponents if configured
        for other_player in self.players:
            if other_player != player:
                config = self._auto_pass_configs[other_player]
                if config.enabled and config.break_on_opponent_action:
                    config.enabled = False
                # Check if targeting breaks auto-pass
                if config.enabled and config.break_on_targeting_me:
                    if other_player in action.targets:
                        config.enabled = False
        
        # Player retains priority after casting/activating
        # (They can respond to their own spell)
        self._start_pass_timer()
        
        return {
            "success": True,
            "message": f"{player} {'casts' if stack_obj.is_spell else 'activates'} {stack_obj.name}.",
            "priority_holder": self.priority_holder,
            "stack_size": len(self.stack),
            "stack_object": {
                "id": stack_obj.id,
                "name": stack_obj.name,
                "targets": stack_obj.targets
            }
        }
    
    async def _handle_play_land(self, player: str, action: PriorityAction) -> Dict[str, Any]:
        """Handle playing a land (doesn't use the stack)."""
        # Lands don't use the stack, but you need priority to play them
        # Don't reset pass tracking - land drop is a special action
        
        return {
            "success": True,
            "message": f"{player} plays {action.card_name}.",
            "priority_holder": self.priority_holder,
            "stack_size": len(self.stack),
            "land_played": action.card_name
        }
    
    def remove_stack_entry_by_priority_id(self, priority_id: str) -> bool:
        """Remove a StackObject from the priority system's stack by id.

        May 18 audit: the engine's fast-path resolution (the 0.5s auto-resolve
        timeout in mtg/spells.py) calls `game.stack.remove(stack_entry)`
        directly, bypassing the PrioritySystem's `_resolve_top_of_stack`.
        That leaves a phantom StackObject in `self.stack` with no
        corresponding entry in game.stack. When the PrioritySystem later
        cycles, it calls on_stack_resolve(stack_obj) for the phantom, and
        the callback emits `[STACK] No priority_id match for X, using shared
        event` while doing a no-op shared-event fire. The cycle visibly
        cascaded through 5 already-resolved spells in
        `game_1505768915773685800` before the combat-priority timeout fired.

        Callers in mtg/spells.py should invoke this immediately after every
        `game.stack.remove(...)` site so the two stacks stay in sync.

        Returns True if a matching entry was found and removed.
        """
        for i, obj in enumerate(self.stack):
            if obj.id == priority_id:
                del self.stack[i]
                return True
        return False

    async def _resolve_top_of_stack(self) -> Dict[str, Any]:
        """Resolve the top object on the stack."""
        if not self.stack:
            return {
                "success": False,
                "message": "Stack is empty.",
                "priority_holder": self.priority_holder,
                "stack_size": 0
            }

        resolving = self.stack.pop()

        # Notify resolution
        if self._on_stack_resolve:
            await self._on_stack_resolve(resolving)

        # Reset pass tracking
        self._passes_in_succession = []

        if self.stack:
            # More items on stack — give priority to active player for responses
            self.priority_holder = self.active_player
            await self._notify_priority_change()
            self._start_pass_timer()
        else:
            # Stack is now empty after resolution. DON'T start a new priority
            # round or auto-pass timer — the game engine owns phase flow and
            # will continue from cast_spell_async() once on_stack_resolve fires.
            # Starting priority here causes an infinite bounce loop where both
            # AI players keep passing on empty stack, advancing phases endlessly.
            self.priority_holder = self.active_player

            # Disable "until stack empty" auto-pass configs
            for player in self.players:
                config = self._auto_pass_configs[player]
                if config.enabled and config.until_stack_empty:
                    config.enabled = False

        return {
            "success": True,
            "message": f"{resolving.name} resolves.",
            "priority_holder": self.priority_holder,
            "stack_size": len(self.stack),
            "resolved": {
                "id": resolving.id,
                "name": resolving.name,
                "controller": resolving.controller,
                "targets": resolving.targets
            }
        }
    
    # =========================================================================
    # Priority Flow Helpers
    # =========================================================================
    
    def _advance_priority(self):
        """Pass priority to the next player in APNAP order."""
        if self.priority_holder is None:
            self.priority_holder = self.active_player
            return
        
        idx = self.players.index(self.priority_holder)
        self.priority_holder = self.players[(idx + 1) % len(self.players)]
        
        # Check if this player should auto-pass
        if self._should_auto_pass(self.priority_holder):
            # Schedule the auto-pass
            asyncio.create_task(self._auto_pass_for_player(self.priority_holder))
    
    async def _auto_pass_for_player(self, player: str):
        """Auto-pass for a player in F6 mode."""
        # Small delay to allow UI updates
        await asyncio.sleep(0.1)
        
        if player == self.priority_holder and self._should_auto_pass(player):
            await self.player_action(player, PriorityAction.pass_priority())
    
    def _should_auto_pass(self, player: str) -> bool:
        """Check if player should auto-pass."""
        config = self._auto_pass_configs.get(player)
        return config is not None and config.enabled
    
    def _all_players_passed(self) -> bool:
        """Check if all players have passed in succession."""
        if len(self._passes_in_succession) < len(self.players):
            return False
        
        # Check last N passes include all players
        recent = self._passes_in_succession[-len(self.players):]
        return set(recent) == set(self.players)
    
    async def _notify_priority_change(self):
        """Notify that priority has changed."""
        if self._on_priority_change and self.priority_holder:
            await self._on_priority_change(self.priority_holder)
    
    # =========================================================================
    # Auto-Pass Timer
    # =========================================================================
    
    def _start_pass_timer(self, seconds: Optional[float] = None):
        """Start the auto-pass countdown timer."""
        if seconds is None:
            delay = (
                self.reconnect_grace_seconds
                if self.priority_holder
                and not self._connected.get(self.priority_holder, True)
                else self.auto_pass_seconds
            )
        else:
            delay = max(0.0, seconds)
        if seconds is None and delay <= 0:
            self._pass_deadline = None
            self._timer_player = None
            return
        
        if self.priority_holder in self._holds:
            return  # Player requested hold
        
        self._cancel_pass_timer()
        holder = self.priority_holder
        if holder is None:
            return
        self._timer_player = holder
        self._pass_deadline = datetime.now() + timedelta(seconds=delay)
        self._pass_timer = asyncio.create_task(
            self._pass_timer_countdown(holder, delay))
    
    def _cancel_pass_timer(self):
        """Cancel the auto-pass timer."""
        timer = self._pass_timer
        self._pass_timer = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if timer and timer is not current:
            timer.cancel()
        self._pass_deadline = None
        self._timer_player = None

    async def _pass_timer_countdown(self, player: str, delay: float):
        """Countdown for auto-pass."""
        try:
            await asyncio.sleep(delay)
            
            # Time's up - auto-pass if still this player's priority
            if (self.priority_holder == player
                    and player not in self._holds):
                await self.player_action(
                    player,
                    PriorityAction.pass_priority()
                )
        except asyncio.CancelledError:
            pass  # Timer was cancelled, that's fine


# =============================================================================
# APNAP Trigger Ordering
# =============================================================================

@dataclass
class TriggeredAbility:
    """A triggered ability waiting to go on the stack."""
    source: str  # Card name
    controller: str
    trigger_text: str
    trigger_event: str  # "enters", "dies", "attacks", etc.
    mandatory: bool = True
    targets_required: bool = False
    timestamp: datetime = field(default_factory=datetime.now)


class TriggerManager:
    """
    Manages triggered abilities and APNAP ordering.
    
    APNAP = Active Player, Non-Active Player
    When multiple triggers happen simultaneously:
    1. Active player's triggers go on stack first (in order of their choice)
    2. Then next player in turn order, etc.
    3. So non-active player's triggers resolve FIRST
    """
    
    def __init__(self, players: List[str], active_player: str):
        self.players = players
        self.active_player = active_player
        self._pending_triggers: Dict[str, List[TriggeredAbility]] = {p: [] for p in players}
    
    def add_trigger(self, trigger: TriggeredAbility):
        """Add a triggered ability to the pending list."""
        self._pending_triggers[trigger.controller].append(trigger)
    
    def add_triggers(self, triggers: List[TriggeredAbility]):
        """Add multiple triggers at once (simultaneous events)."""
        for trigger in triggers:
            self.add_trigger(trigger)
    
    def get_apnap_order(self) -> List[str]:
        """Get players in APNAP order starting from active player."""
        idx = self.players.index(self.active_player)
        return self.players[idx:] + self.players[:idx]
    
    def needs_ordering(self, player: str) -> bool:
        """Check if a player needs to order their triggers."""
        triggers = self._pending_triggers.get(player, [])
        return len(triggers) > 1
    
    def get_pending_triggers(self, player: str) -> List[TriggeredAbility]:
        """Get pending triggers for a player."""
        return self._pending_triggers.get(player, [])
    
    async def order_triggers_for_player(
        self, 
        player: str, 
        order: List[int]
    ) -> List[TriggeredAbility]:
        """
        Set the order for a player's triggers.
        
        Args:
            player: The player ordering their triggers
            order: List of indices in desired stack order (first = bottom of stack)
        
        Returns:
            Ordered list of triggers
        """
        triggers = self._pending_triggers.get(player, [])
        if not triggers:
            return []
        
        if len(order) != len(triggers):
            raise ValueError(f"Order must specify all {len(triggers)} triggers")
        
        ordered = [triggers[i] for i in order]
        self._pending_triggers[player] = []
        return ordered
    
    def auto_order_triggers(self, player: str) -> List[TriggeredAbility]:
        """
        Auto-order triggers for a player (timestamp order).
        Used when player doesn't manually order.
        """
        triggers = self._pending_triggers.get(player, [])
        self._pending_triggers[player] = []
        return sorted(triggers, key=lambda t: t.timestamp)
    
    def get_all_ordered_triggers(self) -> List[TriggeredAbility]:
        """
        Get all pending triggers in APNAP stack order.
        
        Returns triggers in the order they should go on the stack:
        - Active player's triggers first (bottom of stack)
        - Then each subsequent player
        - So NAP's triggers resolve first
        """
        all_triggers = []
        
        for player in self.get_apnap_order():
            player_triggers = self.auto_order_triggers(player)
            all_triggers.extend(player_triggers)
        
        return all_triggers
    
    def clear(self):
        """Clear all pending triggers."""
        self._pending_triggers = {p: [] for p in self.players}


# =============================================================================
# Discord Integration Helpers
# =============================================================================

class DiscordPriorityAdapter:
    """
    Adapts the priority system for Discord message handling.
    
    Maps Discord messages to priority actions.
    """
    
    # Command patterns
    PASS_COMMANDS = {'pass', 'p', 'ok', 'okay', 'done', 'go', 'resolve'}
    HOLD_COMMANDS = {'hold', 'wait', 'thinking', 'sec', 'hmm'}
    CAST_PREFIX = {'cast', 'play', 'c'}
    ACTIVATE_PREFIX = {'activate', 'act', 'a', 'use'}
    
    def __init__(self, priority_system: PrioritySystem):
        self.priority = priority_system
    
    def parse_message(self, content: str) -> Optional[PriorityAction]:
        """
        Parse a Discord message into a priority action.
        
        Returns None if message isn't a game action.
        """
        content = content.strip().lower()
        words = content.split()
        
        if not words:
            return None
        
        first_word = words[0]
        
        # Pass
        if first_word in self.PASS_COMMANDS:
            return PriorityAction.pass_priority()
        
        # Hold
        if first_word in self.HOLD_COMMANDS:
            return PriorityAction.hold()
        
        # Cast
        if first_word in self.CAST_PREFIX and len(words) > 1:
            card_name = ' '.join(words[1:])
            # TODO: Parse targets from message
            return PriorityAction.cast(card_name)
        
        # Activate
        if first_word in self.ACTIVATE_PREFIX and len(words) > 1:
            card_name = ' '.join(words[1:])
            return PriorityAction.activate(card_name)
        
        # F6 shortcuts
        if content in ('f6', 'yield', 'pass all'):
            return PriorityAction(ActionType.SPECIAL)  # Handle specially
        
        return None
    
    async def handle_message(self, player: str, content: str) -> Optional[Dict[str, Any]]:
        """
        Handle a Discord message as a game action.
        
        Returns the result if it was a game action, None otherwise.
        """
        action = self.parse_message(content)
        
        if action is None:
            return None
        
        # Special handling for F6
        if action.action_type == ActionType.SPECIAL:
            return await self.priority.set_auto_pass(player, until="end_of_turn")
        
        return await self.priority.player_action(player, action)
    
    def format_state_message(self) -> str:
        """Format current state for Discord display."""
        state = self.priority.get_state()
        
        lines = [
            f"**Turn {state['turn']}** - {state['phase']}",
            f"Active Player: {state['active_player']}",
        ]
        
        if state['stack']:
            lines.append(f"\n**Stack** ({len(state['stack'])} objects):")
            for i, obj in enumerate(state['stack']):
                targets = f" → {', '.join(map(str, obj['targets']))}" if obj['targets'] else ""
                lines.append(f"  {i+1}. {obj['name']} ({obj['controller']}){targets}")
        else:
            lines.append("\n*Stack is empty*")
        
        lines.append(f"\n⏳ **{state['priority_holder']}** has priority")
        
        return '\n'.join(lines)


# =============================================================================
# Demo / Test
# =============================================================================

async def demo():
    """Demo the priority system."""
    print("=== Priority System Demo ===\n")
    
    # Set up callbacks
    async def on_priority(player: str):
        print(f"  → {player} has priority")
    
    async def on_resolve(obj: StackObject):
        print(f"  ✓ {obj.name} resolves!")
    
    async def on_phase(phase: Phase):
        print(f"\n--- {phase.name} ---")
    
    # Create system
    priority = PrioritySystem(
        players=["Alice", "Bob"],
        auto_pass_seconds=0,  # Disable for demo
        on_priority_change=on_priority,
        on_stack_resolve=on_resolve,
        on_phase_change=on_phase
    )
    
    print("Starting in Main Phase 1")
    print(f"Active player: {priority.active_player}")
    print()
    
    # Alice casts Lightning Bolt
    print("Alice casts Lightning Bolt targeting Bob...")
    result = await priority.player_action(
        "Alice", 
        PriorityAction.cast("Lightning Bolt", targets=["Bob"])
    )
    print(f"  Stack size: {result['stack_size']}")
    
    # Alice passes (retains priority after casting, chooses to pass)
    print("\nAlice passes...")
    result = await priority.player_action("Alice", PriorityAction.pass_priority())
    
    # Bob responds with Counterspell
    print("\nBob casts Counterspell targeting Lightning Bolt...")
    result = await priority.player_action(
        "Bob",
        PriorityAction.cast("Counterspell", targets=["Lightning Bolt"])
    )
    print(f"  Stack size: {result['stack_size']}")
    
    # Bob passes
    print("\nBob passes...")
    await priority.player_action("Bob", PriorityAction.pass_priority())
    
    # Alice passes - Counterspell resolves
    print("\nAlice passes...")
    result = await priority.player_action("Alice", PriorityAction.pass_priority())
    print(f"  Stack size after resolution: {result['stack_size']}")
    
    # Both pass again - Lightning Bolt was countered, stack empty
    print("\nBob passes...")
    await priority.player_action("Bob", PriorityAction.pass_priority())
    
    print("\nAlice passes (stack empty)...")
    result = await priority.player_action("Alice", PriorityAction.pass_priority())
    print(f"  New phase: {result.get('phase', priority.phase.name)}")
    
    # Test APNAP
    print("\n\n=== APNAP Trigger Ordering Demo ===\n")
    
    trigger_mgr = TriggerManager(["Alice", "Bob"], active_player="Alice")
    
    # Simultaneous triggers
    trigger_mgr.add_triggers([
        TriggeredAbility("Soul Warden", "Alice", "Gain 1 life", "enters"),
        TriggeredAbility("Blood Artist", "Alice", "Drain 1", "enters"),
        TriggeredAbility("Suture Priest", "Bob", "Opponent loses 1 life", "enters"),
    ])
    
    print("Triggers added:")
    print("  Alice: Soul Warden (gain 1), Blood Artist (drain 1)")
    print("  Bob: Suture Priest (opponent loses 1)")
    
    print("\nAPNAP order:", trigger_mgr.get_apnap_order())
    
    ordered = trigger_mgr.get_all_ordered_triggers()
    print("\nStack order (bottom to top):")
    for i, t in enumerate(ordered):
        print(f"  {i+1}. {t.source} ({t.controller}): {t.trigger_text}")
    
    print("\nResolution order (top to bottom):")
    for t in reversed(ordered):
        print(f"  - {t.source} ({t.controller}): {t.trigger_text}")
    
    print("\n=== Demo Complete ===")


if __name__ == "__main__":
    asyncio.run(demo())
