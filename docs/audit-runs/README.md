# Audit runs

Each subdirectory is one execution of the multi-perspective audit
(``/audit`` Claude Code slash command). Subdirectory naming:
``YYYY-MM-DD-HHMM`` (UTC).

## What's inside one run

```
2026-05-01-1430/
├── findings.md     ← consolidated, P0/P1/P2 + status open|fixed|wontfix|manual
├── ux.md           ← raw output from the UX auditor agent
├── backend.md      ← raw output from the Backend auditor agent
├── security.md     ← raw output from the Security auditor agent
└── business.md     ← raw output from the Business auditor agent
```

The four ``<perspective>.md`` files are the auditors' detailed
narratives — keep them as evidence for *why* a finding was raised.
``findings.md`` is the operator's working surface.

## Workflow

1. ``/audit`` — fan-out to four parallel sub-agents, write per-
   perspective files, build consolidated ``findings.md``.
2. ``/audit-fix`` — read the latest ``findings.md``, apply every
   finding flagged ``Auto-fixable: yes`` (one logical commit per
   batch, push to production), update statuses in place.
3. ``/audit-verify`` — run a fresh audit, diff against the most
   recent run; promote ``resolved`` / ``persisted`` / ``regressed`` /
   ``new`` per finding so progress is provable.

## Cadence

Manual today. To run on a schedule, use Claude Code's ``/schedule``
skill — for example:

```
/schedule '0 9 * * 1' /audit
```

(every Monday at 09:00 local). The ``/audit`` slash command itself
just does the audit; downstream ``/audit-fix`` and ``/audit-verify``
are explicit operator decisions.

## Why commit these to git?

* Audit trail across launches and post-launch periods.
* ``/audit-verify`` needs the previous ``findings.md`` to diff.
* Reading old runs is the cheapest way to remember "we already
  decided X is `wontfix` because Y" the next time it surfaces.

If a finding contains sensitive data (e.g. a real customer email
that the auditor accidentally captured), redact it in
``findings.md`` before committing.
