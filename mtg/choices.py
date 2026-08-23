"""Durable multiplayer choice sessions with per-seat visibility.

The game state stores only JSON-safe choice records. Live asyncio futures and
opaque engine objects are kept in ``GameState._choice_runtime`` and are never
sent to clients or written to disk. This gives Discord reconnects a stable
choice id while preventing one player's private options (or a simultaneous
answer) from leaking through ``visible_state``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Iterable, List, Optional


def _seat_id(game, player_index: int) -> int:
    player = game.players[player_index]
    seat = getattr(player, "seat_id", None)
    return player_index if seat is None else int(seat)


def _normalize_options(options: Iterable[Any]) -> tuple[List[Dict[str, Any]], List[Any]]:
    public: List[Dict[str, Any]] = []
    payloads: List[Any] = []
    for index, option in enumerate(options):
        if isinstance(option, dict):
            label = str(option.get("label", option.get("value", f"Choice {index}")))
            value = option.get("value", index)
            payload = option.get("payload", value)
        else:
            label = str(option)
            value = index
            payload = option
        public.append({"index": index, "label": label, "value": value})
        payloads.append(payload)
    return public, payloads


def create_choice(
    game,
    *,
    choice_type: str,
    chooser_indices: Iterable[int],
    options_by_player: Dict[int, Iterable[Any]] | Iterable[Any],
    private: bool = True,
    simultaneous: bool = False,
    timeout_seconds: float = 120.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create one serializable choice session and its live completion future."""
    indices = list(dict.fromkeys(int(index) for index in chooser_indices))
    chooser_ids = [_seat_id(game, index) for index in indices]
    choice_id = f"choice-{uuid.uuid4().hex[:12]}"
    public_options: Dict[str, List[Dict[str, Any]]] = {}
    runtime_payloads: Dict[str, List[Any]] = {}

    shared_options = not isinstance(options_by_player, dict)
    for index, chooser_id in zip(indices, chooser_ids):
        raw = (options_by_player if shared_options
               else options_by_player.get(index,
                    options_by_player.get(chooser_id, [])))
        visible, payloads = _normalize_options(raw)
        public_options[str(chooser_id)] = visible
        runtime_payloads[str(chooser_id)] = payloads

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    record = {
        "choice_id": choice_id,
        "type": str(choice_type),
        "chooser_player_ids": chooser_ids,
        "options_by_player": public_options,
        "responses": {},
        "private": bool(private),
        "simultaneous": bool(simultaneous),
        "timeout_seconds": float(timeout_seconds),
        "metadata": dict(metadata or {}),
        "complete": False,
        "timed_out": False,
    }
    # Q-J slice 3: link the choice to the resolution that opened it, so a
    # recovered job knows it was BLOCKED on a human answer rather than
    # merely unfinished. The link is APPEND-ONLY and "unresolved" is derived
    # from the record's own `complete` flag — maintaining a removal list
    # would mean catching every completion path (submit, timeout, cancel,
    # elimination), and a missed one would strand a job forever.
    owning_job = getattr(game, "_active_resolution_job_id", None)
    if owning_job:
        record["owning_job"] = str(owning_job)
        job = game.resolution_jobs.get(str(owning_job))
        if job is not None and choice_id not in job.unresolved_choice_ids:
            job.unresolved_choice_ids.append(choice_id)
    game.pending_choices[choice_id] = record
    game._choice_runtime[choice_id] = {
        "future": future,
        "payloads": runtime_payloads,
    }
    return record


def pending_choices_for(game, player_index: int) -> List[Dict[str, Any]]:
    """Return unresolved choice records belonging to one stable seat."""
    chooser_id = _seat_id(game, player_index)
    return [
        record for record in game.pending_choices.values()
        if (not record.get("complete")
            and chooser_id in record.get("chooser_player_ids", []))
    ]


def choice_views_for(game, player_index: int) -> List[Dict[str, Any]]:
    """Filter private options and seal simultaneous responses per viewer."""
    viewer_id = _seat_id(game, player_index)
    views: List[Dict[str, Any]] = []
    for record in game.pending_choices.values():
        chooser_ids = record.get("chooser_player_ids", [])
        is_chooser = viewer_id in chooser_ids
        view = {
            "choice_id": record.get("choice_id"),
            "type": record.get("type"),
            "chooser_player_ids": list(chooser_ids),
            "private": bool(record.get("private", True)),
            "simultaneous": bool(record.get("simultaneous", False)),
            "complete": bool(record.get("complete", False)),
            "responded": str(viewer_id) in record.get("responses", {}),
        }
        if is_chooser or not record.get("private", True):
            options = record.get("options_by_player", {})
            option_key = str(viewer_id)
            if not is_chooser and record.get("chooser_player_ids"):
                option_key = str(record["chooser_player_ids"][0])
            view["options"] = list(options.get(option_key, []))
        else:
            view["options"] = None
        # A simultaneous session exposes no selected index to any viewer
        # until every seat has committed. The completion consumer may then
        # narrate the aggregate result through the appropriate public channel.
        if record.get("complete") and not record.get("simultaneous") and is_chooser:
            view["response"] = record.get("responses", {}).get(str(viewer_id))
        views.append(view)
    return views


