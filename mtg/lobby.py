"""Durable Discord lobbies for human multiplayer games.

Lobby state is intentionally separate from :class:`GameState`: before a game
starts there are no cards in zones, no turn, and no rules-engine runtime to
restore.  A compact JSON record is enough to survive a bot restart while
keeping Discord user ids authoritative for seat ownership.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class LobbySeat:
    """One stable pregame seat, owned by exactly one Discord user id."""

    seat_id: int
    user_id: int
    display_name: str
    deck_data: Optional[Dict] = None
    ready: bool = False

    def to_dict(self) -> Dict:
        return {
            "seat_id": self.seat_id,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "deck_data": self.deck_data,
            "ready": self.ready,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "LobbySeat":
        return cls(
            seat_id=int(data["seat_id"]),
            user_id=int(data["user_id"]),
            display_name=str(data["display_name"]),
            deck_data=data.get("deck_data"),
            ready=bool(data.get("ready", False)),
        )


@dataclass
class GameLobby:
    """A bounded 3- or 4-seat lobby persisted by Discord thread id."""

    thread_id: int
    guild_id: Optional[int]
    owner_user_id: int
    format_name: str
    max_players: int
    seats: List[LobbySeat] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    next_seat_id: int = 0

    def __post_init__(self) -> None:
        if self.max_players not in (3, 4):
            raise ValueError("Human multiplayer lobbies require 3 or 4 seats")
        self.format_name = self.format_name.lower().strip()

    def seat_for_user(self, user_id: int) -> Optional[LobbySeat]:
        return next((seat for seat in self.seats
                     if seat.user_id == int(user_id)), None)

    def add_user(self, user_id: int, display_name: str,
                 deck_data: Optional[Dict] = None) -> LobbySeat:
        existing = self.seat_for_user(user_id)
        if existing is not None:
            return existing
        if len(self.seats) >= self.max_players:
            raise ValueError("This lobby is full")
        seat = LobbySeat(
            seat_id=self.next_seat_id,
            user_id=int(user_id),
            display_name=str(display_name),
            deck_data=deck_data,
        )
        self.next_seat_id += 1
        self.seats.append(seat)
        return seat

    def remove_user(self, user_id: int) -> bool:
        seat = self.seat_for_user(user_id)
        if seat is None:
            return False
        self.seats.remove(seat)
        return True

    def bind_deck(self, user_id: int, deck_data: Dict) -> LobbySeat:
        seat = self.seat_for_user(user_id)
        if seat is None:
            raise ValueError("That user has not joined this lobby")
        seat.deck_data = deck_data
        # Any deck mutation invalidates the prior readiness acknowledgement.
        seat.ready = False
        return seat

    @property
    def can_start(self) -> bool:
        return (
            len(self.seats) == self.max_players
            and all(seat.deck_data is not None and seat.ready
                    for seat in self.seats)
        )

    def to_dict(self) -> Dict:
        return {
            "thread_id": self.thread_id,
            "guild_id": self.guild_id,
            "owner_user_id": self.owner_user_id,
            "format_name": self.format_name,
            "max_players": self.max_players,
            "seats": [seat.to_dict() for seat in self.seats],
            "created_at": self.created_at,
            "next_seat_id": self.next_seat_id,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "GameLobby":
        seats = [LobbySeat.from_dict(seat) for seat in data.get("seats", [])]
        return cls(
            thread_id=int(data["thread_id"]),
            guild_id=(int(data["guild_id"])
                      if data.get("guild_id") is not None else None),
            owner_user_id=int(data["owner_user_id"]),
            format_name=str(data["format_name"]),
            max_players=int(data["max_players"]),
            seats=seats,
            created_at=str(data.get("created_at") or
                           datetime.now(timezone.utc).isoformat()),
            next_seat_id=int(data.get(
                "next_seat_id",
                max((seat.seat_id for seat in seats), default=-1) + 1,
            )),
        )


class LobbyStore:
    """Small atomic JSON store keyed by lobby thread id."""

    def __init__(self, directory: Path | str = "data/lobbies") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lobbies: Dict[int, GameLobby] = {}
        self.load_all()

    def _path(self, thread_id: int) -> Path:
        return self.directory / f"{int(thread_id)}.json"

    def load_all(self) -> Dict[int, GameLobby]:
        self._lobbies.clear()
        for path in self.directory.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    lobby = GameLobby.from_dict(json.load(handle))
                self._lobbies[lobby.thread_id] = lobby
            except (OSError, ValueError, TypeError, KeyError,
                    json.JSONDecodeError) as exc:
                print(f"[LOBBY-LOAD] Skipping {path.name}: {exc}")
        return dict(self._lobbies)

    def get(self, thread_id: int) -> Optional[GameLobby]:
        return self._lobbies.get(int(thread_id))

    def save(self, lobby: GameLobby) -> None:
        path = self._path(lobby.thread_id)
        temp_path = path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(lobby.to_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        self._lobbies[lobby.thread_id] = lobby

    def delete(self, thread_id: int) -> None:
        thread_id = int(thread_id)
        self._lobbies.pop(thread_id, None)
        path = self._path(thread_id)
        if path.exists():
            path.unlink()

    def all(self) -> List[GameLobby]:
        return list(self._lobbies.values())
