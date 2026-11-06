"""GitHub access for merge-scheduler, through the `gh` CLI.

Auth: GH_TOKEN if set, otherwise gh's own login. Standard library only.
Keep this file identical in both skills (`make sync` copies it; the tests check).
"""
import json
import subprocess
import time

_PR_FIELDS = """
id number title body isDraft createdAt updatedAt isInMergeQueue
author{login} repository{name} baseRefName headRefName headRefOid
mergeStateStatus reviewDecision
labels(first:30){nodes{name}}
files(first:100){nodes{path}}
reviews(last:60){nodes{author{login} state submittedAt commit{oid}}}
commits(last:1){nodes{commit{statusCheckRollup{state contexts(first:60){nodes{
  __typename ... on CheckRun{name conclusion status} ... on StatusContext{context state}}}}}}}
"""
_BAD = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")


def gh(args, input_=None):
    """Run gh, retrying transient failures. Returns stdout."""
    for attempt in range(5):
        try:
            return subprocess.run(["gh", *args], check=True, capture_output=True, text=True, input=input_).stdout
        except subprocess.CalledProcessError as e:
            err = e.stderr or ""
            if attempt == 4 or ("HTTP 4" in err and "HTTP 429" not in err):
                raise RuntimeError(f"gh {' '.join(args[:3])}… failed: {err.strip()[:300]}") from e
            time.sleep(3 * (attempt + 1))


