# dev-insights — Data sources & metric definitions

This document captures the schemas, file layouts, and heuristic thresholds used by `scripts/analyze.py`. Read this when extending the script or explaining how a metric is computed.

## 1. `~/.claude/history.jsonl`

A JSONL file with one entry per user prompt across **all** Claude Code sessions on this machine. Each entry roughly looks like:

```json
{
  "display": "the user's prompt text (or slash command)",
  "project": "/Users/me/Development/MyRepo",
  "timestamp": 1712486400000,
  "pastedContents": { ... optional ... }
}
```

Notes:
- `timestamp` is **milliseconds since epoch** (note: transcripts use ISO strings instead — `to_dt()` handles both).
- `project` is the full cwd path. The script uses `Path(...).name` to derive a short project name for display and filtering.
- `display` is the raw text the user typed, including slash commands like `/clear`, `/login`, `/usage`, `/review staged`, etc.
- This file is the source of truth for **prompt-level** metrics: counts, rhythm, sessions, terse-prompt detection, slash command usage, friction signals.

## 2. `~/.claude/projects/<encoded-cwd>/*.jsonl`

For every project (cwd) the user has worked in, Claude Code stores per-session transcript files. The directory is named by replacing `/` with `-` in the cwd (so `/Users/me/Development/MindExtension` becomes `-Users-me-Development-MindExtension`).

Inside each project directory:
- `<session-uuid>.jsonl` — main session transcripts
- `<session-uuid>/subagents/*.jsonl` — subagent transcripts (one file per spawned subagent)
- `<session-uuid>/` — may also contain other internal files

Each line in a transcript is one of these types:

| `type` | Notes |
|---|---|
| `user` | User input or tool result. `message.content` is a string for raw user input, or a structured object for tool results |
| `assistant` | Assistant response. `message` contains `model`, `usage`, and `content` (a list of blocks: `text`, `thinking`, `tool_use`) |
| `system` | System messages, including hooks, slash command expansions, etc. |
| `progress` | In-progress streaming markers (mostly ignored) |
| `file-history-snapshot` | Internal file-state snapshots (ignored) |

### Key fields the analyzer uses

Common to most entries:
- `timestamp` — ISO 8601 string (e.g. `2026-04-07T04:51:20.702Z`)
- `cwd` — working directory at message time
- `sessionId` — UUID of the session
- `gitBranch` — current git branch (when in a repo)
- `version` — Claude Code version

Assistant-specific (`type: assistant`):
- `message.model` — e.g. `claude-opus-4-6`
- `message.usage` — `{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, ...}`
- `message.content` — list of blocks. The analyzer iterates over these and extracts `tool_use` blocks:
  - `block.type == "tool_use"`, `block.name` is the tool name (`Read`, `Edit`, `Bash`, `Agent`, etc.)
  - `block.input` is the tool's parameters. The analyzer extracts:
    - `Read` → `file_path`
    - `Edit` / `Write` / `NotebookEdit` → `file_path` (or `notebook_path`)
    - `Bash` → `command` (first token used as a category)
    - `Agent` / `Task` → counted as a subagent dispatch

### Performance guards

- **mtime pre-filter**: files whose mtime is older than the lookback cutoff are skipped entirely without opening.
- **Size cap**: any single transcript larger than 50 MB is skipped (extreme outliers — typically corrupted or runaway sessions).
- **Streaming**: each file is read line-by-line, never loaded into memory in full.

A 7-day window over a busy MindExtension-style workload typically completes in 1–5 seconds.

## 3. Git log

When invoked from inside a git repository (or with `--git-dir`), the analyzer runs:

```
git -C <repo> log --since=<cutoff> --all --no-merges --numstat \
    --format=__COMMIT__|%H|%ai|%s
```

It parses the alternating commit-headers and `numstat` lines to extract:
- Commit count
- Lines added / removed (sum of all `numstat` rows)
- Files touched (counter of file paths)
- Commit hour-of-day distribution
- First and last commit timestamps in the window
- Commit subjects (saved but not currently rendered)

If the repo is missing or `git` fails, the git section is silently skipped.

## Heuristic thresholds

All thresholds live as constants near the top of `scripts/analyze.py`. Tune them there.

| Constant | Default | Meaning |
|---|---|---|
| `SESSION_GAP_MIN` | 30 min | Two prompts more than 30 min apart belong to different sessions |
| `LATE_NIGHT_BEFORE` | 6 | Local hour before which a prompt counts as late-night |
| `LATE_NIGHT_AFTER` | 22 | Local hour at/after which a prompt counts as late-night |
| `TERSE_THRESHOLD_WORDS` | 5 | Non-slash prompts shorter than this are flagged as "terse" |

### Insight rules

The `derive_insights()` function fires the following flags only when the listed thresholds are exceeded:

| Insight | Fires when |
|---|---|
| **Context churn** | `clears_per_session > 1.0` |
| **Terse prompting** | terse share `> 20%` |
| **Verification gap** | `< 30%` of terse approval prompts followed by a test/build/lint prompt within the same session |
| **Reactive simplification** | `>= 5` prompts asked to remove overengineering (matched by `OVERENG_PATTERNS`) |
| **Friction signals** | any `/login`, `/usage`, or `/rate-limit-options` calls present |
| **Late-night work** | `> 20%` of prompts in late-night window |
| **Weekend load** | `> 25%` of prompts on Sat/Sun |
| **Project switching** | `>= 4` distinct projects active in the window |
| **Heavy delegation** | subagent (`Agent` tool) calls `> 15%` of all tool calls |

### Pattern definitions

- `OVERENG_PATTERNS` — regex matching `overeng`, `overengineer`, `too complex`, `simpler`, `simplify`, `unnecessary`, `no defensive`, etc.
- `VERIFY_PATTERNS` — regex matching `test`, `build`, `lint`, `tsc`, `jest`, `vitest`, `pnpm`, `npm run`, `verify`, `check`
- `FRICTION_COMMANDS` — set of slash commands the user runs when hitting auth/rate/cost issues: `/login`, `/logout`, `/usage`, `/rate-limit-options`, `/cost`, `/model`
- A "terse approval prompt" is a prompt that contains `fix`, `do it`, `implement`, `apply`, `go`, `continue`, `yes`, or `approve` AND is `< 6` words long. The verification check looks at the next 3 prompts within the same session for any `VERIFY_PATTERNS` match.

## Extending the analyzer

Common extensions:

- **New per-prompt metric** → add it to `analyze_history()` and render in the Workflow Shape section.
- **New transcript-level metric** → add a counter to `analyze_transcripts()` and walk the relevant `tool_use` block in the inner loop.
- **New git metric** → extend `analyze_git()` and render in the Git Activity section.
- **New heuristic** → add a check to `derive_insights()` with a clear threshold.

When adding metrics, prefer **counters and ratios** over averages — they degrade more gracefully on small windows.

## Privacy

The script makes **zero network calls**. All data is read from local files (`~/.claude/...` and the git repo). The output report contains user prompts and file paths — treat it accordingly if sharing.
