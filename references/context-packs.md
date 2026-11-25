# Context pack recipes (plan 7.4 / 8.3)

A context pack is the expert's entire view of the repository: the candidate,
the flagged code, and the minimum surrounding evidence needed to validate or
reject. Packs are built mechanically by `scripts/contextpack.py` — experts
never assemble their own context in scripted mode.

## Always included (v1, implemented)

1. **Candidate metadata** — the full candidate record as JSON.
2. **Flagged file** — in full when <= 400 lines; otherwise a 400-line window
   centered on the flagged range (the flagged lines are never cut, even if
   the range itself exceeds 400 lines). Rendered with 1-based line numbers.
3. **Callers** — for each defining symbol of the flagged lines (Ruby-aware:
   `def x`, `def self.x`, `class X`, `module X`; when the flagged range
   contains no definition, the innermost enclosing definition above it is
   used): `git grep -n --untracked` across the repo, up to 5 call sites per
   symbol, each with 5 lines of context on both sides. Definition lines,
   comment-only lines, and hits inside the flagged range are skipped.
   `scope.exclude` paths from config are excluded from the search.

## Per-domain extras (v2, implemented)

- `expert-db`: schema sections for any table named by the flagged file
  (model class names and `app/models/` paths tableized, plus explicit
  `self.table_name`), from `db/schema.rb` or — decide-and-note extension —
  `db/structure.sql`; plus the 3 most recent migrations touching those
  tables.
- `expert-security`: the auth/authz files (config `packs.security_globs`;
  default `app/controllers/application_controller.rb`, `app/policies/**`,
  `config/initializers/*auth*`; capped at 8 files).
- `expert-tests`: existing specs whose path mirrors the flagged file
  (`app/x/y.rb` -> `spec/x/y_spec.rb`, `lib/` both `spec/lib/` and `spec/`).

## Routing (used by run.sh since v2; unknown routes fall back to expert-backend)

| Condition | Route |
|---|---|
| `category: security` | `expert-security` |
| `category: test-gap` | `expert-tests` |
| `category: bug` AND (path matches `app/models`, `db/`, `*repository*` OR the snippet contains SQL) | `expert-db` |
| everything else | `expert-backend` |

## Budget

Hard cap ~1500 lines per pack. Truncation order: caller sites first (dropped
from the end), then migrations, then schema sections, then auth/mirror-spec
extras — each drop noted in the pack. The flagged-file window and candidate
metadata are never truncated.