def submit_choice(game, player_index: int, option_index: int,
                  choice_id: Optional[str] = None) -> Dict[str, Any]:
    """Commit one seat's answer, resolving only after all required answers."""
    candidates = pending_choices_for(game, player_index)
    if choice_id:
        candidates = [c for c in candidates if c.get("choice_id") == choice_id]
    if not candidates:
        return {"success": False, "message": "No pending choice for your seat."}
    if len(candidates) > 1:
        return {
            "success": False,
            "message": "More than one choice is pending; include its choice id.",
        }
    record = candidates[0]
    chooser_id = _seat_id(game, player_index)
    key = str(chooser_id)
    options = record.get("options_by_player", {}).get(key, [])
    if option_index < 0 or option_index >= len(options):
        return {
            "success": False,
            "message": f"Invalid choice. Choose 0-{max(0, len(options) - 1)}.",
        }
    if key in record.get("responses", {}):
        return {"success": False, "message": "Your answer is already sealed."}

    record.setdefault("responses", {})[key] = int(option_index)
    complete = all(
        str(seat) in record["responses"]
        for seat in record.get("chooser_player_ids", [])
    )
    record["complete"] = complete
    runtime = game._choice_runtime.get(record["choice_id"], {})
    payloads = runtime.get("payloads", {})

    def _payload_for(seat_key: str, selected_index: int):
        seat_payloads = payloads.get(seat_key, [])
        if 0 <= selected_index < len(seat_payloads):
            return seat_payloads[selected_index]
        seat_options = record.get("options_by_player", {}).get(seat_key, [])
        if 0 <= selected_index < len(seat_options):
            return seat_options[selected_index].get("value")
        return None

    selected = _payload_for(key, option_index)

    if complete:
        if record.get("simultaneous"):
            result = {
                int(seat): _payload_for(
                    str(seat), record["responses"][str(seat)])
                for seat in record.get("chooser_player_ids", [])
            }
        else:
            result = selected
        future = runtime.get("future")
        if future is not None and not future.done():
            future.set_result(result)
    return {
        "success": True,
        "choice_id": record["choice_id"],
        "complete": complete,
        "selected_label": options[option_index]["label"],
        "result": selected if complete and not record.get("simultaneous") else None,
    }


async def wait_for_choice(game, choice_id: str, *,
                          fallback_index: int = 0) -> Any:
    """Wait for a session; on timeout commit a deterministic legal fallback."""
    record = game.pending_choices.get(choice_id)
    runtime = game._choice_runtime.get(choice_id)
    if record is None or runtime is None:
        raise RuntimeError(f"Choice session {choice_id} is not live")
    future = runtime["future"]
    try:
        return await asyncio.wait_for(
            asyncio.shield(future),
            timeout=max(0.0, float(record.get("timeout_seconds", 120.0))),
        )
    except asyncio.TimeoutError:
        record["timed_out"] = True
        for index, player in enumerate(game.players):
            seat = _seat_id(game, index)
            if (seat in record.get("chooser_player_ids", [])
                    and str(seat) not in record.get("responses", {})):
                options = record.get("options_by_player", {}).get(str(seat), [])
                if options:
                    submit_choice(
                        game, index, min(fallback_index, len(options) - 1), choice_id)
        if future.done():
            return future.result()
        raise
    finally:
        if record.get("complete"):
            game.pending_choices.pop(choice_id, None)
            game._choice_runtime.pop(choice_id, None)
            rules = getattr(game, '_rules_engine', None)
            engine = getattr(rules, 'engine_ref', None)
            save_game = getattr(engine, 'save_game', None)
            if callable(save_game):
                save_game(game)


def format_choice_prompt(record: Dict[str, Any], chooser_player_id: int) -> str:
    """Format only one seat's options; callers decide DM vs public routing."""
    options = record.get("options_by_player", {}).get(str(chooser_player_id), [])
    lines = [f"Choice `{record.get('choice_id')}` ({record.get('type')}):"]
    lines.extend(f"`{option['index']}` - {option['label']}" for option in options)
    lines.append(f"Reply with `!choose {record.get('choice_id')} <number>`.")
    if record.get("simultaneous"):
        lines.append("Your answer stays sealed until every required seat answers.")
    return "\n".join(lines)
