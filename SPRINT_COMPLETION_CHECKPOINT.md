# Sprint completion checkpoint — 2026-10-18

Status: IMPLEMENTATION AND OFFLINE TESTS READY; release/live checks pending. This branch is not yet deployed. Production remains 1.7.48,
commit 6889d763c5c09179b8f728d5bbda82df5adfbaeb. Do not report Sprint2 complete.
No account data or private audit bundles belong in this public repository.

## Completed in this checkpoint

- Corrected calibration boundary selection: half-open bins, with 1 included
  in the final bin, matching KPI aggregation. A sample below the existing
  minimum cannot borrow observations from its neighbouring bin.
- Added regression tests covering .50/.70/.85/1 and insufficient samples.
- Validation: 2 new calibration tests and 14 existing Sprint2 tests passed.
- Changes are repository-only staging. Do not claim the installed scanner
  skill or live server contains this fix yet.

## Remaining work, in dependency order

1. Define and implement a deterministic opportunity-cohort collector: freeze
   original opportunities/features before outcomes, store provenance, capture
   later outcomes/benchmark/costs, exclude overlap/lookahead. Preserve candidates
   not executed; keep simulations separate from actual trades. No invented data.
2. Bridge validated category/method identity into live candidate selection.
   Reproduced bug: real fuse_level_b_results rows have neither category nor
   method_id; a promoted scoped profile therefore falls back to balanced.
   Do not invent financial classifications merely to enable learned ranking.
3. Complete ticket/execution/close attribution and category KPI aggregation.
   Unknown links remain unknown; COPY excluded. Signed fee convention still
   needs actual verified accounting evidence before changing treatment.
4. Wire calibration into ordinary proposals, preserving original probability,
   sample/version/state references. Current calibrate() is otherwise unused.
5. Integrate primary/two-independent-source collection into selected-candidate
   workflow. Existing event_evidence module and report gate alone do not prove
   automatic collection. Preserve the global ten-candidate chat budget.
6. Complete automated mandate/policy/Registry-formula consistency coverage.
   Existing policy/hash pins are present; semantic coverage is not exhaustive.
7. Remove obsolete operative r6/r7 wording from skill instructions, preserving
   historical replay rules. Validate and publish one skill directory per commit.
8. Test combined flow offline with production-shaped outputs, not only module
   fixtures. Update versions/pins only for the actual release, then merge once.
9. After warm readiness, run full real read-only scan -> candidate gates ->
   risk -> original Registry readback -> visible report, and validate a real
   scheduled run. Never fabricate a Sunday weekday-slot execution.

## Verified constraints and migration

- Current master is r8, ID14UkvOObo3bHHSdRcL72B_lWLAV6ajEBm; old ID is historical.
- Active automation, installed skill defaults, risk engine and Registry current
  metadata were migrated. User reports replacing the old project source link.
- No automatic trades, protection changes, cash movements or risk-threshold
  changes. Account actions still require individual user confirmation.
- Do not reset warm/cache or deploy this branch during incomplete work.
- Existing risk/scanner suites passed (124/190). Production server CI passed.
  Current local runtime lacks Flask: restore dependencies before a full rerun.

## Resume

Read this file, inspect this branch head and git status, then start item1.
Retain every completed change/test in a commit and update this checklist.
Use the current GitHub/Drive/skill state as evidence, not earlier completion claims.

## Completion implementation

Collector, full opportunity archive, neutral DISCOVERY ranking scope, immutable
execution/exit attribution, category KPIs, ordinary confidence calibration, capped
source collector and Registry formula/mandate checks have been implemented.
Read references/completion-flow.md in the installed scanner skill for orchestration.
Independent offline validation: full synthetic lifecycle and missing-ID matrix passed.
Final release must verify CI, skill push, live hashes and actual persistence.
A scheduled-run proof requires a genuine scheduled run; do not invent one.
