---
name: dev-loop
description: One pass of a developer's PR loop under merge-scheduler. Reviews teammates' PRs, re-approves after merge-only pushes, fixes requested changes and conflicts on your own PRs, approves the gate's resync PRs, and lists what needs a nudge. Never queues or merges (the merge gate does). Usually under /loop 5m. Triggers on "dev loop", "review loop", "pr loop", "check the prs".
---

# Dev loop

Everyone on the team runs this. One person, the merge-gate lease holder, runs `merge-gate`, the only thing that queues or merges. This loop never does either. That's how the halves of a cross-repo change stop landing apart.

One pass, then stop. `/loop 5m /dev-loop` repeats it.

## Before the first pass

- It uses the environment you already work in: your `gh` login, git identity, clones, CLAUDE.md and memory. It also reads the shared `merge-scheduler.json` config, which lists the repos and the team.
- Check no other session of yours is running this loop. Two loops race pushes and post reviews twice.

## Each pass

```bash
D=<this skill's base directory>
python3 $D/tools/devq.py            # uses your gh login (or GH_TOKEN); --json for machine use
```

Keep state in `dev-loop-state.json` in your session scratchpad: running agents, and which `(PR, head SHA, bucket)` you've already handled. Skip anything handled at the same head, or owned by a running agent. A fix that's blocked on someone else is handled once per head, not once per pass.

Work the buckets in this order:

1. **fix**: start a fix agent with `references-brief-fix.md`. A flaky CI run gets one re-run first.
2. **rereview** and **review**: start a review agent with `references-brief-review.md`. Use one per PR, or one for a small cross-repo pair.
3. **reapprove**: inline. Check only what changed since your approved commit (for a merge-only push, `git show --remerge-diff <merge>`), check CI is green, then re-approve and name the new head.
4. **resync**: the gate's empty-commit PR. If `gh pr diff` is empty, approve it.
5. **update**: your PR on a repo without a merge queue is behind main. Update *your own* branch. Never update a teammate's.
6. **ask** and **hint**: report them, so you can nudge reviewers wherever your team does that. Fix your own PR's trailers or `sched:hold` when the answer is clear.

At most 3 subagents at once, counting ones still running. When both kinds are waiting, take the fix that unblocks the most first, then alternate.

## Writing your own PRs

The merge gate reads these, so write them every time:
- **PRs that must land together** share a body line: `Merge-Group: ticket-123`.
- **Order, or a hard dependency:** `Depends-On: api#12`, or `#12` for the same repo. Without it, a group lands in the configured repo tier order.
- **Waiting on a decision before merge:** add the `sched:hold` label and say why in the body. Remove it when the decision is in.
- **Priority:** `prio:urgent`, `prio:high` or `prio:low`; none means normal. Urgent jumps the queue and costs everyone behind it a rebuild, so use it sparingly.
- Add trailers **before** a PR is approved. An approved, green PR with no group is a job on its own, and the gate may merge it alone.

## Never

- Queue, enqueue, merge or dequeue anything. The merge gate owns all of it.
- Update or push to a teammate's branch.
- Approve your own PR, or call it "ready to merge" before someone else approves.
- Route around a denied tool call. Report it.

## Report after a pass

If nothing changed, say so in one line. Otherwise group it: done (reviews posted, fixes pushed, with head SHAs), needs you, waiting on others.
