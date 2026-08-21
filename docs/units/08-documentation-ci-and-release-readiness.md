# Unit 08 — Documentation, CI & Release Readiness

## Objective
Ship `tscribe` with the documentation and CI shape every LUFS tool is expected to have, per `bplate`'s own documentation standard.

## Context
- Precedent: `lufs-audio/bplate` `docs/units/08-documentation-and-workflow-standard.md` — the four-layer standard (in-repo contract docs, CHANGELOG, CI, KB product suite)
- `ci-cd-authoring` skill / `ci-and-runners` KB suite — fleet house rules (system Python via apt on arm64 Pis, no sudo, exec-capped tmpfs)
- `lufs-audio/bplate` `docs/units/07-lufs-primitive-cli-naming.md` — `tscribe` (7 chars) is grandfathered as acceptable; no rename needed

## Status (2026-08-21)
The `github-ci` trait's `.github/workflows/ci.yml` could not be pushed automatically (GitHub App token lacks `workflow` scope — the same fleet-wide constraint documented in the ci-cd-authoring skill). Its content is in the PR description for Daniel to add by hand; once added, the "CI" acceptance criterion below is satisfied by that same file.

## Acceptance criteria
- [ ] `SPEC.md` + `docs/units/*.md` live in-repo (DONE — this very commit).
- [ ] `CHANGELOG.md` gets a real entry per notable PR from Unit 01 onward.
- [ ] `.github/workflows/` targets `runs-on: [self-hosted, lufs]` per fleet rules; at minimum a build+test workflow gating merges. (bplate's own `github-ci` trait ships a generic `ubuntu-latest` workflow — needs updating to the fleet's `[self-hosted, lufs]` + system-Python convention per the ci-cd-authoring skill, not used as-is.)
- [ ] `README.md` explains the tool and links to `SPEC.md`, `docs/units/`, and (once it exists) the KB product suite — cross-reference, don't duplicate. (Currently still bplate's generic scaffold text plus a pointer note — real content pending.)
- [ ] A stub notes the full `lufs-audio/kb docs/product/tscribe/` narrative suite is a follow-on task once the tool has shipped with real usage to narrate — do NOT write it now.
- [ ] This unit's own table stays current as rows resolve.

## Boundaries — do NOT touch
- Does not write the KB product doc suite itself.
- Does not implement functional code — documentation and CI config only.
- Cannot commit `.github/workflows/*.yml` directly without repo `workflow` scope — hand the YAML to Daniel to commit, same as every other fleet repo.

## Output
- `CHANGELOG.md`, `README.md` (real content), `.github/workflows/ci.yml` (fleet-adapted, handed to Daniel)
- Commit style: `docs(release): add changelog, real README, and fleet CI workflow`

## Verification
- CI workflow runs and passes on a real PR (build + pytest across Units 01-07)
- README and SPEC.md cross-reference with no orphaned/duplicate content