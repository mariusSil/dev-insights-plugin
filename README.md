# dev-insights

A Claude Code plugin that builds a comprehensive **local** analytics report on how you actually use Claude Code — work hours, daily focus time, tool/code activity, git commits, token spend, and heuristic growth insights.

Everything runs locally. No network calls, no telemetry, no third-party services.

## What you get

A single markdown report covering:

- **At a Glance** — prompts, sessions, commits, tokens
- **Work Rhythm** — 24h heatmap, day-of-week distribution, late-night/weekend share, longest session
- **Daily Breakdown** — per-day prompts, first/last activity, active hours, **focus hours**, sessions, **token spend**, commits, **commits-per-focus-hour**, top project
- **Projects** — distribution with shares
- **Workflow Shape** — `/clear` count, subagent dispatches, terse-prompt ratio, anti-overengineering pushbacks, verification follow-up rate, top slash commands
- **Tool & Code Activity** — tool call mix, hottest edited files, most-run bash commands, models used, token breakdown
- **Git Activity** — commits, lines, hot files, commit hour heatmap
- **Branches Worked On**
- **Heuristic Insights** — automated flags for context churn, terse prompting, verification gap, friction signals, late-night work, weekend load, project switching, heavy delegation

## Install

### As a Claude Code plugin

```
/plugin install https://github.com/MariusSilenskis/dev-insights-plugin
```

After install, the `dev-insights` skill becomes available across all your Claude Code projects.

### Manual install (no plugin system)

Copy the `skills/dev-insights/` folder into `~/.claude/skills/`:

```bash
git clone https://github.com/MariusSilenskis/dev-insights-plugin
cp -r dev-insights-plugin/skills/dev-insights ~/.claude/skills/
```

## Usage

In any Claude Code session, just say:

```
run dev-insights
analyze my coding patterns this week
show me my dev stats
how am I working
```

Or run the script directly without Claude Code:

```bash
python3 ~/.claude/skills/dev-insights/scripts/analyze.py --days 7 --output report.md
```

### Options

| Flag | Purpose |
|---|---|
| `--days N` | Lookback window (default: 7) |
| `--project NAME` | Filter to a single project (short name, e.g. `MindExtension`) |
| `--output FILE` | Write to a file instead of stdout |
| `--no-transcripts` | Skip session transcript parsing (much faster, less detail) |
| `--no-git` | Skip git log (use when not in a repo) |
| `--git-dir PATH` | Analyze a different repo |

### Common invocations

```bash
# Last 24 hours
python3 ~/.claude/skills/dev-insights/scripts/analyze.py --days 1

# Last month, scoped to one project
python3 ~/.claude/skills/dev-insights/scripts/analyze.py --days 30 --project MindExtension

# Quick history-only run
python3 ~/.claude/skills/dev-insights/scripts/analyze.py --days 7 --no-transcripts --no-git
```

## Requirements

- **Python 3.8+** (uses only the standard library — no `pip install` needed)
- **Claude Code** (for the data sources under `~/.claude/`)
- **git** (optional, only used when running inside a repo)

Works on macOS, Linux, and Windows.

## How it works

The script reads three local data sources:

1. **`~/.claude/history.jsonl`** — every prompt you've typed, with timestamp + project
2. **`~/.claude/projects/<encoded-cwd>/*.jsonl`** — full session transcripts (tool calls, file edits, bash commands, token usage, models, branches)
3. **`git log`** — commits, diff sizes, hot files (when in a git repo)

It streams these files line-by-line so it stays cheap even on multi-GB transcript histories. A 7-day window typically completes in 1–5 seconds.

For schema details and how every metric is computed, see [`skills/dev-insights/references/data-sources.md`](skills/dev-insights/references/data-sources.md).

## Privacy

- **All data stays local.** The script makes zero network calls.
- **Each user sees only their own data** — `~/.claude/` is per-user.
- **The generated report contains your prompts, file paths, and bash commands.** Treat it accordingly. Don't paste it into a public chat without redacting.
- **No telemetry, no opt-in tracking, no callbacks.**

## Heuristic insights

The report flags 9 patterns when thresholds are clearly exceeded. All thresholds live as constants at the top of `scripts/analyze.py` — easy to tune.

| Insight | Fires when |
|---|---|
| Context churn | More than 1 `/clear` per session on average |
| Terse prompting | More than 20% of prompts are `<5` words |
| Verification gap | Less than 30% of "fix it" approvals followed by a test/build |
| Reactive simplification | 5+ "remove overengineering" prompts |
| Friction signals | Any `/login`, `/usage`, or `/rate-limit-options` |
| Late-night work | More than 20% of prompts in late-night window |
| Weekend load | More than 25% of prompts on Sat/Sun |
| Project switching | 4+ distinct projects active in the window |
| Heavy delegation | Subagent calls more than 15% of all tool calls |

## Repo layout

```
dev-insights-plugin/
├── .claude-plugin/
│   └── plugin.json              # plugin manifest
├── skills/
│   └── dev-insights/
│       ├── SKILL.md             # skill metadata + invocation guide
│       ├── scripts/
│       │   └── analyze.py       # the analyzer (Python 3 stdlib only)
│       └── references/
│           └── data-sources.md  # schemas + heuristic thresholds
└── README.md
```

## Extending

Common extensions:

- **New per-prompt metric** → add it to `analyze_history()` and render in the Workflow Shape section
- **New transcript-level metric** → add a counter to `analyze_transcripts()` and walk the relevant `tool_use` block
- **New git metric** → extend `analyze_git()` and render in the Git Activity section
- **New heuristic** → add a check to `derive_insights()` with a clear threshold

When adding metrics, prefer **counters and ratios** over averages — they degrade more gracefully on small windows.

## License

MIT
