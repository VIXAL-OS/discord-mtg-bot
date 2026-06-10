"""One-shot helper: compute the ratchet baselines for tests/test_ratchets.py.

Run from repo root: venv\\Scripts\\python.exe tools\\_compute_ratchet_baselines.py
Prints the per-file broad-except counts and the undeclared-staple census so
the test file's baseline constants can be updated deliberately.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import fields as dc_fields
from mtg.models import Card, Player, GameState

declared = {
    "game": {f.name for f in dc_fields(GameState)},
    "player": {f.name for f in dc_fields(Player)},
    "card": {f.name for f in dc_fields(Card)},
}
staple_re = re.compile(r"\b(game|player|card)\.(_[a-z0-9_]+)\s*=[^=]")
except_re = re.compile(r"except(\s+Exception(\s+as\s+\w+)?)?\s*:")

total = 0
per_attr = {}
print("--- per-file broad-except counts (mtg/):")
for p in sorted(Path("mtg").glob("*.py")):
    text = p.read_text(encoding="utf-8")
    n_exc = len(except_re.findall(text))
    if n_exc:
        print(f'    "{p.name}": {n_exc},')
    for m in staple_re.finditer(text):
        var, attr = m.group(1), m.group(2)
        if attr in declared[var]:
            continue
        total += 1
        per_attr[attr] = per_attr.get(attr, 0) + 1

print(f"--- undeclared staple sites total: {total}")
for k, v in sorted(per_attr.items(), key=lambda x: -x[1])[:20]:
    print(f"    {k}: {v}")
