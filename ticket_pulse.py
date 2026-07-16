#!/usr/bin/env python3
"""
ticket-pulse: monthly ticket-flow stats for a GitHub org or user account.

Fetches every issue in every repo owned by the given account (open and
closed), then reports per month: opened, closed, net, and open backlog at
month end — plus the "active backlog" (open issues actually touched
recently), a breakdown by issue-type bucket, a self-contained HTML
dashboard, and an optional LLM-written narrative of the month's themes.

Requires: Python 3.9+ and the GitHub CLI (`gh`), authenticated.

Usage:
  python3 ticket_pulse.py OWNER                        # stats to stdout
  python3 ticket_pulse.py OWNER --html dashboard.html  # leadership dashboard
  python3 ticket_pulse.py OWNER --snapshot history.csv # accumulate history
  python3 ticket_pulse.py OWNER --digest digest.md \\
                                --narrative report.md  # LLM themes report
  python3 ticket_pulse.py OWNER --month 2026-05        # a specific month
  python3 ticket_pulse.py OWNER --midmonth             # month-to-date check-in
  python3 ticket_pulse.py OWNER --buckets buckets.json # group issue types
"""

import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

REPOS_QUERY = """
query($owner: String!, $cursor: String) {
  repositoryOwner(login: $owner) {
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes { name isArchived hasIssuesEnabled issues { totalCount } }
    }
  }
}
"""

ISSUES_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, after: $cursor, states: [OPEN, CLOSED]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title state createdAt closedAt updatedAt
        issueType { name }
      }
    }
  }
}
"""

NARRATIVE_PROMPT = """\
You are an engineering chief of staff writing the monthly ticket report for a
non-technical leadership team. Below is a digest of one month's GitHub ticket
activity: every ticket opened and closed, with repo, issue type, title, and
(for closed tickets) how long it had been open. Write a short markdown
report, roughly 250-350 words:
- a 2-3 sentence headline summary of the month
- 3-5 themes (clusters of related work, bug patterns, cleanup of old tickets,
  areas of new investment), each with a one-line takeaway
