## What this changes

<!-- One or two sentences. If it's a rules fix, cite the CR section. -->

## How it's verified

- [ ] `python -m pytest tests -q` passes
- [ ] Added or updated a test covering this change
- [ ] (Engine changes) ratchet baselines in `tests/test_ratchets.py` unchanged,
      or bumped with a justification in the commit message
- [ ] (Optional) Ran an autoplay batch with `MTG_STRICT=1` — no new
      `[ETB-UNHANDLED]` / `[TRIGGER-UNHANDLED]` / tracebacks

## Notes

<!-- Anything a reviewer should know: log excerpts, edge cases you decided
     not to handle, follow-up work. -->