def gql(query, **variables):
    args = ["api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        if v is None:
            continue
        flag = "-F" if isinstance(v, (int, bool)) else "-f"
        args += [flag, f"{k}={json.dumps(v) if isinstance(v, bool) else v}"]
    out = json.loads(gh(args))
    if out.get("errors"):
        raise RuntimeError(f"GraphQL: {out['errors'][0].get('message')}")
    return out["data"]


def rest(method, path, body=None):
    args = ["api", "-X", method, path] + (["--input", "-"] if body is not None else [])
    out = gh(args, json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else None


def me():
    return gh(["api", "user", "--jq", ".login"]).strip()


def key(repo, n):
    return f"{repo}#{n}"


def _latest_per_reviewer(nodes):
    """Each reviewer's latest review that took a position (or their latest comment if that's all).
    GraphQL's latestReviews drops some dismissed reviews, so this works from the full list."""
    latest = {}
    for r in nodes:                                   # oldest first
        by = (r["author"] or {}).get("login", "?")
        rec = dict(by=by, state=r["state"], at=r["submittedAt"], sha=(r["commit"] or {}).get("oid", ""))
        if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED") or by not in latest \
                or latest[by]["state"] == "COMMENTED":
            latest[by] = rec
    return list(latest.values())


def _normalise(p):
    roll = (p["commits"]["nodes"][0]["commit"].get("statusCheckRollup") if p["commits"]["nodes"] else None) or {}
    ctx = (roll.get("contexts") or {}).get("nodes") or []
    name = lambda c: c.get("name") or c.get("context")  # noqa: E731
    repo = p["repository"]["name"]
    return dict(
        key=key(repo, p["number"]), id=p["id"], repo=repo, n=p["number"], title=p["title"], body=p["body"] or "",
        author=(p["author"] or {}).get("login", "?"), draft=p["isDraft"], base=p["baseRefName"],
        head=p["headRefName"], sha=p["headRefOid"], ms=p["mergeStateStatus"], rd=p["reviewDecision"],
        in_queue=p["isInMergeQueue"], ci=roll.get("state"),
        bad=[name(c) for c in ctx if (c.get("conclusion") or c.get("state")) in _BAD],
        pending=[name(c) for c in ctx if c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING")
                 or c.get("state") in ("PENDING", "EXPECTED")],
        created=p["createdAt"], updated=p["updatedAt"],
        labels=[x["name"] for x in p["labels"]["nodes"]], files=[f["path"] for f in p["files"]["nodes"]],
        reviews=_latest_per_reviewer(p["reviews"]["nodes"]),
    )


class Client:
    def __init__(self, cfg):
        self.cfg, self.org = cfg, cfg["org"]

    def repos(self):
        if self.cfg["repos"]:
            return sorted(self.cfg["repos"])
        names = set()
        for prefix in self.cfg["repo_prefixes"]:
            d = gql("query($q:String!){search(query:$q,type:REPOSITORY,first:100){nodes{... on Repository{name}}}}",
                    q=f"org:{self.org} {prefix} in:name archived:false")["search"]["nodes"]
            names |= {n["name"] for n in d if n and n["name"].lower().startswith(prefix.lower())}
        return sorted(names)

    def open_prs(self, batch=5):
        """Open PRs by the watched authors, keyed 'repo#N'. Lists each repo's PRs rather than searching
        by author: an org-wide bot search can page through tens of thousands of PRs."""
        out, names, authors = {}, self.repos(), set(self.cfg["authors"])
        for i in range(0, len(names), batch):
            part = names[i:i + batch]
            q = "query{" + " ".join(
                f'r{j}:repository(owner:"{self.org}",name:"{n}"){{pullRequests(states:OPEN,first:50,'
                f'orderBy:{{field:UPDATED_AT,direction:DESC}}){{nodes{{{_PR_FIELDS}}}}}}}'
                for j, n in enumerate(part)) + "}"
            for repo in gql(q).values():
                for n in (repo or {}).get("pullRequests", {}).get("nodes", []):
                    if not authors or (n.get("author") or {}).get("login") in authors:
                        p = _normalise(n)
                        out[p["key"]] = p
        return out

    def queues(self, repos):
        out = {}
        for repo in sorted(set(repos)):
            d = gql("query($o:String!,$r:String!){repository(owner:$o,name:$r){mergeQueue(branch:\"main\"){"
                    "entries(first:20){nodes{position pullRequest{number}}}}}}", o=self.org, r=repo)["repository"]
            mq = d["mergeQueue"]
            nodes = sorted((mq or {}).get("entries", {}).get("nodes", []), key=lambda e: e["position"])
            out[repo] = dict(enabled=mq is not None, entries=[key(repo, e["pullRequest"]["number"]) for e in nodes])
        return out

    def _pr(self, k, fields):
        repo, n = k.rsplit("#", 1)
        return gql("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){"
                   + fields + "}}}", o=self.org, r=repo, n=int(n))["repository"]["pullRequest"]

    def pr_state(self, k):
        d = self._pr(k, "state merged")
        return dict(state=d["state"], merged=d["merged"]) if d else dict(state="MISSING", merged=False)

    def removal_reason(self, k):
        d = self._pr(k, "timelineItems(last:1,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{"
                        "... on RemovedFromMergeQueueEvent{reason}}}")["timelineItems"]["nodes"]
        return (d[0]["reason"] or "unknown").lower() if d else "unknown"

    def last_queue_event(self, k):
        nodes = self._pr(k, "timelineItems(last:1,itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT])"
                            "{nodes{__typename ... on RemovedFromMergeQueueEvent{reason}}}")["timelineItems"]["nodes"]
        if not nodes:
            return None
        if nodes[0]["__typename"] == "AddedToMergeQueueEvent":
            return ("added", None)
        return ("removed", (nodes[0].get("reason") or "unknown").lower())

    def enqueue(self, pr_id, sha, jump=False):
        return gql("mutation($id:ID!,$sha:GitObjectID!,$j:Boolean){enqueuePullRequest(input:{pullRequestId:$id,"
                   "expectedHeadOid:$sha,jump:$j}){mergeQueueEntry{position}}}", id=pr_id, sha=sha, j=jump)

    def merge(self, k, sha):
        repo, n = k.rsplit("#", 1)
        gh(["pr", "merge", n, "-R", f"{self.org}/{repo}", "--merge", "--match-head-commit", sha])
