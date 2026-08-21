# Agent Integration & Architecture Guide — `tscribe`

> Operational guidelines, architectural contracts, and verification commands for AI coding agents.

<!-- bplate:todo-before-push -->
> **Before your first push to a remote:** update this file and `README.md` to
> describe what `tscribe` actually does, not the bplate scaffold
> boilerplate. Once you have, delete this callout and the HTML comment marker
> above it — a pre-push hook (`.bplate-hooks/pre-push`) checks for that marker
> and reminds you if it's still here. The reminder never blocks the push;
> it's a nudge, not a gate.

---

## 1. Project Purpose & Architecture

`tscribe` is a contract-verified system authored by Daniel Ramirez.

### Core Rules for Agents
1. **Contract-First:** Never break public interfaces without updating contracts and tests.
2. **Deterministic Outputs:** CLI commands should support `--json` output where appropriate.
3. **Verification Before Hand-off:** Run the verification test suite before declaring any task complete.

---

## 2. Essential Commands

```bash
# Run tests
mise run test 2>/dev/null || pytest 2>/dev/null || npm test 2>/dev/null || cargo test

# Lint / Static Analysis
mise run check 2>/dev/null || true
```

---

## 3. If This Grows Into a Family of Operations

Not every project needs this section — plenty of good tools are exactly one
thing, done well. But if `tscribe` starts looking less like "one tool"
and more like "several repeatable operations that share a shape" (a batch of
converters, a set of capture-then-verify steps, a library of generators),
notice that before hand-rolling a fourth near-identical module. Consider a
**process registry** instead:

- **process** — one self-contained, authored unit of work per folder.
- **registry** — folder-per-entry, a typed manifest per entry, and a
  generated index so the catalog is provable from the filesystem rather than
  hand-maintained (drift between the index and the folders is a bug, not a
  detail).
- **verification** — a gate that proves each unit's *output* against a
  contract, not just that it exited `0`.

If that shape fits, `bplate` itself is a worked example of it
(`profiles/`, `traits/`, `bplate registry generate --check`) — and it is not
a requirement to look like `bplate` specifically. The point is the shape,
not the tool.

---

**Note (Ciani, 2026-08-21):** this AGENTS.md is still the bplate scaffold boilerplate — see SPEC.md and docs/units/ for what tscribe actually is (a unification of tscribe-class-carnyx, tscribe-transcription-tool, and tscribe-capture-rig). This file gets its real content as Unit 08 lands.
