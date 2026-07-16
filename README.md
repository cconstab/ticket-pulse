# ticket-pulse

Monthly ticket-flow reports from GitHub issues, for people who don't live in
GitHub. One script, no dependencies beyond Python and the GitHub CLI.

For any GitHub org (or user account) it produces:

- **Tickets opened / closed / net per month**, and the backlog trend
- **Active backlog** — the open issues actually being worked, separated from
  the years-old pile that makes raw "open issues" counts meaningless
- **A self-contained HTML dashboard** you can email, host, or screenshot into
  a slide (light/dark, interactive charts, no external assets)
- **An LLM-written narrative** of the month's themes, via any local or cloud
  model — Claude, Ollama, whatever reads stdin

## Quickstart

```bash
gh auth login          # once — needs repo access to the account you'll report on
python3 ticket_pulse.py your-org --html dashboard.html
open dashboard.html
```

That's it. The fetch takes a few minutes for large orgs (it pages through
every issue in every repo), then you have the dashboard and a markdown
summary on stdout.

## The monthly routine

```bash
python3 ticket_pulse.py your-org \
  --snapshot snapshots.csv \
  --html dashboard.html \
  --digest digest.md \
  --narrative narrative.md
```

Or put it on cron — `monthly-report.sh` wraps exactly that, validates the
environment first, and names outputs after the month they report on:

```cron
0 7 1 * * /path/to/ticket-pulse/monthly-report.sh your-org >> $HOME/ticket-pulse.log 2>&1
```

## The narrative — bring your own LLM

`--digest` writes every ticket opened and closed in the report month (repo,
type, title, how long closed tickets had been open) as one markdown file.
`--narrative` pipes that through an LLM with a chief-of-staff prompt and
writes a short plain-language report: headline, 3–5 themes, what to watch.

The LLM is any command that reads a prompt on stdin and prints the answer:

```bash
--narrative report.md                                  # default: claude -p
--narrative report.md --llm-cmd "ollama run llama3.1"  # local model
--narrative report.md --llm-cmd "llm -m gpt-4o-mini"   # anything else
```

Small local models produce noticeably shallower analysis than frontier
models on a busy month — the digest is a lot to synthesize. The digest file
always stands alone: paste it into any chat with your own question.

Tip: for ollama "thinking" models (qwen3, deepseek-r1, …) add
`--hidethinking` so the reasoning trace stays out of the report:
`--llm-cmd "ollama run qwen3 --hidethinking"`.

## Options

| Flag | What it does |
|---|---|
| `--html X.html` | Render the dashboard (self-contained, from `dashboard_template.html`) |
| `--snapshot X.csv` | Append today's backlog counts; run monthly to build history |
| `--digest X.md` | LLM-ready capture of the report month's tickets |
| `--narrative X.md` | Digest → LLM → themes report (see `--llm-cmd`) |
| `--month 2026-05` | Report a specific past month instead of the last complete one |
| `--midmonth` | Report the in-progress month to date (a mid-month check-in) |
| `--buckets X.json` | Group issue types into leadership-friendly buckets |
| `--active-days N` | Active-backlog window (default 90) |
| `--json` / `--csv` | Machine-readable stats / raw issue dump |
| `--type A,B` | Restrict everything to specific issue types |

## Multiple organizations

Pass several owners to aggregate them into one report — useful when work
spans a company org, an open-source org, and a founder's personal account:

```bash
python3 ticket_pulse.py org-one org-two some-user --html dashboard.html
```

In multi-owner runs, repos are qualified as `owner/repo` in digests and CSVs
so names can't collide. Your `gh` token needs read access to all of them.

## Buckets

By default every GitHub issue type is its own bucket. If your org has many
types, group them — copy `buckets.example.json` to `buckets.json` and edit:

```json
{
  "Features": ["Feature", "Enhancement", "Proposal"],
  "Bugs": ["Bug", "Regression"],
  "Engineering health": ["Chore", "Refactor", "Tech Debt", "Performance"],
  "Docs": ["Docs"]
}
```

Unlisted types land in **Other**; issues with no type land in **Untyped**.
(A large Untyped bucket is itself a finding — fix it by assigning a type at
triage.)

## Why not just read the numbers off the board?

Hard-won lessons this tool encodes:

- **Project boards undercount closures.** Board workflows archive closed
  items, so the board API can show zero closures ever. ticket-pulse reads
  the issues themselves.
- **The GitHub search API caps at 1,000 results**, silently. ticket-pulse
  enumerates repo-by-repo via GraphQL instead.
- **Total open issues ≠ workload.** Old repos get archived with issues
  locked open forever, and fast-moving teams abandon tickets rather than
  close them. The *active backlog* — open, non-archived, touched in the
  last 90 days — is the number that reflects reality, and the dashboard
  shows the active / dormant / archived split honestly rather than hiding
  it.

## Requirements

- Python 3.9+ (standard library only)
- [GitHub CLI](https://cli.github.com/) authenticated with access to the
  account you're reporting on (private repos need a token that can read them)
- Optionally, an LLM CLI for `--narrative`

## License

MIT
