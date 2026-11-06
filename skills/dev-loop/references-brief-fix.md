# Fix brief (your own PR)

Read `references-brief-common.md` beside this file first.

You own this one PR's branch until you report back. Pull its reviews, its inline comments and its current state, including any `sched:*` labels and the merge gate's sticky comment.

**Requested changes:** address every open one.
- Fix each point with a test that fails without the fix.
- If a point is wrong, or needs a decision that isn't the author's, reply on the PR explaining why, or list it for the owner. Don't fix it silently.
- If the PR is blocked on something outside it (an unpublished package, someone else's decision), say so in one reply and stop. Don't work around it.

**Conflicts, or a `sched:needs-rebase` label:** merge main in, and resolve keeping both sides' intent.
- Regenerate generated files (migrations, API clients, lockfiles) the way the project generates them. Never hand-edit them into shape.
- Apply your branch's delta onto main's version of shared contract files.

**Red CI:** read the failed log first. A flake gets one re-run; anything else is a fix.

**Before pushing:**
- The build is clean.
- Any failing tests are known local-only failures *by name*, not just by count.
- If you merged main, nothing is left behind it.

Make the push conditional on those checks.

Push once, when finished. Pushing dismisses approvals, so say who needs to re-approve.

**Trailers:** keep `Merge-Group:` and `Depends-On:`. If your change creates a new pairing, add the trailers to both PRs.
