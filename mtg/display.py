"""GameDisplay — Discord text + embed formatters for game state.

Pure formatting layer: takes a GameState (or Player) and produces
human-readable strings or discord.Embed objects. Doesn't mutate state.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

from typing import Optional

import discord

from mtg.constants import PHASE_NAMES
from mtg.models import GameState, Player


# =============================================================================
# GAME DISPLAY
# =============================================================================

class GameDisplay:
    """Format game state for Discord display."""
    
    @staticmethod
    def commander_damage_summary(game: GameState, player: Player) -> Optional[str]:
        """"<source> N/21" for each commander that has connected, else None.

        Shared by the text board and the embed so the two cannot drift. The
        /21 matters as much as the number: without it a player watches a
        total climb with no sense of what it is climbing towards, which is
        how the June 11 audit found people learning the rule from their own
        death message.

        Gated on Commander/EDH because that is where the rule applies — Brawl
        and Oathbreaker keep their command zones without inheriting the
        21-damage loss condition (Aug 14).
        """
        if game.format not in ("commander", "edh"):
            return None

        def _label(key):
            # Keys are commander NAMES (CR 903.10a per-commander, Aug 1);
            # legacy int keys from older saves fall back to the player name.
            if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
                index = int(key)
                return (game.players[index].name
                        if 0 <= index < len(game.players) else str(key))
            return str(key)

        parts = ["%s %d/21" % (_label(k), dmg)
                 for k, dmg in player.commander_damage.items() if dmg > 0]
        return ", ".join(parts) if parts else None

    @staticmethod
    def format_board_state(game: GameState, viewer_index: Optional[int] = None) -> str:
        """Format the full board state as a string."""
        lines = []
        
        # Header
        lines.append(f"**⚔️ MTG Game - {game.format.title()}**")
        lines.append(f"Turn {game.turn_number} • {PHASE_NAMES[game.phase]}")
        lines.append("")
        
        for i, player in enumerate(game.players):
            is_active = i == game.active_player_index
            active_marker = "👉 " if is_active else "   "
            claude_marker = " 🤖" if player.is_claude else ""
            
            eliminated_marker = " ☠️ eliminated" if player.eliminated else ""
            lines.append(
                f"{active_marker}**{player.name}**{claude_marker}"
                f"{eliminated_marker}")
            lines.append(f"   ❤️ {player.life} | ☠️ {player.poison} | 🎴 {len(player.hand)} cards")
            
            # Commander damage received
            _cd = GameDisplay.commander_damage_summary(game, player)
            if _cd:
                lines.append(f"   ⚔️ Commander damage: {_cd}")
            
            # Battlefield
            if player.battlefield:
                permanents = [c.display_name() for c in player.battlefield]
                lines.append(f"   📍 Battlefield: {', '.join(permanents)}")
            else:
                lines.append(f"   📍 Battlefield: (empty)")
            
            # Graveyard count
            if player.graveyard:
                lines.append(f"   ⚰️ Graveyard: {len(player.graveyard)} cards")
            
            # Exile - show suspended cards with time counters  
            if player.exile:
                suspended = [c for c in player.exile if c.suspended]
                other_exile = len(player.exile) - len(suspended)
                
                if suspended:
                    suspend_strs = [f"{c.name} ({c.counters.get('time', 0)}⏳)" for c in suspended]
                    lines.append(f"   ⏳ Suspended: {', '.join(suspend_strs)}")
                if other_exile > 0:
                    lines.append(f"   🚫 Exile: {other_exile} cards")

            # Companion zone
            if player.companion_zone:
                comp_names = [c.name for c in player.companion_zone]
                lines.append(f"   🐾 Companion: {', '.join(comp_names)}")

            lines.append("")

        # Stack
        if game.stack:
            lines.append("**📚 Stack:**")
            for item in reversed(game.stack):
                lines.append(f"   • {item.get('description', 'Unknown')}")
            lines.append("")
        
        return "\n".join(lines)
    
    @staticmethod
    def format_hand(player: Player) -> str:
        """Format a player's hand (for DM)."""
        if not player.hand:
            return "Your hand is empty."
        
        lines = [f"**🎴 Your Hand ({len(player.hand)} cards):**\n"]
        for i, card in enumerate(player.hand, 1):
            type_short = card.type_line.split("—")[0].strip() if "—" in card.type_line else card.type_line
            lines.append(f"{i}. **{card.name}** {card.mana_cost}")
            lines.append(f"   *{type_short}*")
            if card.oracle_text:
                # Truncate long text (400 chars to fit cards like Voice of Resurgence at 237)
                text = card.oracle_text[:400] + "..." if len(card.oracle_text) > 400 else card.oracle_text
                lines.append(f"   {text}")
        
        return "\n".join(lines)
    
    @staticmethod
    def format_graveyard(player: Player) -> str:
        """Format a player's graveyard."""
        if not player.graveyard:
            return f"{player.name}'s graveyard is empty."
        
        cards = [c.name for c in player.graveyard]
        return f"**⚰️ {player.name}'s Graveyard ({len(cards)} cards):**\n{', '.join(cards)}"
    
    @staticmethod
    def create_game_embed(game: GameState) -> discord.Embed:
        """Create a Discord embed for the game state."""
        embed = discord.Embed(
            title=f"⚔️ MTG Game - {game.format.title()}",
            description=f"Turn {game.turn_number} • {PHASE_NAMES[game.phase]}",
            color=discord.Color.gold() if game.active_player_index == 0 else discord.Color.blue()
        )
        
        for i, player in enumerate(game.players):
            is_active = i == game.active_player_index
            name = (f"{'👉 ' if is_active else ''}{player.name}"
                    f"{'🤖' if player.is_claude else ''}"
                    f"{' ☠️ eliminated' if player.eliminated else ''}")
            
            # Build field value
            value_parts = [f"❤️ {player.life} | ☠️ {player.poison} | 🎴 {len(player.hand)} in hand"]
            
            # The embed is what !state and every turn handoff actually send,
            # and it omitted the second loss condition entirely.
            cmd_dmg = GameDisplay.commander_damage_summary(game, player)
            if cmd_dmg:
                value_parts.append(f"👑 Commander damage: {cmd_dmg}")

            if player.battlefield:
                bf = [c.display_name() for c in player.battlefield[:5]]
                if len(player.battlefield) > 5:
                    bf.append(f"...and {len(player.battlefield) - 5} more")
                value_parts.append(f"📍 {', '.join(bf)}")
            
            embed.add_field(name=name, value="\n".join(value_parts), inline=False)

        # Show stack if non-empty
        if game.stack:
            stack_lines = []
            for i, entry in enumerate(reversed(game.stack)):
                if hasattr(entry, 'card'):
                    name_str = entry.card.name if entry.card else "Ability"
                    ctrl = entry.controller_name
                    kind = "spell" if entry.is_spell else "trigger"
                    # Format target as a readable name, not raw Card repr
                    if entry.target:
                        if hasattr(entry.target, 'name'):
                            target_str = f" → {entry.target.name}"
                        elif hasattr(entry.target, 'life'):
                            target_str = f" → {entry.target.name}"
                        else:
                            target_str = f" → {entry.target}"
                    else:
                        target_str = ""
                    countered_str = " *(countered)*" if entry.countered else ""
                    stack_lines.append(f"{i+1}. **{name_str}** ({ctrl}, {kind}){target_str}{countered_str}")
                else:
                    # Legacy dict format
                    stack_lines.append(f"{i+1}. {entry}")
            embed.add_field(
                name=f"📚 Stack ({len(game.stack)})",
                value="\n".join(stack_lines[:5]) or "*empty*",
                inline=False
            )

        return embed
