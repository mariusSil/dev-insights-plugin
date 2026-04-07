---
name: dev-insights
description: Generate a comprehensive developer analytics report from local Claude Code data — work rhythm, project distribution, workflow shape, tool/code activity, git commits, token spend, and heuristic growth insights. Use when the user asks for coding patterns, dev stats, work-hour analysis, productivity insights, "how am I working", weekly review, or any retrospective on their own Claude Code usage.
---

# dev-insights

A self-contained analytics skill that builds a single comprehensive markdown dashboard from three local data sources:

1. **`~/.claude/history.jsonl`** — every prompt the user has typed, with timestamps and project paths
2. **`~/.claude/projects/<project>/*.jsonl`** — full session transcripts (tool calls, file edits, bash commands, token usage, models, branches)
3. **`git log`** — commits, diff sizes, hot files, commit hours (when invoked from inside a repo)

The output is one well-structured markdown report covering work rhythm, project distribution, workflow shape, tool/code activity, git activity, and heuristic insights. No external dependencies, no network calls, all data stays local.

## When to use this skill

Use whenever the user asks for any of:

- "Analyze my coding patterns / dev habits / work patterns"
- "How am I working / how productive was I / weekly review"
- "Show me my work hours / when am I most active"
- "What did I work on this week / which files am I touching most"
- "Dev insights / dev stats / developer growth report"
- Any retrospective on their own Claude Code activity

## How to invoke the skill

Run the bundled Python script directly. It is self-contained (Python 3 stdlib only, no `pip install` needed).

The script lives at `<skill-dir>/scripts/analyze.py`. When installed as a plugin the skill folder is inside the plugin cache; when installed manually it lives at `~/.claude/skills/dev-insights/scripts/analyze.py`. Resolve the absolute path from the skill folder Claude Code loaded this SKILL.md from.

```bash
python3 <skill-dir>/scripts/analyze.py [OPTIONS]
```

### Default invocation (most common)

ALWAYS save the report to a persistent file in `~/dev-insights-reports/` named with the date and window so the user can keep it. Do not write to `/tmp` and do not skip the file.

```bash
mkdir -p ~/dev-insights-reports && \
python3 <skill-dir>/scripts/analyze.py --days 7 \
  --output ~/dev-insights-reports/dev-insights-$(date +%Y-%m-%d)-7d.md
```

After generating, ALWAYS:
1. Tell the user the **full absolute path** of the saved file in the first sentence of the response
2. Read the file and present a concise inline summary (highlights only — do not dump the whole report)
3. Mention they can `open` the file to see the full report

### Useful variations

| User intent | Command |
|---|---|
| Past 24h quick look | `--days 1` |
| Past week (default) | `--days 7` |
| Past month | `--days 30` |
| Filter to one project | `--project MindExtension` |
| Skip transcripts (much faster) | `--no-transcripts` |
| Skip git (when not in a repo) | `--no-git` |
| Analyze a different repo | `--git-dir /path/to/repo` |
| Print to stdout | omit `--output` |

### Recommended workflow

1. Ask the user (only if ambiguous) about lookback window and whether they want it scoped to one project
2. Run the script with `--output ~/dev-insights-reports/dev-insights-<DATE>-<WINDOW>.md` so the file persists in a stable location
3. Read the file with the Read tool
4. Lead the response with the absolute file path of the saved report
5. Present a concise summary inline highlighting:
   - Top-line numbers (prompts, sessions, commits, tokens)
   - The 2–3 most actionable heuristic insights
   - Anything genuinely surprising in the hot-files or workflow-shape sections
5. Offer to re-run with a different window or project filter

Do **not** dump the entire raw report to the user unless they ask — surface the highlights.

## What the report contains

The script always emits these sections (some are skipped when there's no data):

- **At a Glance** — total prompts, sessions, transcripts inspected, commits, tokens
- **Work Rhythm** — hour-of-day heatmap, day-of-week distribution, late-night/weekend share, total active time, longest session
- **Projects** — prompt count per project with share %
- **Workflow Shape** — `/clear` count, subagent dispatches, terse-prompt ratio, anti-overengineering pushbacks, verification follow-up rate, top slash commands, friction commands
- **Tool & Code Activity** — tool call mix, hottest edited files, most-run bash commands, models used, full token breakdown
- **Git Activity** — commit count, lines added/removed, hot files, commit hour-of-day heatmap
- **Branches Worked On** — branches the user touched in the window
- **Heuristic Insights** — automated flags for context churn, terse prompting, verification gap, friction signals, late-night work, weekend load, project switching, heavy delegation

Heuristics are intentionally conservative — they fire only when thresholds are clearly exceeded. See `references/data-sources.md` for thresholds and logic.

## Important notes

- **Period** is always specified relative to "now" using `--days N` (default 7). The user can ask for any window.
- **Project filtering** uses the *short* project name (the basename of the cwd path), e.g. `MindExtension` not the full path.
- **Performance**: Transcripts can be large. The script streams files line-by-line, skips files with mtime older than the cutoff, and skips any single file >50 MB. A 7-day window typically completes in 1–5 seconds.
- **Privacy**: All data is local. The script makes zero network calls.
- **No third-party APIs**: This skill deliberately does not fetch external articles or send to Slack — it is purely a local analytics tool. If the user wants curated learning resources or Slack delivery, point them at the separate `developer-growth-analysis` skill.

## Reference material

- `references/data-sources.md` — full schema of `history.jsonl` and session transcript JSONL files, plus the heuristic thresholds the script uses. Read this when the user asks how a specific metric is computed, or when extending the script with new dimensions.
