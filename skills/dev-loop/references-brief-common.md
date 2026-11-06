# Shared brief for dev-loop subagents

**Use the environment you're in:** this developer's `gh` login, git identity, clones, CLAUDE.md and memory notes, the way they normally work. The project's CLAUDE.md is the authority on conventions, build and test commands, and which tests are known to fail locally. Never put a token in a URL or a file.

**Work in your own worktree** of the developer's existing clone, named `loop-<repo>-<pr>`, created fresh and detached. If there's no clone, clone into a scratch folder. Chain every command after a `cd` with `&&`, so a failed `cd` can't run a merge in the wrong checkout. Don't touch other agents' worktrees. Remove yours when finished, unless it holds unpushed work.

**Limits, for every agent:**
- Never merge, enqueue, dequeue or run update-branch on a teammate's PR. The merge gate (one person) does that.
- Never push to a teammate's branch.
- Don't @-mention anyone in what you post.
- Don't start subagents.
- A product or scope decision goes in your report with its owner. Don't guess at it.
- Run targeted tests rather than the full suite unless the change is broad.
- If a tool call is denied, stop that step and report it.

**PR trailers** (the merge gate reads them): `Merge-Group: <name>` for PRs that must land together, and `Depends-On: <repo>#<n>` for order. Keep them intact when you edit a PR body.

**Report back** in at most 12 lines: PR, head SHA, what you did, what you verified and how, and anything left open with its owner.
