## Summary

Brief description of the change and the motivation behind it.

## Related issue

Closes #

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change adding functionality)
- [ ] Breaking change (fix or feature causing existing behaviour to change)
- [ ] Documentation update
- [ ] Refactor / cleanup (no functional change)

## Checklist

- [ ] `uv run ruff check .` is clean
- [ ] `uv run ruff format --check .` is clean
- [ ] `uv run pytest -q` passes locally
- [ ] New code has tests
- [ ] New environment variables documented in `docs/ENV.md` and `.env.example`
- [ ] New templates include `{{ csrf_input() }}` on every form
- [ ] New user-facing strings wrapped with `{{ _("...") }}` (once i18n lands)
- [ ] New migrations numbered sequentially with working `downgrade()`
- [ ] `CHANGELOG.md` updated under `[Unreleased]`

## How to test

Step-by-step instructions for reviewers to validate the change locally.

## Screenshots / recordings

(For UI changes — otherwise delete this section.)
