#!/usr/bin/env python3
"""
dev-insights — Comprehensive local analytics on Claude Code usage.

Reads three local data sources to build a developer dashboard:
  1. ~/.claude/history.jsonl                          (every prompt + timestamp)
  2. ~/.claude/projects/<proj>/*.jsonl                (full session transcripts: tool calls, tokens, files)
  3. git log (if cwd is a git repo, or via --git-dir)  (commits, diff sizes, hot files)

Outputs a single markdown report to stdout (or --output).

Usage:
  analyze.py [--days 7] [--project NAME] [--output report.md] [--no-transcripts] [--no-git]

All flags optional. By default: last 7 days, all projects, transcripts on, git on if repo detected.

Designed to be self-contained — Python 3 stdlib only, no pip deps. Streams files line-by-line
so it stays cheap even on multi-GB transcript histories.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------- Configuration ----------

HOME = Path.home()
HISTORY_FILE = HOME / ".claude" / "history.jsonl"
PROJECTS_DIR = HOME / ".claude" / "projects"

# A "session" in history.jsonl is detected by a gap > SESSION_GAP_MIN minutes
SESSION_GAP_MIN = 30

# Late-night = before this hour or after that hour (local time)
LATE_NIGHT_BEFORE = 6
LATE_NIGHT_AFTER = 22

# Terse prompt = fewer than this many words
TERSE_THRESHOLD_WORDS = 5

# Patterns indicating user pushed back on overengineering / requested simplification
OVERENG_PATTERNS = re.compile(
    r"\b(overeng|over-eng|overengineer|too complex|simpler|simplify|too much|remove.*fallback|"
    r"no defensive|no need|unnecessary)\b",
    re.IGNORECASE,
)

# Patterns indicating verification follow-up after a fix
VERIFY_PATTERNS = re.compile(r"\b(test|build|lint|tsc|jest|vitest|pnpm|npm run|verify|check)\b", re.IGNORECASE)

# Friction commands the user runs when hitting limits
FRICTION_COMMANDS = {"/login", "/logout", "/usage", "/rate-limit-options", "/cost", "/model"}

# Slash commands worth tracking by frequency
TRACKED_SLASH = re.compile(r"^/[a-zA-Z][\w-]*")


# ---------- Helpers ----------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_dt(ts):
    """Accepts ms-epoch (history.jsonl) or ISO string (transcripts)."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def short_project(path: str) -> str:
    if not path:
        return "unknown"
    return Path(path).name


def project_dir_to_cwd(name: str) -> str:
    """Reverse the encoding ~/.claude/projects uses: dashes → slashes (best effort)."""
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name


def fmt_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    h = minutes / 60
    if h < 24:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def pct(n, d):
    return f"{(100.0 * n / d):.0f}%" if d else "—"


# ---------- Stage 1: history.jsonl analysis (prompt rhythm) ----------


