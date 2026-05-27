---
name: prior-auth-packet-builder
description: Build a concise prior authorization packet from local case files and payer policy docs.
---

# Prior Auth Packet Builder

Use this skill when a case requires prior authorization review, referral validation, imaging review,
or payer-specific policy checks.

## Workflow

1. Inspect `case/scenario.json` and `case/transcript.txt`.
2. Use `rg` against `policies/` to find payer, prior auth, referral, imaging, and PPO guidance.
3. Read only the most relevant policy files.
4. Create `output/policy_findings.md` with:
   - case summary
   - matched policy files
   - prior auth determination
   - referral determination
   - missing information
5. Create `output/human_review_checklist.md` with:
   - what a human reviewer should verify
   - what to tell the patient
   - what queue should own the case

## Rules

- Use targeted `rg` searches over broad file reads.
- Only cite policy files you actually inspected.
- Keep outputs concise and operational.
- If referral status is pending and prior auth is unclear, recommend human review.
