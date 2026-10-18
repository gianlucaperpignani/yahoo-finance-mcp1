# Collector/target correction checkpoint — 2026-10-19

Release1.7.50: code staged; deployment and live checks recorded separately.
Previous production1.7.49 was technically validated with partial cache, not as
an autonomous profitable agent. Do not infer full completion from this file.

- Exit plan is frozen from real exchange sessions; historical exact-session
  closes may be acquired later; subsequent-session live quotes are rejected.
- Entry quote and benchmark identity/session are validated. No closed-market entry.
- Targets derive from authenticated r8; no future reset is enabled.
- Pipeline/report1.6.0 require explicit target progress; risk1.3.7 unchanged.
- Cost adapter uses verified read-only schema; absent exit fee proof stays missing.
- Forward checks use synthetic data only. Real cohort and weekday scheduled
  proof require future observations and actual delivery/readback.
- No sample, probability, quote, source or approval may be fabricated.
- Make one main merge after CI; do not repeatedly reset warm during tests.