def analyze_history(cutoff: datetime, project_filter=None):
    """
    Returns a dict of prompt-level metrics:
      - total_prompts, prompts_per_project
      - sessions: list of (start, end, prompt_count, project)
      - hour_histogram: {0..23: count} (local time)
      - dow_histogram: {0..6: count}
      - late_night_count, weekend_count
      - clear_count, terse_count, friction_counter
      - slash_command_counter
      - overeng_pushback_count
      - prompts (chronological list of dicts) — used by later stages
    """
    out = {
        "total_prompts": 0,
        "prompts_per_project": Counter(),
        "sessions": [],
        "hour_histogram": Counter(),
        "dow_histogram": Counter(),
        "late_night_count": 0,
        "weekend_count": 0,
        "clear_count": 0,
        "terse_count": 0,
        "friction_counter": Counter(),
        "slash_command_counter": Counter(),
        "overeng_pushback_count": 0,
        "prompts": [],
        # Per-day aggregates (key = local YYYY-MM-DD)
        "prompts_by_day": Counter(),
        "projects_by_day": defaultdict(Counter),
        "first_prompt_by_day": {},
        "last_prompt_by_day": {},
    }

    if not HISTORY_FILE.exists():
        return out

    entries = []
    with HISTORY_FILE.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            dt = to_dt(e.get("timestamp"))
            if dt is None or dt < cutoff:
                continue
            proj = short_project(e.get("project", ""))
            if project_filter and proj != project_filter:
                continue
            entries.append((dt, proj, (e.get("display") or "").strip()))

    entries.sort(key=lambda x: x[0])
    out["total_prompts"] = len(entries)

    # Session detection (gap-based)
    cur_session = None
    for dt, proj, display in entries:
        local = dt.astimezone()
        day_key = local.strftime("%Y-%m-%d")
        out["prompts_per_project"][proj] += 1
        out["hour_histogram"][local.hour] += 1
        out["dow_histogram"][local.weekday()] += 1
        out["prompts_by_day"][day_key] += 1
        out["projects_by_day"][day_key][proj] += 1
        if day_key not in out["first_prompt_by_day"] or local < out["first_prompt_by_day"][day_key]:
            out["first_prompt_by_day"][day_key] = local
        if day_key not in out["last_prompt_by_day"] or local > out["last_prompt_by_day"][day_key]:
            out["last_prompt_by_day"][day_key] = local
        if local.hour < LATE_NIGHT_BEFORE or local.hour >= LATE_NIGHT_AFTER:
            out["late_night_count"] += 1
        if local.weekday() >= 5:
            out["weekend_count"] += 1
        if display == "/clear":
            out["clear_count"] += 1
        if len(display.split()) < TERSE_THRESHOLD_WORDS and not display.startswith("/"):
            out["terse_count"] += 1
        if display in FRICTION_COMMANDS:
            out["friction_counter"][display] += 1
        m = TRACKED_SLASH.match(display)
        if m:
            out["slash_command_counter"][m.group(0)] += 1
        if OVERENG_PATTERNS.search(display):
            out["overeng_pushback_count"] += 1

        out["prompts"].append({"dt": dt, "project": proj, "display": display})

        if cur_session and (dt - cur_session["last"]).total_seconds() / 60 <= SESSION_GAP_MIN:
            cur_session["last"] = dt
            cur_session["count"] += 1
        else:
            if cur_session:
                out["sessions"].append(cur_session)
            cur_session = {
                "start": dt,
                "last": dt,
                "count": 1,
                "project": proj,
            }
    if cur_session:
        out["sessions"].append(cur_session)

    # Verification follow-ups: did a "fix it" / terse approval get followed by a test/build prompt?
    verify_followup = 0
    fix_keywords = re.compile(r"\b(fix|do it|implement|apply|go|continue|yes|approve)\b", re.IGNORECASE)
    for i, p in enumerate(out["prompts"]):
        if fix_keywords.search(p["display"]) and len(p["display"].split()) < 6:
            # look at the next 3 prompts in same session
            for j in range(i + 1, min(i + 4, len(out["prompts"]))):
                gap = (out["prompts"][j]["dt"] - p["dt"]).total_seconds() / 60
                if gap > SESSION_GAP_MIN:
                    break
                if VERIFY_PATTERNS.search(out["prompts"][j]["display"]):
                    verify_followup += 1
                    break
    out["verify_followup_count"] = verify_followup
    out["fix_terse_count"] = sum(
        1 for p in out["prompts"] if fix_keywords.search(p["display"]) and len(p["display"].split()) < 6
    )

    return out


# ---------- Stage 2: session transcripts (tool-level depth) ----------


def iter_transcript_files(project_filter=None):
    """Yield (project_dir_name, jsonl_path) for all top-level session files in projects dir."""
    if not PROJECTS_DIR.exists():
        return
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        if project_filter and short_project(project_dir_to_cwd(proj_dir.name)) != project_filter:
            continue
        for f in proj_dir.glob("*.jsonl"):
            yield proj_dir.name, f
        # Subagent transcripts
        sub = proj_dir / "subagents"
        if sub.exists():
            for f in sub.glob("*.jsonl"):
                yield proj_dir.name, f


