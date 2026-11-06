# Review brief (a teammate's PR)

Read `references-brief-common.md` beside this file first.

This is a real, full review. Read every changed file, the ticket it cites where you can, and the callers the change reaches. Look for:
- correctness;
- security and permission gaps, and data leaking across tenants or users;
- migrations and schema changes, following the project's rules;
- input validation;
- API contracts kept in step between producer and consumer;
- anything the project's CLAUDE.md says reviews must check.

Verify each finding by running or tracing the code, not by reading alone. Drop what doesn't survive.

**The bar:** approve unless something is blocking or high-severity. Mediums and lows go in the approval body, marked non-blocking.

**Merge grouping:** if this PR must land with another (an API change with its UI, a library with its consumers) and the `Merge-Group:` / `Depends-On:` trailers are missing or wrong, say so in the review. A missing group is how a half lands alone.

**Post the review** as yourself, in plain language:
1. Re-check the head SHA just before posting. If it moved, review the new commits first.
2. `gh pr review N -R <owner>/<repo> --approve --body-file <file>`, or `--request-changes` for a blocker.
3. Name the head SHA in the body. For each finding give file:line, what goes wrong, and the fix.

Never review your own PR.