- anything leadership should watch next month
Plain language; name the products/repos; reference tickets sparingly as
(repo#123). Distinguish genuinely new work from housekeeping. Do not restate
raw counts the digest already gives except where they carry the story.
"""


def gql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"gh api failed:\n{out.stderr}")
    return json.loads(out.stdout)["data"]


def fetch_all_issues(owner):
    repos, cursor = [], None
    while True:
        data = gql(REPOS_QUERY, owner=owner, cursor=cursor)["repositoryOwner"]
        if data is None:
            sys.exit(f"No GitHub org or user named {owner!r} (or no access)")
        page = data["repositories"]
        repos += [(r["name"], r["isArchived"]) for r in page["nodes"]
                  if r["hasIssuesEnabled"] and r["issues"]["totalCount"] > 0]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    if not repos:
        sys.exit(f"{owner} has no repositories with issues")

    rows = []
    for i, (repo, archived) in enumerate(repos, 1):
        print(f"  [{i}/{len(repos)}] {repo}...", file=sys.stderr)
        cursor = None
        while True:
            page = gql(ISSUES_QUERY, owner=owner, repo=repo,
                       cursor=cursor)["repository"]["issues"]
            for n in page["nodes"]:
                rows.append({
                    "repo": repo,
                    "repo_archived": archived,
                    "number": n["number"],
                    "title": n["title"],
                    "state": n["state"],
                    "type": (n.get("issueType") or {}).get("name") or "(no type)",
                    "created": datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")),
                    "closed": (datetime.fromisoformat(n["closedAt"].replace("Z", "+00:00"))
                               if n["closedAt"] else None),
                    "updated": datetime.fromisoformat(n["updatedAt"].replace("Z", "+00:00")),
                })
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return rows


def clean_terminal_output(s):
    """Undo terminal control sequences some CLIs (ollama) emit even when
    piped: cursor-back (ESC[nD) rewrites are applied, everything else and
    carriage returns are stripped."""
    out, i = [], 0
    while i < len(s):
        m = re.match(r"\x1b\[([0-9]*)D", s[i:])
        if m:  # cursor moved back n columns: the next chars overwrite them
            n = int(m.group(1) or 1)
            del out[len(out) - min(n, len(out)):]
            i += m.end()
            continue
        m = re.match(r"\x1b\[[0-9;?]*[A-Za-z]", s[i:])
        if m:
            i += m.end()
            continue
        if s[i] == "\r":
            i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def month_key(dt):
    return f"{dt.year:04d}-{dt.month:02d}"


def iter_months(start, end):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def load_bucket_fn(path):
    """Buckets config maps bucket name -> list of issue-type names. Without a
    config, every issue type is its own bucket."""
    if not path:
        return None, lambda r: ("Untyped" if r["type"] == "(no type)"
                                else r["type"])
    with open(path) as f:
        mapping = json.load(f)
    inv = {t.lower(): b for b, types in mapping.items() for t in types}
    return mapping, lambda r: ("Untyped" if r["type"] == "(no type)"
                               else inv.get(r["type"].lower(), "Other"))


def main():
    ap = argparse.ArgumentParser(
        description="Monthly ticket-flow stats for a GitHub org or user")
    ap.add_argument("owner", help="GitHub org or user whose repos to analyze")
    ap.add_argument("--since", help="start the stdout table at YYYY-MM")
    ap.add_argument("--type", help="comma-separated issue types to restrict to")
    ap.add_argument("--buckets", help="JSON file grouping issue types into "
                                      "buckets (see buckets.example.json)")
    ap.add_argument("--active-days", type=int, default=90,
                    help="open issues touched within this many days count as "
                         "the active backlog (default 90)")
    ap.add_argument("--csv", help="dump the raw issue list to this CSV")
    ap.add_argument("--json", help="dump computed stats to this JSON file")
    ap.add_argument("--snapshot", help="append today's backlog counts to this "
                                       "CSV (run monthly to build history)")
    ap.add_argument("--html", help="render the dashboard to this HTML file")
    ap.add_argument("--month", metavar="YYYY-MM",
                    help="report a specific month instead of the last "
                         "complete one")
    ap.add_argument("--midmonth", action="store_true",
                    help="report the in-progress month to date instead of the "
                         "last complete month (skip --snapshot on these runs)")
    ap.add_argument("--digest", help="write a markdown digest of the report "
                                     "month's tickets (LLM-ready)")
    ap.add_argument("--narrative", help="pipe the digest through --llm-cmd and "
                                        "write a themes/trends report here")
    ap.add_argument("--llm-cmd", default="claude -p",
                    help="command that reads a prompt on stdin and writes the "
                         "narrative to stdout (default: 'claude -p'; e.g. "
                         "'ollama run llama3.1')")
    args = ap.parse_args()

    # Fail fast on bad inputs — the fetch takes minutes.
    for p in (args.csv, args.json, args.html, args.digest,
              args.narrative, args.snapshot):
        d = os.path.dirname(p) if p else ""
        if d and not os.path.isdir(d):
            sys.exit(f"Output directory does not exist: {d} (for {p})")
    if args.month:
        if args.midmonth:
            sys.exit("--month and --midmonth are mutually exclusive")
        try:
            datetime.strptime(args.month, "%Y-%m")
        except ValueError:
            sys.exit(f"--month must be YYYY-MM, got {args.month!r}")
        if args.month > month_key(datetime.now(timezone.utc)):
            sys.exit(f"--month {args.month} is in the future")
    bucket_map, bucket = load_bucket_fn(args.buckets)

    rows = fetch_all_issues(args.owner)
    if args.type:
        wanted = {t.strip().lower() for t in args.type.split(",")}
        rows = [r for r in rows if r["type"].lower() in wanted]
    if not rows:
        sys.exit("No issues matched.")

    added = Counter(month_key(r["created"]) for r in rows)
    closed = Counter(month_key(r["closed"]) for r in rows if r["closed"])
    first = min(month_key(r["created"]) for r in rows)
    now_month = month_key(datetime.now(timezone.utc))

    table, open_count = [], 0
    for mo in iter_months(first, now_month):
        open_count += added[mo] - closed[mo]
        table.append({"month": mo, "opened": added[mo], "closed": closed[mo],
                      "net": added[mo] - closed[mo], "backlog": open_count})
    shown = [t for t in table if not args.since or t["month"] >= args.since]

    print(f"# {args.owner} — ticket flow & backlog")
    print(f"_Generated {date.today()} · {len(rows)} issues"
          + (f" · types: {args.type}" if args.type else "") + "_\n")

    print("| Month | Opened | Closed | Net | Backlog at month end |")
    print("|-------|-------:|-------:|----:|---------------------:|")
    for t in shown:
        arrow = "🔻" if t["net"] < 0 else ("🔺" if t["net"] > 0 else "▪️")
        print(f"| {t['month']} | {t['opened']} | {t['closed']} | "
              f"{t['net']:+d} {arrow} | {t['backlog']} |")

    # Active backlog: open, repo not archived, touched within --active-days
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.active_days)
    open_rows = [r for r in rows if r["state"] == "OPEN"]
    active = [r for r in open_rows
              if not r["repo_archived"] and r["updated"] >= cutoff]
    dormant = len([r for r in open_rows if not r["repo_archived"]]) - len(active)
    in_archived = len([r for r in open_rows if r["repo_archived"]])
    active_buckets = Counter(bucket(r) for r in active)
    untyped_open = sum(1 for r in open_rows if r["type"] == "(no type)")

    print(f"\n**Active backlog (open, updated in last {args.active_days} days, "
          f"non-archived repos): {len(active)}**")
    print("  by bucket: "
          + " · ".join(f"{k}: {v}" for k, v in active_buckets.most_common()))
    print(f"  (plus {dormant} dormant and {in_archived} in archived repos "
          f"= {len(open_rows)} total open)")

    backlog_buckets = Counter(bucket(r) for r in open_rows)
    print("\n**Total open by bucket:** "
          + " · ".join(f"{k}: {v}" for k, v in backlog_buckets.most_common()))

    # Which month does this report headline? (--month / --midmonth / default;
    # shared by the stdout summary, --html, --digest and --narrative)
    def label(mo):
        return datetime.strptime(mo, "%Y-%m").strftime("%b %y")

    def full_name(mo):
        return datetime.strptime(mo, "%Y-%m").strftime("%B")

    complete = table[:-1][-12:]          # last 12 complete months
    current = table[-1]                  # month in progress
    by_bucket = lambda mo, field: Counter(
        bucket(r) for r in rows
        if (r[field] and month_key(r[field]) == mo))

    if args.month:
        idx = {t["month"]: i for i, t in enumerate(table)}
        if args.month not in idx:
            sys.exit(f"No data for {args.month} — {args.owner}'s issues span "
                     f"{table[0]['month']} to {table[-1]['month']}")
        i = idx[args.month]
        rep, prev = table[i], (table[i - 1] if i > 0 else None)
    elif args.midmonth:
        rep, prev = current, complete[-1]
    else:
        rep, prev = complete[-1], (complete[-2] if len(complete) > 1 else None)

    if rep is current:
        rep_name = f"{full_name(rep['month'])} so far"
        report_month = (f"{full_name(rep['month'])} {rep['month'][:4]} "
                        f"· mid-month check-in")
        prev_note = lambda n: f"{n:,} in all of {full_name(prev['month'])}"
    else:
        rep_name = full_name(rep["month"])
        report_month = datetime.strptime(rep["month"], "%Y-%m").strftime("%B %Y")
        prev_note = lambda n: f"{n:,} in {full_name(prev['month'])}" if prev else ""
    mo_open = by_bucket(rep["month"], "created")
    mo_close = by_bucket(rep["month"], "closed")

    print(f"\n**{rep['month']} by bucket (opened / closed):** "
          + " · ".join(f"{b}: {mo_open.get(b, 0)}/{mo_close.get(b, 0)}"
                       for b in sorted(set(mo_open) | set(mo_close))))

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["repo", "repo_archived", "number",
                                              "title", "state", "type", "bucket",
                                              "created", "closed", "updated"])
            w.writeheader()
            for r in rows:
                w.writerow({**{k: r[k] for k in ("repo", "repo_archived", "number",
                                                 "title", "state", "type")},
                            "bucket": bucket(r), "created": r["created"].date(),
                            "closed": r["closed"].date() if r["closed"] else "",
                            "updated": r["updated"].date()})
        print(f"\nWrote {args.csv}")

    if args.json:
        monthly_buckets = {}
        for t in table:
            mo = t["month"]
            monthly_buckets[mo] = {
                **t,
                "opened_by_bucket": dict(by_bucket(mo, "created")),
                "closed_by_bucket": dict(by_bucket(mo, "closed")),
            }
        with open(args.json, "w") as f:
            json.dump({"generated": str(date.today()),
                       "owner": args.owner,
                       "active_days": args.active_days,
                       "active_backlog": len(active),
                       "active_by_bucket": dict(active_buckets),
                       "dormant_open": dormant,
                       "open_in_archived_repos": in_archived,
                       "backlog_by_bucket": dict(backlog_buckets),
                       "monthly": monthly_buckets}, f, indent=2)
        print(f"Wrote {args.json}")

    if args.snapshot:
        line = {"date": str(date.today()),
                "active_backlog": len(active),
                "total_backlog": len(open_rows),
                "dormant": dormant,
                "in_archived_repos": in_archived}
        try:
            has_header = bool(open(args.snapshot).readline())
        except FileNotFoundError:
            has_header = False
        with open(args.snapshot, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(line.keys()))
            if not has_header:
                w.writeheader()
            w.writerow(line)
        print(f"Appended snapshot to {args.snapshot}")

    if args.html:
        if bucket_map:
            bucket_note = ("Buckets group issue types — "
                           + "; ".join(f"{b}: {', '.join(ts)}"
                                       for b, ts in bucket_map.items())
                           + "; Untyped: no issue type assigned; Other: any "
                             "type not listed.")
        else:
            bucket_note = ("Buckets are the repositories' issue types as-is; "
                           "Untyped means no issue type was assigned. Group "
                           "them with a --buckets config if the list gets "
                           "noisy.")
        data = {
            "org": args.owner,
            "generated": date.today().strftime("%-d %b %Y"),
            "activeDays": args.active_days,
            "reportMonth": report_month,
            "report": {
                "name": rep_name,
                "opened": rep["opened"],
                "closed": rep["closed"],
                "prevOpenedNote": prev_note(prev["opened"]) if prev else "",
                "prevClosedNote": prev_note(prev["closed"]) if prev else "",
            },
            "months": [{"m": label(t["month"]), "opened": t["opened"],
                        "closed": t["closed"], "backlog": t["backlog"]}
                       for t in complete],
            "partial": {"m": f"{label(current['month'])} (to date)",
                        "opened": current["opened"], "closed": current["closed"],
                        "backlog": current["backlog"]},
            "active": len(active),
            "dormant": dormant,
            "archivedOpen": in_archived,
            "activeBuckets": [{"name": k, "n": v}
                              for k, v in active_buckets.most_common()],
            "monthBuckets": [{"name": b, "opened": mo_open.get(b, 0),
                              "closed": mo_close.get(b, 0)}
                             for b in sorted(set(mo_open) | set(mo_close),
                                             key=lambda b: -mo_open.get(b, 0))],
            "footnotes": [
                f"Active backlog = open issues in non-archived repositories "
                f"with any activity (comment, label, edit) in the last "
                f"{args.active_days} days. This is the workload the team is "
                f"actually carrying; the {len(open_rows):,} total also counts "
                f"{dormant:,} dormant issues (no activity for "
                f"{args.active_days}+ days) and {in_archived:,} issues locked "
                f"open in archived repositories.",
                bucket_note,
                f"{untyped_open:,} open tickets have no issue type assigned"
                + (", so bucket-level numbers understate every category. "
                   "Assigning a type at triage fixes this going forward."
                   if untyped_open else "."),
                f"Source: all issues (open and closed) across every "
                f"repository owned by {args.owner}, via the GitHub API. Pull "
                f"requests are not counted. "
                f"{full_name(current['month'])} {current['month'][:4]} is in "
                f"progress and excluded from the charts"
                + (", though the headline tiles report it to date."
                   if rep is current else "."),
            ],
        }
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "dashboard_template.html")
        with open(template_path) as f:
            html = f.read()
        html = html.replace("__DATA_JSON__", json.dumps(data, indent=2))
        with open(args.html, "w") as f:
            f.write(html)
        print(f"Wrote {args.html}")

    if args.digest or args.narrative:
        def month_rows(field):
            return sorted((r for r in rows
                           if r[field] and month_key(r[field]) == rep["month"]),
                          key=lambda r: (r["repo"], r["number"]))

        opened_list, closed_list = month_rows("created"), month_rows("closed")
        lines = [
            f"# Ticket digest — {args.owner} — {report_month}",
            "",
            f"Generated {date.today()}",
            f"Active backlog: {len(active)} open tickets touched in the last "
            f"{args.active_days} days · {len(open_rows):,} open in total "
            f"({dormant:,} dormant, {in_archived:,} in archived repos)",
            f"Opened in {rep_name}: {rep['opened']} · closed: {rep['closed']} "
            f"· net: {rep['opened'] - rep['closed']:+d}",
            "",
            "(Each ticket's still-open / already-closed status reflects today, "
            "not the end of the report month.)",
            "",
            f"## Tickets opened in {rep_name} ({len(opened_list)})",
            "",
        ]
        for r in opened_list:
            status = "already closed" if r["closed"] else "still open"
            lines.append(f"- {r['repo']}#{r['number']} [{r['type']}] "
                         f"{r['title']} ({status})")
        lines += ["", f"## Tickets closed in {rep_name} ({len(closed_list)})", ""]
        for r in closed_list:
            age = (r["closed"] - r["created"]).days
            lines.append(f"- {r['repo']}#{r['number']} [{r['type']}] "
                         f"{r['title']} (was open {age:,} days)")
        digest_text = "\n".join(lines) + "\n"

        if args.digest:
            with open(args.digest, "w") as f:
                f.write(digest_text)
            print(f"Wrote {args.digest}")

    if args.narrative:
        cmd = shlex.split(args.llm_cmd)
        try:
            out = subprocess.run(cmd, input=NARRATIVE_PROMPT + "\n" + digest_text,
                                 capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            sys.exit(f"--narrative needs {cmd[0]!r} on PATH (from --llm-cmd "
                     f"{args.llm_cmd!r})")
        if out.returncode != 0:
            sys.exit(f"--llm-cmd failed:\n{out.stderr}")
        text = clean_terminal_output(out.stdout)
        with open(args.narrative, "w") as f:
            f.write(text.strip() + "\n")
        print(f"Wrote {args.narrative}")


if __name__ == "__main__":
    main()
