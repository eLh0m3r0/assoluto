---
name: audit-fix
description: Triage the latest audit run and auto-fix every finding marked Auto-fixable=yes. Commits + pushes per logical batch.
argument-hint: "[run-id]   (optional, defaults to latest)"
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, Agent
---

You are the **fix orchestrator**. Walk the most recent audit run
(or the run id passed as ``$ARGUMENTS`` if any) and apply every
finding marked ``Auto-fixable: yes`` and ``status: open``. Each
fix is a real code change with tests; no shortcuts.

## Resolve target run

```bash
RUN_ID="${1:-$(ls -t docs/audit-runs/ | head -n 1)}"
```

If ``docs/audit-runs/$RUN_ID/findings.md`` doesn't exist, abort
with a message asking the user to run ``/audit`` first.

## Plan

1. Read ``docs/audit-runs/$RUN_ID/findings.md`` end to end.
2. Build a worklist: each finding with ``Auto-fixable: yes`` AND
   ``status: open``. Discard the rest (they go to the manual queue;
   surface them in the final summary).
3. Group fixes into **logical batches** by perspective and adjacency:
   - "UX dark-mode polish across X templates" = one batch
   - "Backend: drop unused imports" = one batch
   - Don't mix perspectives in a single commit — keeps the audit
     trail readable.
4. Quick risk check per batch — anything that looks risky despite
   ``Auto-fixable: yes``? Demote it to ``manual``, note the reason
   in ``findings.md``, and skip. The agent might have been wrong.

## Execute (per batch)

For each batch:

1. Apply the fix(es) using Edit/Write.
2. ``.venv/bin/ruff check . --fix`` then ``.venv/bin/ruff format .``.
3. ``.venv/bin/python3 -m pytest tests/ -q`` — full suite.
4. If the batch touched i18n strings:
   - ``.venv/bin/pybabel extract ... && pybabel update ... --no-fuzzy-matching``
   - apply CS + DE translations for any new msgids (default skeleton
     is acceptable; flag in finding's note that DE pass is pending if
     you can't do it inline)
   - ``.venv/bin/pybabel compile -d app/locale``
5. ``.venv/bin/mypy app/`` — no new errors.
6. If anything in steps 2–5 fails, **revert this batch only** (git
   checkout the affected files) and update findings.md status to
   ``manual`` with the failure reason. Do NOT push partially-broken
   batches.
7. ``git add <files> && git commit -m "<descriptive message>"``.
   Commit message format:
   ```
   fix(audit:<RUN_ID>): <short>

   Findings addressed:
   * F-UX-007 (P1): dark mode invisible text on /app/admin/audit
   * F-UX-009 (P2): focus ring missing on bulk-transition button

   <2-3 sentences of context>
   ```
8. Mark each finding's status to ``fixed`` (with the commit SHA) in
   ``findings.md``.

## Push

After all batches are committed:

1. ``git push origin main``
2. ``git push origin main:production`` — triggers Hetzner deploy.
3. Wait for the GitHub Action to finish (poll
   ``gh run list --limit 1 --branch production`` every 20s up to
   3 minutes, or until ``conclusion=success``).
4. Smoke check: ``curl -sf https://assoluto.eu/healthz`` and
   ``/readyz``. If either fails, alert the user immediately and
   STOP — do not pretend success.

## Report

Final summary to the user:

* Run ID
* Number of findings auto-fixed (with their IDs)
* Number deferred to manual (with their IDs and one-line reason)
* Number of commits pushed + the prod deploy status
* "Run `/audit-verify` to re-walk and confirm fixes held."