def analyze_transcripts(cutoff: datetime, project_filter=None):
    """
    Stream session transcripts and compute tool-level metrics:
      - tool_counter: tool name → count
      - file_edits: edited file path → count
      - file_reads: read file path → count
      - bash_command_counter: first token of each bash invocation → count
      - subagent_dispatches
      - tokens: input/output/cache by day
      - branches_touched
      - sessions_inspected
    """
    out = {
        "tool_counter": Counter(),
        "file_edits": Counter(),
        "file_reads": Counter(),
        "bash_first_token": Counter(),
        "subagent_dispatches": 0,
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_cache_read": 0,
        "tokens_cache_write": 0,
        "tokens_by_day": Counter(),
        "branches_touched": Counter(),
        "sessions_inspected": 0,
        "models_used": Counter(),
    }

    for proj_name, path in iter_transcript_files(project_filter=project_filter):
        # Cheap pre-filter: skip files whose mtime is older than cutoff
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue

        # Skip files larger than 50 MB to avoid pathological reads
        if path.stat().st_size > 50 * 1024 * 1024:
            continue

        in_window = False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = to_dt(e.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    in_window = True

                    branch = e.get("gitBranch")
                    if branch:
                        out["branches_touched"][branch] += 1

                    if e.get("type") != "assistant":
                        continue
                    msg = e.get("message", {}) or {}
                    model = msg.get("model")
                    if model:
                        out["models_used"][model] += 1

                    usage = msg.get("usage") or {}
                    in_t = usage.get("input_tokens", 0) or 0
                    out_t = usage.get("output_tokens", 0) or 0
                    cr_t = usage.get("cache_read_input_tokens", 0) or 0
                    cw_t = usage.get("cache_creation_input_tokens", 0) or 0
                    out["tokens_input"] += in_t
                    out["tokens_output"] += out_t
                    out["tokens_cache_read"] += cr_t
                    out["tokens_cache_write"] += cw_t
                    day_key = ts.astimezone().strftime("%Y-%m-%d")
                    out["tokens_by_day"][day_key] += in_t + out_t + cr_t + cw_t

                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name", "?")
                        out["tool_counter"][name] += 1
                        inp = block.get("input") or {}

                        if name == "Edit" or name == "Write" or name == "NotebookEdit":
                            fp = inp.get("file_path") or inp.get("notebook_path") or ""
                            if fp:
                                out["file_edits"][fp] += 1
                        elif name == "Read":
                            fp = inp.get("file_path") or ""
                            if fp:
                                out["file_reads"][fp] += 1
                        elif name == "Bash":
                            cmd = (inp.get("command") or "").strip()
                            if cmd:
                                first = cmd.split()[0]
                                # strip common prefixes
                                if first in ("sudo", "time"):
                                    parts = cmd.split()
                                    first = parts[1] if len(parts) > 1 else first
                                out["bash_first_token"][first] += 1
                        elif name == "Agent" or name == "Task":
                            out["subagent_dispatches"] += 1
        except OSError:
            continue

        if in_window:
            out["sessions_inspected"] += 1

    return out


# ---------- Stage 3: git log ----------


def analyze_git(cutoff: datetime, repo: Path):
    """
    Run git log to get commit-level activity. Returns {} if not a repo.
    """
    out = {
        "commits": 0,
        "files_touched": Counter(),
        "lines_added": 0,
        "lines_removed": 0,
        "commits_by_hour": Counter(),
        "commits_by_day": Counter(),
        "first_commit": None,
        "last_commit": None,
        "branches": Counter(),
        "messages": [],
    }
    if not (repo / ".git").exists():
        return out
    since = cutoff.strftime("%Y-%m-%d")
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                f"--since={since}",
                "--all",
                "--no-merges",
                "--numstat",
                "--format=__COMMIT__|%H|%ai|%s",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return out
    if log.returncode != 0:
        return out

    cur_hash = None
    for raw in log.stdout.splitlines():
        if raw.startswith("__COMMIT__|"):
            _, h, iso, subj = raw.split("|", 3)
            out["commits"] += 1
            cur_hash = h
            out["messages"].append(subj)
            try:
                dt = datetime.fromisoformat(iso.replace(" ", "T", 1))
            except ValueError:
                dt = None
            if dt:
                out["commits_by_hour"][dt.hour] += 1
                day = dt.astimezone().strftime("%Y-%m-%d")
                out["commits_by_day"][day] += 1
                if not out["first_commit"] or dt < out["first_commit"]:
                    out["first_commit"] = dt
                if not out["last_commit"] or dt > out["last_commit"]:
                    out["last_commit"] = dt
        elif raw.strip() and cur_hash:
            parts = raw.split("\t")
            if len(parts) == 3:
                a, r, fp = parts
                try:
                    out["lines_added"] += int(a)
                except ValueError:
                    pass
                try:
                    out["lines_removed"] += int(r)
                except ValueError:
                    pass
                out["files_touched"][fp] += 1
    return out


# ---------- Report rendering ----------


def heatmap_line(hist: Counter) -> str:
    """24-cell hour heatmap, aligned to a 'NN NN NN ...' header row (3 chars per cell)."""
    if not hist:
        return "—"
    blocks = " ▁▂▃▄▅▆▇█"
    mx = max(hist.values()) or 1
    cells = []
    for h in range(24):
        v = hist.get(h, 0)
        idx = int(round((v / mx) * (len(blocks) - 1)))
        cells.append(blocks[idx])
    # Each header cell is 2 digits + 1 space; render each block + 2 spaces to match.
    return "  ".join(cells)


def render(history, transcripts, git, args, period_label):
    lines = []
    out = lines.append

    out(f"# Developer Insights Report")
    out("")
    out(f"**Period**: {period_label}")
    out(f"**Generated**: {now_utc().astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    if args.project:
        out(f"**Filter**: project = `{args.project}`")
    out("")

    # ---- Top-line numbers ----
    out("## At a Glance")
    out("")
    out(f"- **{history['total_prompts']}** prompts across **{len(history['prompts_per_project'])}** projects")
    out(f"- **{len(history['sessions'])}** focused sessions (gap > {SESSION_GAP_MIN}min)")
    out(f"- **{transcripts['sessions_inspected']}** session transcripts inspected")
    if git.get("commits"):
        out(
            f"- **{git['commits']}** commits, "
            f"**+{git['lines_added']:,}** / **−{git['lines_removed']:,}** lines"
        )
    if transcripts["tokens_input"] or transcripts["tokens_output"]:
        total = (
            transcripts["tokens_input"]
            + transcripts["tokens_output"]
            + transcripts["tokens_cache_read"]
            + transcripts["tokens_cache_write"]
        )
        out(f"- **{total/1_000_000:.1f}M** tokens processed (in+out+cache)")
    out("")

    # ---- Work rhythm ----
    out("## Work Rhythm")
    out("")
    out("**Hour-of-day heatmap (local time, 0–23):**")
    out("")
    out("```")
    out("00 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23")
    out(" ".join(f"{history['hour_histogram'].get(h,0):>2}" for h in range(24)))
    out("│" + heatmap_line(history["hour_histogram"]) + "│")
    out("```")
    out("")
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out("**Day-of-week distribution:**")
    out("")
    out("| " + " | ".join(dow_names) + " |")
    out("|" + "----|" * 7)
    out("| " + " | ".join(str(history["dow_histogram"].get(i, 0)) for i in range(7)) + " |")
    out("")
    out(
        f"- **Late-night** prompts (before {LATE_NIGHT_BEFORE}:00 / after {LATE_NIGHT_AFTER}:00): "
        f"{history['late_night_count']} ({pct(history['late_night_count'], history['total_prompts'])})"
    )
    out(
        f"- **Weekend** prompts: {history['weekend_count']} "
        f"({pct(history['weekend_count'], history['total_prompts'])})"
    )

    if history["sessions"]:
        durations = [
            max(1, (s["last"] - s["start"]).total_seconds() / 60) for s in history["sessions"]
        ]
        total_minutes = sum(durations)
        avg = total_minutes / len(durations)
        longest = max(history["sessions"], key=lambda s: (s["last"] - s["start"]).total_seconds())
        longest_min = (longest["last"] - longest["start"]).total_seconds() / 60
        out(f"- **Total active time** (sum of session spans): {fmt_duration(total_minutes)}")
        out(f"- **Avg session length**: {fmt_duration(avg)}")
        out(
            f"- **Longest session**: {fmt_duration(longest_min)} on {longest['start'].astimezone().strftime('%Y-%m-%d')} "
            f"({longest['count']} prompts, {longest['project']})"
        )
    out("")

    # ---- Daily breakdown ----
    if history["prompts_by_day"]:
        out("## Daily Breakdown")
        out("")
        out("Per-day rollup. **Active hours** = span between first and last prompt of the day. ")
        out("**Focus hours** = sum of session spans starting that day (gap-based, ignores idle time).")
        out("")

        # Group sessions by their local start date
        sessions_by_day = defaultdict(list)
        for s in history["sessions"]:
            day_key = s["start"].astimezone().strftime("%Y-%m-%d")
            sessions_by_day[day_key].append(s)

        out("| Date | Day | Prompts | First | Last | Active | Focus | Sessions | Tokens | Commits | Cmt/h | Top project |")
        out("|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---|")
        all_days = sorted(history["prompts_by_day"].keys())
        for d in all_days:
            n = history["prompts_by_day"][d]
            first = history["first_prompt_by_day"].get(d)
            last = history["last_prompt_by_day"].get(d)
            first_s = first.strftime("%H:%M") if first else "—"
            last_s = last.strftime("%H:%M") if last else "—"
            active_min = (last - first).total_seconds() / 60 if first and last else 0
            day_sessions = sessions_by_day.get(d, [])
            focus_min = sum(
                max(1, (s["last"] - s["start"]).total_seconds() / 60) for s in day_sessions
            )
            top_proj = history["projects_by_day"][d].most_common(1)
            top_proj_str = f"{top_proj[0][0]} ({top_proj[0][1]})" if top_proj else "—"
            tokens = transcripts.get("tokens_by_day", {}).get(d, 0)
            tokens_str = f"{tokens/1_000_000:.1f}M" if tokens >= 1_000_000 else (f"{tokens/1000:.0f}K" if tokens else "—")
            commits = git.get("commits_by_day", {}).get(d, 0) if git else 0
            commits_per_hour = (commits / (focus_min / 60)) if focus_min >= 30 else None
            cph_str = f"{commits_per_hour:.1f}" if commits_per_hour is not None else "—"
            try:
                dow = datetime.strptime(d, "%Y-%m-%d").strftime("%a")
            except ValueError:
                dow = ""
            out(
                f"| {d} | {dow} | {n} | {first_s} | {last_s} | "
                f"{fmt_duration(active_min)} | {fmt_duration(focus_min)} | "
                f"{len(day_sessions)} | {tokens_str} | {commits or '—'} | {cph_str} | {top_proj_str} |"
            )
        out("")

        # Totals row
        total_active = sum(
            (history["last_prompt_by_day"][d] - history["first_prompt_by_day"][d]).total_seconds() / 60
            for d in all_days
        )
        total_focus = sum(
            max(1, (s["last"] - s["start"]).total_seconds() / 60) for s in history["sessions"]
        )
        out(
            f"**Totals**: {sum(history['prompts_by_day'].values())} prompts · "
            f"active span sum **{fmt_duration(total_active)}** · "
            f"focus time sum **{fmt_duration(total_focus)}** · "
            f"avg **{fmt_duration(total_focus/max(1,len(all_days)))}/day** focus"
        )
        out("")

    # ---- Project distribution ----
    out("## Projects")
    out("")
    out("| Project | Prompts | Share |")
    out("|---|---:|---:|")
    for proj, n in history["prompts_per_project"].most_common():
        out(f"| {proj} | {n} | {pct(n, history['total_prompts'])} |")
    out("")

    # ---- Workflow shape ----
    out("## Workflow Shape")
    out("")
    out(f"- **`/clear` count**: {history['clear_count']} (context resets)")
    out(f"- **Subagent dispatches**: {transcripts['subagent_dispatches']}")
    out(
        f"- **Terse prompts** (<{TERSE_THRESHOLD_WORDS} words, non-slash): "
        f"{history['terse_count']} ({pct(history['terse_count'], history['total_prompts'])})"
    )
    out(
        f"- **Anti-overengineering pushbacks**: {history['overeng_pushback_count']} "
        "(prompts asking to remove complexity)"
    )
    out(
        f"- **Verification follow-ups after terse approvals**: "
        f"{history['verify_followup_count']} of {history['fix_terse_count']} "
        f"({pct(history['verify_followup_count'], history['fix_terse_count'])})"
    )
    out("")
    if history["slash_command_counter"]:
        out("**Top slash commands / skills used:**")
        out("")
        out("| Command | Count |")
        out("|---|---:|")
        for name, n in history["slash_command_counter"].most_common(15):
            out(f"| `{name}` | {n} |")
        out("")
    if history["friction_counter"]:
        out("**Friction commands** (auth/limits/cost):")
        out("")
        for name, n in history["friction_counter"].most_common():
            out(f"- `{name}` × {n}")
        out("")

    # ---- Tool & code activity ----
    out("## Tool & Code Activity")
    out("")
    if transcripts["tool_counter"]:
        total_tools = sum(transcripts["tool_counter"].values())
        out(f"**Tool call mix** (from {transcripts['sessions_inspected']} sessions, {total_tools} total calls):")
        out("")
        out("| Tool | Calls | Share |")
        out("|---|---:|---:|")
        for name, n in transcripts["tool_counter"].most_common(15):
            out(f"| {name} | {n} | {pct(n, total_tools)} |")
        out("")
    if transcripts["file_edits"]:
        out("**Hottest edited files:**")
        out("")
        for fp, n in transcripts["file_edits"].most_common(10):
            short = fp.replace(str(HOME), "~")
            out(f"- `{short}` × {n}")
        out("")
    if transcripts["bash_first_token"]:
        out("**Most-run bash commands** (by first token):")
        out("")
        for cmd, n in transcripts["bash_first_token"].most_common(10):
            out(f"- `{cmd}` × {n}")
        out("")
    if transcripts["models_used"]:
        out("**Models used:**")
        out("")
        for m, n in transcripts["models_used"].most_common():
            out(f"- {m}: {n} responses")
        out("")
    if transcripts["tokens_input"] or transcripts["tokens_output"]:
        out("**Token spend:**")
        out("")
        out(f"- Input: {transcripts['tokens_input']:,}")
        out(f"- Output: {transcripts['tokens_output']:,}")
        out(f"- Cache read: {transcripts['tokens_cache_read']:,}")
        out(f"- Cache write: {transcripts['tokens_cache_write']:,}")
        out("")

    # ---- Git activity ----
    if git.get("commits"):
        out("## Git Activity")
        out("")
        out(f"- **Commits**: {git['commits']}")
        out(f"- **Lines**: +{git['lines_added']:,} / −{git['lines_removed']:,}")
        if git["first_commit"] and git["last_commit"]:
            out(
                f"- **First → last commit in window**: "
                f"{git['first_commit'].strftime('%Y-%m-%d %H:%M')} → "
                f"{git['last_commit'].strftime('%Y-%m-%d %H:%M')}"
            )
        if git["files_touched"]:
            out("")
            out("**Hottest committed files:**")
            out("")
            for fp, n in git["files_touched"].most_common(10):
                out(f"- `{fp}` × {n}")
        if git["commits_by_hour"]:
            out("")
            out("**Commit hour-of-day:**")
            out("")
            out("```")
            out(" ".join(f"{git['commits_by_hour'].get(h,0):>2}" for h in range(24)))
            out("│" + heatmap_line(git["commits_by_hour"]) + "│")
            out("```")
        out("")

    # ---- Branches ----
    if transcripts["branches_touched"]:
        out("## Branches Worked On")
        out("")
        for branch, n in transcripts["branches_touched"].most_common(10):
            out(f"- `{branch}` ({n} events)")
        out("")

    # ---- Heuristic insights ----
    out("## Heuristic Insights")
    out("")
    insights = derive_insights(history, transcripts, git)
    if not insights:
        out("_No notable patterns flagged this period — keep at it._")
    for tag, msg in insights:
        out(f"- **{tag}**: {msg}")
    out("")

    out("---")
    out("")
    out(
        "_Generated by `dev-insights` skill. Data sources: "
        "`~/.claude/history.jsonl`, `~/.claude/projects/`, local git repo._"
    )
    return "\n".join(lines)


def derive_insights(history, transcripts, git):
    """Return a list of (tag, message) tuples flagging notable patterns. Heuristic, not gospel."""
    insights = []

    total = history["total_prompts"] or 1
    sessions = max(1, len(history["sessions"]))

    clears_per_session = history["clear_count"] / sessions
    if clears_per_session > 1.0:
        insights.append(
            (
                "Context churn",
                f"{history['clear_count']} `/clear`s across {sessions} sessions "
                f"(~{clears_per_session:.1f}/session). Consider keeping related work in one context.",
            )
        )

    terse_share = history["terse_count"] / total
    if terse_share > 0.20:
        insights.append(
            (
                "Terse prompting",
                f"{history['terse_count']} terse prompts ({pct(history['terse_count'], total)}). "
                "Front-loading constraints in your initial prompt reduces re-work.",
            )
        )

    if history["fix_terse_count"]:
        verify_ratio = history["verify_followup_count"] / history["fix_terse_count"]
        if verify_ratio < 0.3:
            insights.append(
                (
                    "Verification gap",
                    f"Only {pct(history['verify_followup_count'], history['fix_terse_count'])} "
                    "of terse approvals were followed by a test/build prompt. Consider running tests after fixes.",
                )
            )

    if history["overeng_pushback_count"] >= 5:
        insights.append(
            (
                "Reactive simplification",
                f"{history['overeng_pushback_count']} prompts asked to remove overengineering. "
                "Add anti-overengineering constraints to your initial prompts so it isn't written in the first place.",
            )
        )

    if history["friction_counter"]:
        insights.append(
            (
                "Friction signals",
                "Multiple `/login`/`/usage`/`/rate-limit-options` calls — possibly running too many parallel "
                "agents or hitting account limits.",
            )
        )

    if history["late_night_count"] / total > 0.20:
        insights.append(
            (
                "Late-night work",
                f"{pct(history['late_night_count'], total)} of prompts happen late-night/early-morning. "
                "Watch for fatigue-driven mistakes.",
            )
        )

    if history["weekend_count"] / total > 0.25:
        insights.append(
            (
                "Weekend load",
                f"{pct(history['weekend_count'], total)} of prompts on weekends.",
            )
        )

    # Project sprawl
    proj_count = len(history["prompts_per_project"])
    if proj_count >= 4:
        insights.append(
            (
                "Project switching",
                f"Active across {proj_count} different projects. Heavy context-switching has a cognitive tax.",
            )
        )

    # Tool mix observations
    tc = transcripts["tool_counter"]
    if tc:
        total_tools = sum(tc.values())
        agent_share = tc.get("Agent", 0) / total_tools
        if agent_share > 0.15:
            insights.append(
                (
                    "Heavy delegation",
                    f"{pct(tc.get('Agent',0), total_tools)} of tool calls dispatched subagents. "
                    "Powerful for breadth but expensive in tokens — make sure each one earns its cost.",
                )
            )

    return insights


# ---------- Main ----------


def main():
    parser = argparse.ArgumentParser(description="Comprehensive local Claude Code dev analytics")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--project", type=str, default=None, help="Filter to a single project (short name)")
    parser.add_argument("--output", type=str, default=None, help="Write report to file (default: stdout)")
    parser.add_argument("--no-transcripts", action="store_true", help="Skip session transcript parsing")
    parser.add_argument("--no-git", action="store_true", help="Skip git log parsing")
    parser.add_argument("--git-dir", type=str, default=None, help="Repo to analyze (default: cwd)")
    args = parser.parse_args()

    cutoff = now_utc() - timedelta(days=args.days)
    period_label = f"last {args.days} days (since {cutoff.astimezone().strftime('%Y-%m-%d')})"

    print(f"[dev-insights] analyzing {period_label}…", file=sys.stderr)

    history = analyze_history(cutoff, project_filter=args.project)
    print(f"[dev-insights] history: {history['total_prompts']} prompts", file=sys.stderr)

    if args.no_transcripts:
        transcripts = {
            "tool_counter": Counter(),
            "file_edits": Counter(),
            "file_reads": Counter(),
            "bash_first_token": Counter(),
            "subagent_dispatches": 0,
            "tokens_input": 0,
            "tokens_output": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "branches_touched": Counter(),
            "sessions_inspected": 0,
            "models_used": Counter(),
        }
    else:
        transcripts = analyze_transcripts(cutoff, project_filter=args.project)
        print(
            f"[dev-insights] transcripts: {transcripts['sessions_inspected']} sessions, "
            f"{sum(transcripts['tool_counter'].values())} tool calls",
            file=sys.stderr,
        )

    if args.no_git:
        git = {}
    else:
        repo = Path(args.git_dir).resolve() if args.git_dir else Path.cwd().resolve()
        git = analyze_git(cutoff, repo)
        if git.get("commits"):
            print(f"[dev-insights] git: {git['commits']} commits in {repo}", file=sys.stderr)

    report = render(history, transcripts, git, args, period_label)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"[dev-insights] wrote report → {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
