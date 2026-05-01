---
name: audit-verify
description: Re-run the audit and diff against the previous run. Marks findings resolved / regressed / new.
argument-hint: "[no args]"
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent
---

You are the **audit verifier**. Run a fresh 4-perspective audit
exactly the way ``/audit`` does, then diff its findings against the
previous run to prove whether the fixes held.

## Setup

1. Find the previous run id:
   ```bash
   PREV_RUN=$(ls -t docs/audit-runs/ | head -n 1)
   ```
   If none exists, abort with: "No previous audit run — start with
   ``/audit`` first."

2. Run a fresh audit by following the instructions in
   ``.claude/commands/audit.md`` end-to-end (paste them mentally;
   you don't need to invoke the slash command — just do the same
   work). New ``RUN_ID`` is now-timestamp.

## Diff

After both runs exist:

1. Read ``docs/audit-runs/$PREV_RUN/findings.md`` and the new
   ``docs/audit-runs/$RUN_ID/findings.md``.
2. Build the comparison block. For each finding in the new run,
   match it against the previous one by **title proximity** (Levenshtein-ish
   on the first line) and **Where field** equality. Be a bit
   generous — a finding that was "F-UX-007: dark text on
   /app/admin/audit" and now reads "F-UX-014: dark text on
   /app/admin/audit (still)" is the same issue.

3. Categorise every NEW-run finding into:
   - **resolved** — was in PREV, gone now (don't appear in new)
   - **persisted** — was in PREV with status ``open``, still in new
   - **regressed** — was in PREV with status ``fixed``, back in new
   - **new** — wasn't in PREV at all
   - **manual** — was in PREV with status ``manual``, still
     unresolved (operator action item)

4. Append to the new ``findings.md``:

```markdown
## Verification vs. previous run (`<PREV_RUN>`)

### Resolved (fixes held)
* F-UX-007 — Dark text on /app/admin/audit (fixed in <commit-sha>)

### Persisted (open in both runs)
* F-BIZ-009 — Founder bio missing from homepage

### Regressed (came back!)
* F-SEC-003 — CSP missing object-src 'none' (was fixed in <sha>, now back)

### New in this run
* F-UX-021 — Modal close button has no aria-label

### Manual / operator action
* Stripe live keys, lawyer review of Terms, OG-image PNG
```

5. If there are **regressed** findings, surface them PROMINENTLY at
   the top of the user-facing summary — those are the canaries that
   say "the auto-fix process is leaking" or "a recent commit
   undid prior work".

## Report

Final user-facing summary:

* New run ID + path to the diff'd ``findings.md``
* Counts: resolved / persisted / regressed / new (per perspective)
* Top 3 regressions if any (with the SHA that originally fixed them
  + the SHA range that introduced the regression — use ``git log
  --oneline <sha>..HEAD -- <file>`` to find culprits)
* Recommendation:
  - Zero regressions + ≥ 50% reduction in P0/P1 = "fixes held;
    consider closing the audit cycle".
  - Any regressions = "open a focused fix-up + add a test that
    catches the regression next time".
  - Persistence > 3 cycles = "demote to manual, the auto-fix isn't
    working for these".
