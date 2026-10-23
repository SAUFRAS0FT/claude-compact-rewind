#!/usr/bin/env python3
"""claude-compact-rewind: undo a Claude Code auto-compaction in place.

Rewinds a Claude Code session transcript (~/.claude/projects/<project>/<session-id>.jsonl)
to a point before a compaction, so the *same* session (same id, still listed in the
Claude Desktop sidebar) resumes with its full pre-compaction context.

Sub-commands:
  list      sessions that contain at least one compaction
  show      readable timeline around a compaction (one line per message)
  restore   plan (dry-run, default) or apply (--apply) a rewind
  rollback  restore a backup written by `restore --apply`

Python 3.8+, standard library only. The transcript format is internal to Claude Code
and undocumented: see README.md for the assumptions and the versions it was tested on.
"""
import argparse
import csv
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

__version__ = "0.1.0"
TESTED_WITH = "Claude Code 2.1.308"

CHAIN_TYPES = ("user", "assistant", "system", "attachment")
LINK_KEYS = ("parentUuid", "logicalParentUuid", "leafUuid")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
MIN_IDLE_SECONDS = 60
DEFAULT_IDLE_COMPACT_MIN_TOKENS = 200_000
NOT_PROMPT_PREFIXES = ("<local-command", "<system-reminder", "<task-notification", "<side-sessions",
                       "<command-message", "<bash-", "<user-memory", "Caveat:",
                       "[Request interrupted")


class ToolError(Exception):
    """A refusal or a user-facing error: printed without a traceback, exit code 2."""


# --------------------------------------------------------------------------- console

class Style:
    enabled = False
    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36"}

    @classmethod
    def setup(cls, no_color=False):
        cls.enabled = (not no_color and "NO_COLOR" not in os.environ
                       and hasattr(sys.stdout, "isatty") and sys.stdout.isatty())
        if cls.enabled and os.name == "nt":
            cls.enabled = _enable_windows_vt()

    @classmethod
    def paint(cls, text, *names):
        if not cls.enabled or not names:
            return text
        return "\x1b[%sm%s\x1b[0m" % (";".join(cls.CODES[n] for n in names), text)


def _enable_windows_vt():
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def setup_stdout():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def P(text="", *styles):
    print(Style.paint(text, *styles) if styles else text)


def term_width():
    return max(40, shutil.get_terminal_size((100, 24)).columns)


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip(text, width, tail=False):
    """Single-line text clipped to `width` columns; tail=True keeps the end."""
    text = re.sub(r"\s*[\r\n\t\f\v]\s*", " ", str(text)).strip()
    if sum(char_width(c) for c in text) <= width:
        return text
    chars = list(reversed(text)) if tail else list(text)
    out, used = [], 1
    for c in chars:
        w = char_width(c)
        if used + w > width:
            break
        out.append(c)
        used += w
    return "…" + "".join(reversed(out)) if tail else "".join(out) + "…"


def tilde(path):
    """Display a path with the home directory replaced by ~ (no user name on screen)."""
    path = str(path)
    home = os.path.expanduser("~")
    if home and os.path.normcase(path).startswith(os.path.normcase(home)):
        return "~" + path[len(home):]
    return path


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def ktok(n):
    if n is None:
        return "?"
    return "%dk" % round(n / 1000) if n >= 1000 else str(n)


def parse_ts(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_ts(ts, with_date=True):
    d = parse_ts(ts)
    if d is None:
        return "?" if not ts else ts
    d = d.astimezone()
    return d.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- locations

def config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.abspath(os.path.expanduser(env)) if env else os.path.join(os.path.expanduser("~"), ".claude")


def projects_dir():
    return os.path.join(config_dir(), "projects")


def file_history_dir(session_id):
    return os.path.join(config_dir(), "file-history", session_id)


def default_backup_root():
    return os.path.join(config_dir(), "compact-rewind-backups")


def session_files():
    """Main session transcripts (sub-agent transcripts excluded), most recent first."""
    files = [f for f in glob.glob(os.path.join(projects_dir(), "*", "*.jsonl"))
             if not os.path.basename(f).startswith("agent-")]
    return sorted(files, key=os.path.getmtime, reverse=True)


# --------------------------------------------------------------------------- transcript model

def is_boundary(r):
    return bool(r) and r.get("type") == "system" and r.get("subtype") == "compact_boundary"


def content_of(r):
    m = r.get("message") if r else None
    return m.get("content") if isinstance(m, dict) else None


def text_of(r):
    """Concatenated text blocks of a record ('' when none)."""
    c = content_of(r)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def has_tool_result(r):
    c = content_of(r)
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def command_name(r):
    m = re.search(r"<command-name>\s*([^<]+?)\s*</command-name>", text_of(r))
    return m.group(1) if m else None


def is_prompt(r):
    """A message typed by the user (not tool results, meta records, summaries or command plumbing)."""
    if not r or r.get("type") != "user" or r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary"):
        return False
    if has_tool_result(r):
        return False
    t = text_of(r).lstrip()
    return bool(t) and not t.startswith(NOT_PROMPT_PREFIXES) and command_name(r) is None


def usage_total(r):
    m = r.get("message") if r else None
    u = (m or {}).get("usage") if isinstance(m, dict) else None
    if not u:
        return 0
    return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                        "cache_creation_input_tokens", "output_tokens"))


def stop_reason(r):
    m = r.get("message") if r else None
    return m.get("stop_reason") if isinstance(m, dict) else None


class Transcript:
    def __init__(self, path, raw=None):
        self.path = os.path.abspath(path)
        if raw is None:
            with open(self.path, "rb") as fh:
                raw = fh.read().splitlines(keepends=True)
        self.raw = raw
        self.recs = []
        for line in self.raw:
            try:
                r = json.loads(line)
                self.recs.append(r if isinstance(r, dict) else None)
            except ValueError:
                self.recs.append(None)  # unreadable line: always copied byte for byte
        self.session_id = os.path.splitext(os.path.basename(self.path))[0]
        self.boundaries = [i for i, r in enumerate(self.recs) if is_boundary(r)]
        self.by_uuid = {}  # last occurrence wins, like the loader's map
        for i, r in enumerate(self.recs):
            if r and r.get("uuid"):
                self.by_uuid[r["uuid"]] = i

    @property
    def size(self):
        return sum(len(l) for l in self.raw)

    @property
    def cwd(self):
        return next((r["cwd"] for r in self.recs if r and r.get("cwd")), None)

    def title_before(self, end):
        return next((r.get("customTitle") for r in reversed(self.recs[:end])
                     if r and r.get("type") == "custom-title"), None)

    @property
    def title(self):
        return self.title_before(len(self.recs))

    @property
    def model(self):
        return next(((r.get("message") or {}).get("model") for r in reversed(self.recs)
                     if r and r.get("type") == "assistant" and (r.get("message") or {}).get("model")
                     and not str((r.get("message") or {}).get("model")).startswith("<")), None)

    def next_boundary(self, b):
        return next((i for i in self.boundaries if i > b), len(self.recs))

    def compact_meta(self, b):
        return self.recs[b].get("compactMetadata") or {}

    def idle_notice(self, b):
        """Text of the 'compacted while you were away' notice following boundary b, if any."""
        for r in self.recs[b + 1:self.next_boundary(b)]:
            if r and r.get("type") == "system" and r.get("subtype") == "informational":
                txt = str(r.get("content") or "")
                if "idleCompaction" in txt or "were away" in txt or "idle" in txt.lower():
                    return txt
        return None


def boundary_cause(t, b):
    cm = t.compact_meta(b)
    if cm.get("trigger") == "manual":
        return "manual", "/compact was run manually"
    if t.idle_notice(b) is not None:
        return "idle", "idle compaction: summary written while you were away, applied on your next message"
    if cm.get("precomputed"):
        return "precomputed", "auto compaction with a summary precomputed in the background (idle compaction or precomputeCompactionEnabled)"
    return "auto", "auto compaction: the context reached the auto-compact window"


# --------------------------------------------------------------------------- readable timeline

TOOL_ARG_KEYS = ("file_path", "notebook_path", "path", "pattern", "command", "url", "query",
                 "description", "skill", "prompt")


def tool_summary(block):
    name = block.get("name") or "?"
    short = name.split("__")[-1] if name.startswith("mcp__") else name
    inp = block.get("input") or {}
    arg = next((inp[k] for k in TOOL_ARG_KEYS if isinstance(inp.get(k), str) and inp.get(k)), "")
    if arg and any(k in inp for k in ("file_path", "notebook_path")):
        arg = os.path.basename(arg.replace("\\", "/"))
    home = os.path.expanduser("~")
    arg = arg.replace(home, "~").replace(home.replace("\\", "/"), "~") if home else arg
    arg = clip(arg, 40)
    return "%s %s" % (short, arg) if arg else short


class Item:
    __slots__ = ("first", "last", "kind", "ts", "text", "tools", "stop")

    def __init__(self, first, kind, ts, text="", tools=None, stop=None):
        self.first, self.last, self.kind, self.ts = first, first, kind, ts
        self.text, self.tools, self.stop = text, tools or [], stop


def build_items(t, lo, hi, show_all=False):
    """Group records [lo, hi) into display items: one per prompt / assistant API message / boundary."""
    items, cur, cur_mid = [], None, None
    for i in range(lo, hi):
        r = t.recs[i]
        if not r or r.get("isSidechain"):
            continue
        typ = r.get("type")
        if typ == "assistant":
            mid = (r.get("message") or {}).get("id")
            if cur is None or not mid or mid != cur_mid:  # one API message is split over several records
                cur, cur_mid = Item(i, "assistant", r.get("timestamp"), ""), mid
                items.append(cur)
            cur.last = i
            for blk in content_of(r) or []:
                if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text", "").strip():
                    cur.text = blk["text"]
                elif isinstance(blk, dict) and blk.get("type") == "tool_use":
                    cur.tools.append(tool_summary(blk))
            cur.stop = stop_reason(r) or cur.stop
            continue
        if typ in ("user", "system") or show_all:
            cur = None  # attachments and bookkeeping records do not split an assistant message
        if is_boundary(r):
            items.append(Item(i, "boundary", r.get("timestamp")))
        elif typ == "user" and r.get("isCompactSummary"):
            items.append(Item(i, "summary", r.get("timestamp"), text_of(r)))
        elif is_prompt(r):
            items.append(Item(i, "user", r.get("timestamp"), text_of(r)))
        elif typ == "user" and command_name(r):
            items.append(Item(i, "command", r.get("timestamp"), command_name(r)))
        elif show_all and typ == "user" and has_tool_result(r):
            items.append(Item(i, "result", r.get("timestamp"), "tool result"))
        elif show_all and typ in ("system", "attachment", "user"):
            sub = r.get("subtype") or (r.get("attachment") or {}).get("type") or ("meta" if r.get("isMeta") else "")
            items.append(Item(i, typ, r.get("timestamp"), sub))
    return items


def collapse_tools(tools):
    out = []
    for name in tools:
        if out and out[-1][0] == name:
            out[-1][1] += 1
        else:
            out.append([name, 1])
    return ", ".join(n if c == 1 else "%s ×%d" % (n, c) for n, c in out)


def render_item(t, it, width, marks):
    label = {"user": "USER", "assistant": "ASSIST", "summary": "SUMMARY", "command": "CMD",
             "boundary": "", "result": "result"}.get(it.kind, it.kind[:7])
    ts = local_ts(it.ts, with_date=False)
    mark = marks.get(it.last, "")
    head = "L%-6d %s %-7s " % (it.last + 1, ts, label)
    if it.kind == "boundary":
        cm = t.compact_meta(it.first)
        kind, _ = boundary_cause(t, it.first)
        txt = " compaction (%s) %s → %s tokens " % (kind, ktok(cm.get("preTokens")), ktok(cm.get("postTokens")))
        line = "L%-6d %s %s" % (it.first + 1, ts, txt.center(max(10, width - 17), "═"))
        return Style.paint(clip(line, width), "magenta", "bold")
    room = max(10, width - len(head) - (len(mark) + 1 if mark else 0))
    if it.kind == "assistant":
        tools = collapse_tools(it.tools)
        body = " · ".join(x for x in ("⚙ " + tools if tools else "", " ".join(it.text.split())) if x)
        text = clip(body, room, tail=bool(it.text))
        if tools and it.text and not text.startswith("⚙"):
            text = clip("⚙ " + clip(tools, room // 3) + " · " + " ".join(it.text.split()), room, tail=True)
        color = ("green",) if it.stop == "end_turn" else ()
    elif it.kind == "user":
        text, color = clip(it.text, room), ("cyan", "bold")
    elif it.kind == "summary":
        text, color = clip(it.text, room), ("magenta",)
    else:
        text, color = clip(it.text, room), ("dim",)
    line = Style.paint(head, "dim") + Style.paint(text, *color)
    if mark:
        line += " " + Style.paint(mark, "yellow", "bold")
    return line


def page_lines(lines, page_size):
    interactive = sys.stdout.isatty() and sys.stdin and sys.stdin.isatty() and page_size > 0
    for n, line in enumerate(lines, 1):
        print(line)
        if interactive and n % page_size == 0 and n < len(lines):
            try:
                ans = input(Style.paint("-- %d/%d -- Enter: more, q: quit -- " % (n, len(lines)), "dim"))
            except EOFError:
                return
            if ans.strip().lower() == "q":
                return


# --------------------------------------------------------------------------- loader simulation

def simulate(lines):
    """Approximates the 2.1.308 loader on a list of raw lines.

    Leaf = the chain record (user/assistant/system/attachment, not sidechain) that no record
    references as parentUuid, with the most recent timestamp; `last-prompt.leafUuid` is NOT
    used. The chain is then walked up through parentUuid. Without any compact_boundary the
    loader reads the whole file, so the walk must reach the first chain record of the file.
    """
    recs = []
    for l in lines:
        try:
            r = json.loads(l)
            recs.append(r if isinstance(r, dict) else None)
        except ValueError:
            recs.append(None)
    by, line_of = {}, {}
    for i, r in enumerate(recs):
        if r and r.get("uuid"):
            by[r["uuid"]], line_of[r["uuid"]] = r, i
    parents = {r.get("parentUuid") for r in recs if r and r.get("parentUuid")}
    leaf = None
    for r in recs:  # ISO-8601 UTC timestamps: lexical order == chronological order
        if (r and r.get("uuid") and r["uuid"] not in parents and r.get("type") in CHAIN_TYPES
                and not r.get("isSidechain")):
            if leaf is None or (r.get("timestamp") or "") >= (leaf.get("timestamp") or ""):
                leaf = r
    chain, seen, cur, broken = [], set(), leaf, None
    while cur is not None and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        chain.append(cur)
        p = cur.get("parentUuid")
        if p and p not in by:
            broken = p
        cur = by.get(p) if p else None
    chain.reverse()
    first_chain = next((r for r in recs if r and r.get("uuid") and r.get("type") in CHAIN_TYPES
                        and not r.get("isSidechain")), None)
    orphans = [line_of[r["uuid"]] + 1 for r in recs
               if r and r.get("uuid") and r.get("parentUuid") and r["parentUuid"] not in by]
    last_asst = next((r for r in reversed(chain) if r.get("type") == "assistant"), None)
    last_used = next((usage_total(r) for r in reversed(chain) if r.get("type") == "assistant" and usage_total(r)), 0)
    last_text = next((text_of(r) for r in reversed(chain) if r.get("type") == "assistant" and text_of(r).strip()), "")
    return {
        "leaf_line": line_of[leaf["uuid"]] + 1 if leaf else None,
        "leaf_kind": (leaf.get("type") + ("/" + leaf["subtype"] if leaf.get("subtype") else "")) if leaf else None,
        "chain_records": len(chain),
        "chain_user": sum(1 for r in chain if r.get("type") == "user"),
        "chain_assistant": sum(1 for r in chain if r.get("type") == "assistant"),
        "chain_prompts": sum(1 for r in chain if is_prompt(r)),
        "boundary_in_chain": any(is_boundary(r) for r in chain),
        "summary_in_chain": any(r.get("isCompactSummary") for r in chain),
        "root_is_first_record": bool(chain) and first_chain is not None and chain[0] is first_chain,
        "root_is_boundary": bool(chain) and is_boundary(chain[0]),
        "root_line": line_of[chain[0]["uuid"]] + 1 if chain else None,
        "broken_parent": broken,
        "orphan_lines": orphans,
        "boundaries_left": sum(1 for r in recs if is_boundary(r)),
        "last_assistant_line": line_of[last_asst["uuid"]] + 1 if last_asst else None,
        "last_assistant_text_end": " ".join(last_text.split())[-160:],
        "context_tokens_last_usage": last_used,
    }


def print_simulation(sim, allow_earlier_compaction=False):
    """Prints the simulation; True when the chain is what the rewind intends."""
    sound = not sim["orphan_lines"] and not sim["broken_parent"]
    full = sound and sim["root_is_first_record"] and not sim["boundary_in_chain"] and not sim["summary_in_chain"]
    partial = sound and allow_earlier_compaction and sim["root_is_boundary"]
    ok = full or partial
    P("Loader simulation of the result:", "bold")
    P("  leaf                 L%s (%s)" % (sim["leaf_line"], sim["leaf_kind"]))
    P("  chain                %d records: %d user (%d prompts), %d assistant" % (
        sim["chain_records"], sim["chain_user"], sim["chain_prompts"], sim["chain_assistant"]))
    found = [n for n, k in (("boundary", "boundary_in_chain"), ("summary", "summary_in_chain")) if sim[k]]
    P("  compaction in chain  %s" % (" + ".join(found) or "none"))
    P("  root = first record  %s" % sim["root_is_first_record"])
    P("  orphans              %s" % (sim["orphan_lines"][:10] or "none")
      + (" (+%d)" % (len(sim["orphan_lines"]) - 10) if len(sim["orphan_lines"]) > 10 else ""))
    P("  boundaries left      %d" % sim["boundaries_left"])
    if sim["last_assistant_text_end"]:
        P("  last assistant text  " + clip(sim["last_assistant_text_end"], term_width() - 26, tail=True))
    if full:
        P("  verdict              full chain back to the first record, no compaction", "green", "bold")
    elif partial:
        P("  verdict              chain starts at an earlier compaction (L%d), kept as is" % sim["root_line"],
          "yellow", "bold")
    else:
        P("  verdict              INCOMPLETE CHAIN (see above)", "red", "bold")
    return ok


# --------------------------------------------------------------------------- planning

def choose_boundary(t, index):
    if not t.boundaries:
        raise ToolError("no compaction (compact_boundary) in this transcript: nothing to rewind")
    if index == "all":
        return t.boundaries[-1]
    if index is not None:
        try:
            return t.boundaries[int(index)]
        except IndexError:
            raise ToolError("--boundary %s: this transcript has %d compaction(s) (0..%d)"
                            % (index, len(t.boundaries), len(t.boundaries) - 1))
    autos = [b for b in t.boundaries if t.compact_meta(b).get("trigger") == "auto"]
    return autos[-1] if autos else t.boundaries[-1]


def auto_cut(t, b):
    """Line index just after the last complete turn before boundary b (end_turn + its stop hook record)."""
    for i in range(b - 1, -1, -1):
        r = t.recs[i]
        if is_boundary(r):
            break
        if r and r.get("type") == "assistant" and not r.get("isSidechain") and stop_reason(r) == "end_turn":
            cut = i + 1
            for j in range(i + 1, b):
                x = t.recs[j]
                if x and x.get("type") == "system" and x.get("subtype") == "stop_hook_summary" \
                        and x.get("parentUuid") == r.get("uuid"):
                    cut = j + 1
                    break
                if x and x.get("type") in ("user", "assistant"):
                    break
            return cut
    return None


def pre_chain_leaf(t, b):
    """Last chain record (not sidechain) before the boundary: fallback when logicalParentUuid is missing."""
    for i in range(b - 1, -1, -1):
        r = t.recs[i]
        if r and r.get("uuid") and r.get("type") in CHAIN_TYPES and not r.get("isSidechain"):
            return r["uuid"]
    return None


def restitch_lines(t, b):
    """Drop boundary b and its isCompactSummary record(s); relink their children. Returns (lines, removed, modified, notes)."""
    bnd = t.recs[b]
    nxt = t.next_boundary(b)
    notes = []
    lp = bnd.get("logicalParentUuid")
    if lp not in t.by_uuid or t.by_uuid[lp] > b:
        fb = pre_chain_leaf(t, b)
        notes.append("logicalParentUuid %s is not in the file before the boundary: children relinked to L%s instead"
                     % (lp, t.by_uuid.get(fb, -2) + 1))
        lp = fb
    repl = {bnd["uuid"]: lp}
    for i in range(b + 1, nxt):
        r = t.recs[i]
        if r and r.get("isCompactSummary") and r.get("uuid"):
            repl[r["uuid"]] = r.get("parentUuid")

    def resolve(u):
        seen = set()
        while u in repl and u not in seen:
            seen.add(u)
            u = repl[u]
        return u

    out, removed, modified = [], [], []
    for i, (line, r) in enumerate(zip(t.raw, t.recs)):
        if r and r.get("uuid") in repl and (i == b or (b < i < nxt and r.get("isCompactSummary"))):
            removed.append(i)
            continue
        if r and any(r.get(k) in repl for k in LINK_KEYS):
            r = json.loads(line)  # fresh copy: never mutate the parsed transcript
            for k in LINK_KEYS:
                if r.get(k) in repl:
                    r[k] = resolve(r[k])
            eol = line[len(line.rstrip(b"\r\n")):]
            line = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + eol
            modified.append(i)
        out.append(line)
    return out, removed, modified, notes


class Plan:
    def __init__(self):
        self.mode = None
        self.boundary = None
        self.cut = None          # truncate: number of lines kept
        self.lines = []
        self.removed = []
        self.modified = []
        self.notes = []
        self.removed_prompts = []
        self.estimate = None
        self.at = None


def build_plan(t, mode="truncate", compaction=None, at_line=None):
    """compaction: index, "all" (restitch only) or None (last auto); at_line: keep lines 1..at_line (truncate)."""
    plan = Plan()
    plan.mode = mode
    if mode == "truncate" and compaction == "all":
        raise ToolError("--compaction all only works with --keep-after")
    b = plan.boundary = choose_boundary(t, compaction)
    if b < 2 or not any(r and r.get("type") in ("user", "assistant") for r in t.recs[:b]):
        raise ToolError("the messages from before this compaction are no longer in the file, "
                        "so there is nothing to bring back")
    if t.recs[b].get("logicalParentUuid") not in t.by_uuid:
        plan.notes.append("the record the compaction points to is missing: part of the old history may be gone")
    if mode == "truncate":
        if at_line is not None:
            if not 1 <= at_line <= len(t.recs):
                raise ToolError("--at %d is outside the file (1..%d)" % (at_line, len(t.recs)))
            if at_line > b:
                raise ToolError("--at %d is after the compaction (line %d): pick an earlier line (see `show`)"
                                % (at_line, b + 1))
            plan.cut = at_line
        else:
            plan.cut = auto_cut(t, b)
            if plan.cut is None:
                raise ToolError("no complete answer from Claude before the compaction: pick a line with --at "
                                "(see `show`)")
        plan.lines = t.raw[:plan.cut]
        plan.removed = list(range(plan.cut, len(t.raw)))
        plan.removed_prompts = [i for i in plan.removed if is_prompt(t.recs[i])]
        plan.estimate = _estimate_truncate(t, plan.cut)
    else:
        targets = t.boundaries if compaction == "all" else [b]
        cur = t
        for k in reversed(range(len(targets))):  # last first: earlier line numbers stay valid
            lines, _, _, notes = restitch_lines(cur, targets[k])
            plan.notes.extend(notes)
            cur = Transcript(t.path, lines)
        plan.lines = cur.raw
        kept = set(cur.by_uuid)
        plan.removed = [i for i, r in enumerate(t.recs) if r and r.get("uuid") and r["uuid"] not in kept]
        original = set(t.raw)
        plan.modified = [i for i, l in enumerate(plan.lines) if l not in original]  # indexes in the result
        plan.estimate = _estimate_restitch(t, targets)
    return plan


def _estimate_restitch(t, targets):
    """Pre-compaction context of the first target + what each later segment added on top of its summary."""
    metas = [t.compact_meta(b) for b in targets]
    if not metas[0].get("preTokens"):
        return None
    est = metas[0]["preTokens"]
    for prev, cm in zip(metas, metas[1:]):
        est += max(0, (cm.get("preTokens") or 0) - (prev.get("postTokens") or 0))
    last = targets[-1]
    after = next((usage_total(r) for r in reversed(t.recs[last + 1:t.next_boundary(last)])
                  if r and r.get("type") == "assistant" and usage_total(r)), 0)
    return est + max(0, after - (metas[-1].get("postTokens") or 0))


def _estimate_truncate(t, cut):
    for r in reversed(t.recs[:cut]):
        if is_boundary(r):
            break
        if r and r.get("type") == "assistant" and not r.get("isSidechain") and usage_total(r):
            return usage_total(r)
    return None


# --------------------------------------------------------------------------- file restoration

def norm(path):
    return os.path.normcase(os.path.normpath(path))


def path_matches(abs_path, key):
    """Does a file-history key (absolute, or relative to the session's root) designate abs_path?"""
    a, k = norm(abs_path), norm(key)
    if os.path.isabs(key):
        return a == k
    return a == k or a.endswith(os.sep + k)


def tool_errors(t):
    errs = set()
    for r in t.recs:
        if r and r.get("type") == "user" and isinstance(content_of(r), list):
            for b in content_of(r):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    errs.add(b.get("tool_use_id"))
    return errs


def edits_after(t, start):
    """Edit/Write/MultiEdit/NotebookEdit tool calls at lines >= start: list of (line, path, block)."""
    out = []
    for i in range(start, len(t.recs)):
        r = t.recs[i]
        if not r or r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        for blk in content_of(r) or []:
            if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") in EDIT_TOOLS:
                inp = blk.get("input") or {}
                path = inp.get("file_path") or inp.get("notebook_path")
                if path:
                    out.append((i, path, blk))
    return out


def subagent_edits_after(t, since_ts):
    """Edits made by sub-agents after a timestamp (sub-agent transcripts live in <session-id>/subagents/)."""
    out = []
    companion = os.path.splitext(t.path)[0]
    for f in sorted(glob.glob(os.path.join(companion, "subagents", "*.jsonl"))):
        try:
            sub = Transcript(f)
        except OSError:
            continue
        for i, r in enumerate(sub.recs):
            if r and r.get("type") == "assistant" and (r.get("timestamp") or "") > (since_ts or ""):
                for blk in content_of(r) or []:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") in EDIT_TOOLS:
                        inp = blk.get("input") or {}
                        p = inp.get("file_path") or inp.get("notebook_path")
                        if p:
                            out.append((os.path.basename(f), p))
    return out


def history_events(t):
    """(line, kind, key, backupFileName-or-None, extra) for every file-history entry, in file order."""
    ev = []
    for i, r in enumerate(t.recs):
        if not r:
            continue
        if r.get("type") == "file-history-snapshot":
            for key, b in ((r.get("snapshot") or {}).get("trackedFileBackups") or {}).items():
                if isinstance(b, dict):
                    ev.append((i, "snapshot", key, b.get("backupFileName"), r.get("messageId")))
        elif r.get("type") == "file-history-delta":
            b = r.get("backup") or {}
            ev.append((i, "delta", r.get("trackingPath"), b.get("backupFileName"), r.get("messageId")))
    return ev


def apply_edit(text, inp, name, crlf):
    def rep(s, old, new, all_):
        if old not in s and crlf and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        if old not in s:
            raise ValueError("old_string not found")
        return s.replace(old, new) if all_ else s.replace(old, new, 1)
    if name == "Write":
        return inp.get("content", "")
    if name == "Edit":
        return rep(text, inp.get("old_string", ""), inp.get("new_string", ""), inp.get("replace_all"))
    if name == "MultiEdit":
        for e in inp.get("edits") or []:
            text = rep(text, e.get("old_string", ""), e.get("new_string", ""), e.get("replace_all"))
        return text
    raise ValueError("cannot replay %s" % name)


class FileAction:
    __slots__ = ("path", "edits", "first_line", "key", "backup_name", "backup_path", "status", "detail",
                 "target_sha", "current_sha")

    def __init__(self, path):
        self.path, self.edits = path, []
        self.first_line = self.key = self.backup_name = self.backup_path = None
        self.status = self.detail = self.target_sha = self.current_sha = None

    @property
    def restorable(self):
        return self.status in ("ok", "unverified")


def plan_files(t, cut):
    """For every file edited after the restore point, find its content at the restore point in file-history."""
    edits = edits_after(t, cut)
    if not edits:
        return []
    errors = tool_errors(t)
    events = history_events(t)
    actions, order = {}, []
    for line, path, blk in edits:
        a = actions.get(norm(path))
        if a is None:
            a = actions[norm(path)] = FileAction(path)
            a.first_line = line
            order.append(a)
        a.edits.append((line, blk, blk.get("id") in errors))
    hist_dir = file_history_dir(t.session_id)
    for a in order:
        # content at the restore point = first history entry for this file after the cut and no later than
        # its first post-cut edit: a turn snapshot (state at turn start) or the delta written for that edit.
        edit_uuid = t.recs[a.first_line].get("uuid")
        cand = None
        for line, kind, key, name, mid in events:
            if line < cut or not key or not path_matches(a.path, key):
                continue
            if kind == "snapshot" and line < a.first_line:
                cand = (key, name)
                break
            if kind == "delta" and mid == edit_uuid:
                cand = (key, name)
                break
            if line > a.first_line + 50:
                break
        if cand is None:
            a.status, a.detail = "no-backup", "no file-history entry for its state at the restore point"
            continue
        a.key, a.backup_name = cand
        if a.backup_name is None:
            a.status = "created"
            a.detail = "did not exist at the restore point (created later): left untouched, delete it yourself if needed"
            continue
        a.backup_path = os.path.join(hist_dir, a.backup_name)
        if not os.path.isfile(a.backup_path):
            a.status, a.detail = "missing-backup", "backup %s not found in file-history" % a.backup_name
            continue
        a.target_sha = sha256_file(a.backup_path)
        _verify_current(t, a, events)
    return order


def _verify_current(t, a, events):
    """Prove the file on disk is exactly what the recorded edits produced (no change made elsewhere)."""
    if not os.path.isfile(a.path):
        a.status, a.detail = "unverified", "file no longer exists on disk"
        return
    a.current_sha = sha256_file(a.path)
    if a.current_sha == a.target_sha:
        a.status, a.detail = "unchanged", "already identical to its restore-point version"
        return
    last_edit = a.edits[-1][0]
    later = [(line, name) for line, kind, key, name, _ in events
             if kind == "snapshot" and line > last_edit and key == a.key]
    if later:
        name = later[0][1]
        p = os.path.join(file_history_dir(t.session_id), name) if name else None
        if p and os.path.isfile(p) and sha256_file(p) == a.current_sha:
            a.status, a.detail = "ok", "current content matches file-history %s" % name
        else:
            a.status, a.detail = "unverified", "current content differs from file-history after the last edit (changed elsewhere?)"
        return
    try:
        with open(a.backup_path, "rb") as fh:
            base = fh.read()
        text = base.decode("utf-8")
        crlf = "\r\n" in text
        for line, blk, failed in a.edits:
            if not failed:
                text = apply_edit(text, blk.get("input") or {}, blk.get("name"), crlf)
        replay = text.encode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        a.status, a.detail = "unverified", "edits could not be replayed (%s)" % e
        return
    if sha256_bytes(replay) == a.current_sha:
        a.status, a.detail = "ok", "current content = restore-point version + the %d recorded edit(s)" % len(a.edits)
    else:
        a.status, a.detail = "unverified", "current content differs from a replay of the recorded edits (changed elsewhere?)"


def print_files(actions, sub_edits):
    if not actions and not sub_edits:
        P("Files edited after the restore point: none")
        return
    P("Files edited after the restore point (%d):" % len(actions), "bold")
    color = {"ok": "green", "unchanged": "dim", "created": "yellow", "unverified": "yellow"}
    for a in actions:
        tag = {"ok": "restorable", "unverified": "UNVERIFIED"}.get(a.status, a.status)
        P("  %-12s %s" % (tag, tilde(a.path)), color.get(a.status, "red"))
        P("               %d edit(s) from L%d · %s%s" % (
            len(a.edits), a.first_line + 1, a.detail,
            (" · backup " + a.backup_name) if a.backup_name and a.status in ("ok", "unverified") else ""), "dim")
    for agent, p in sub_edits:
        P("  %-12s %s (by sub-agent %s: not covered by file-history, review by hand)" % ("sub-agent", tilde(p), agent),
          "yellow")


# --------------------------------------------------------------------------- settings diagnostic

def managed_settings_path():
    if os.name == "nt":
        return r"C:\Program Files\ClaudeCode\managed-settings.json"
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode/managed-settings.json"
    return "/etc/claude-code/managed-settings.json"


def settings_sources(cwd):
    srcs = [("user", os.path.join(config_dir(), "settings.json"))]
    if cwd:
        srcs += [("project", os.path.join(cwd, ".claude", "settings.json")),
                 ("project-local", os.path.join(cwd, ".claude", "settings.local.json"))]
    srcs.append(("managed", managed_settings_path()))
    out = []
    for name, path in srcs:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            out.append((name, path, None, str(e)))
            continue
        out.append((name, path, data if isinstance(data, dict) else {}, None))
    return out


def compaction_settings(cwd, model):
    """Collected values, lowest to highest precedence: user < project < project-local < managed < env."""
    found = []
    for name, path, data, err in settings_sources(cwd):
        if data is None:
            found.append((name, path, "unreadable", err))
            continue
        for key in ("autoCompactWindow", "idleCompaction", "autoCompactEnabled", "precomputeCompactionEnabled"):
            if key in data:
                found.append((name, path, key, data[key]))
        for m, ms in (data.get("modelSettings") or {}).items():
            if isinstance(ms, dict) and "autoCompactWindow" in ms:
                found.append((name, path, "modelSettings.%s.autoCompactWindow" % m, ms["autoCompactWindow"]))
        for k, v in (data.get("env") or {}).items():
            if k in ENV_KEYS:
                found.append((name, path, "env." + k, v))
    for k in ENV_KEYS:
        if k in os.environ:
            found.append(("environment", "(this shell)", k, os.environ[k]))
    return found


ENV_KEYS = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", "DISABLE_AUTO_COMPACT",
            "DISABLE_COMPACT", "CLAUDE_CODE_DISABLE_PRECOMPACT_SKIP")


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def effective(found, model):
    """Best guess of the effective window and idle setting (Claude Code's exact resolution is internal)."""
    window, wsrc, idle, isrc, idle_min = None, None, None, None, DEFAULT_IDLE_COMPACT_MIN_TOKENS
    for name, path, key, val in found:
        # within one file modelSettings.<model> replaces the top-level value; later files take precedence
        if key == "autoCompactWindow" or (key.startswith("modelSettings.") and model
                                          and _model_key_matches(key.split(".", 2)[1], model)):
            window = _int(val)  # "auto" (window tuned for the model) -> None
            wsrc = "%s settings: %s = %s" % (name, key, json.dumps(val))
        elif key == "idleCompaction":
            idle, isrc = val, "%s settings" % name
        elif key.endswith("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS") and _int(val):
            idle_min = _int(val)
    for name, path, key, val in found:  # env always wins over the settings value
        if key.endswith("CLAUDE_CODE_AUTO_COMPACT_WINDOW") and _int(val):
            window, wsrc = _int(val), "%s %s" % (name, key)
    return window, wsrc, idle, isrc, idle_min


def canonical_model(name):
    """claude-x-1-20300101, claude-x-1[1m], us.anthropic.claude-x-1-v1:0, claude-x-1@20300101 -> claude-x-1"""
    n = name.lower().replace("[1m]", "").strip()
    n = n.split("anthropic.", 1)[-1]
    n = re.sub(r"(-v\d+(:\d+)?|@\d{8}|-\d{8})$", "", n)
    return n


def _model_key_matches(key, model):
    return canonical_model(key) == canonical_model(model)


def diagnose(t, b, estimate):
    """Why compaction b happened and what in the settings would make it happen again."""
    cm = t.compact_meta(b)
    kind, why = boundary_cause(t, b)
    model = t.model
    found = compaction_settings(t.cwd, model)
    window, wsrc, idle, _, idle_min = effective(found, model)
    pre = cm.get("preTokens")
    d = {"kind": kind, "why": why, "model": model, "found": found, "window": window, "wsrc": wsrc,
         "idle": idle, "idle_min": idle_min, "pre": pre, "estimate": estimate, "notice": t.idle_notice(b),
         "settings": tilde(os.path.join(config_dir(), "settings.json")), "tips": [], "suggest": {}}
    tips = d["tips"]
    if estimate and estimate > 1_000_000:
        tips.append(("error", "~%s tokens is more than any model can take: the next message would fail. "
                              "Rewind without --keep-after." % ktok(estimate)))
    if estimate and window and estimate >= window:
        tips.append(("warn", "~%s tokens is above your auto-compact limit (%s): Claude would compact again right "
                             "away. Raise it first (/autocompact in Claude)." % (ktok(estimate), ktok(window))))
    if idle is not False and (kind in ("idle", "precomputed") or (estimate and estimate >= idle_min)):
        tips.append(("warn", 'Idle compaction is on, so this can happen again. To turn it off, add '
                             '"idleCompaction": false to %s' % d["settings"]))
        d["suggest"]["idleCompaction"] = False
    if kind == "auto" and window and pre and pre < window * 0.9:
        tips.append(("info", "It compacted at %s while your limit is now %s: the limit was lower at that time."
                             % (ktok(pre), ktok(window))))
    elif kind == "auto" and not window:
        tips.append(("info", "It reached the default auto-compact limit. To raise it: /autocompact in Claude."))
    need = max(estimate or 0, pre or 0)
    if kind != "manual" and need and (not window or window <= need * 1.2):
        d["suggest"]["autoCompactWindow"] = int(min(1_000_000, max(100_000, -(-(need * 1.5) // 50_000) * 50_000)))
    return d


def print_tips(d):
    for level, text in d["tips"]:
        P(*{"error": ("✖ " + text, "red", "bold"), "warn": ("⚠ " + text, "yellow"),
            "info": ("ℹ " + text, "dim")}[level])


def print_diagnostic(d):
    P("Why it compacted", "bold")
    P("  " + d["why"] + (" (at %s tokens)" % "{:,}".format(d["pre"]) if d["pre"] else ""))
    if d["notice"]:
        P("  message shown: " + clip(d["notice"], term_width() - 18), "dim")
    P("  settings found (model %s):" % (d["model"] or "unknown"))
    if not d["found"]:
        P("    none of autoCompactWindow / idleCompaction / related environment variables", "dim")
    for name, path, key, val in d["found"]:
        P("    %-14s %-48s %s   %s" % (name, key, val if isinstance(val, str) else json.dumps(val), tilde(path)), "dim")
    P("  auto-compact limit: %s%s" % ("{:,}".format(d["window"]) if d["window"] else "default (tuned for the model)",
                                     "  [%s]" % d["wsrc"] if d["wsrc"] else ""))
    P("  idle compaction:    %s" % ("off" if d["idle"] is False else
                                    "on, above %s tokens once the session sits idle" % ktok(d["idle_min"])))
    P("  (Claude Desktop may run with other environment variables than this terminal)", "dim")
    if d["suggest"]:
        P("  suggested change to %s (not applied by this tool):" % d["settings"])
        for k, v in d["suggest"].items():
            P('    + "%s": %s' % (k, json.dumps(v)), "green")
        if "autoCompactWindow" in d["suggest"]:
            P("    range 100000..1000000, never above the model's context window. /autocompact writes "
              "modelSettings.<model>.autoCompactWindow, which wins over the top-level key; "
              "CLAUDE_CODE_AUTO_COMPACT_WINDOW wins over both.", "dim")


# --------------------------------------------------------------------------- guards

def running_claude():
    """Descriptions of running Claude Code / Claude Desktop processes, or None when detection failed."""
    found = []
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, errors="replace", timeout=30).stdout
            for row in csv.reader(out.splitlines()):
                if row and row[0].strip().lower() == "claude.exe":  # exact image name only
                    found.append("%s (pid %s)" % (row[0], row[1] if len(row) > 1 else "?"))
            return found
        checks = [["pgrep", "-x", "claude"], ["pgrep", "-f", r"(^|/)claude( |$)"]]
        if sys.platform == "darwin":
            checks.insert(1, ["pgrep", "-x", "Claude"])
        for cmd in checks:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                found += ["%s (pid %s)" % (cmd[-1], pid) for pid in p.stdout.split() if pid != str(os.getpid())]
            elif p.returncode != 1:
                return None
        return sorted(set(found))
    except (OSError, subprocess.SubprocessError):
        return None


def interactive():
    return bool(sys.stdin) and sys.stdin.isatty()


def guard_problem(path, skip_process_check):
    """Why it is not safe to rewrite the transcript now (None when it is)."""
    age = time.time() - os.path.getmtime(path)
    if age < MIN_IDLE_SECONDS:
        return ("Claude wrote to this session %d s ago: it is probably still open. Close it and wait a minute."
                % age)
    if skip_process_check:
        return None
    procs = running_claude()
    if procs is None:
        raise ToolError("cannot check whether Claude is running (tasklist/pgrep failed). Close Claude yourself, "
                        "then run again with --no-process-check.")
    if procs:
        return ("Claude is still running. Quit Claude Desktop completely (tray / menu bar icon > Quit) "
                "and any `claude` running in a terminal.")
    return None


def wait_until_safe(path, skip_process_check):
    while True:
        problem = guard_problem(path, skip_process_check)
        if problem is None:
            return
        if not interactive():
            raise ToolError(problem)
        P("⚠ " + problem, "yellow")
        if input("  Press Enter when it is done (q to cancel) ").strip().lower().startswith("q"):
            raise ToolError("cancelled, nothing was changed")


# --------------------------------------------------------------------------- backup / atomic write

def atomic_write(target, data, mode_from=None):
    d = os.path.dirname(os.path.abspath(target))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".compact-rewind-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        src = mode_from if mode_from and os.path.exists(mode_from) else (target if os.path.exists(target) else None)
        if src:
            shutil.copymode(src, tmp)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)  # our own temporary file only
        raise


def safe_name(path):
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path)) or "file"


def make_backup(session_id, transcript_path, files, reason, backup_root=None, extra=None):
    """Copy transcript + companion dir + files into a new timestamped folder with a SHA-256 manifest."""
    root = os.path.join(backup_root or default_backup_root(), session_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(root, stamp + ("" if reason == "restore" else "-" + reason))
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(root, "%s-%s-%d" % (stamp, reason, n))
    os.makedirs(dest)
    entries = []

    def add(role, src, rel):
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        h = sha256_file(dst)
        if h != sha256_file(src):
            raise ToolError("backup copy differs from its source: %s" % src)
        entries.append({"role": role, "path": os.path.abspath(src), "backup": rel.replace(os.sep, "/"),
                        "sha256": h, "size": os.path.getsize(dst)})

    add("transcript", transcript_path, os.path.join("transcript", os.path.basename(transcript_path)))
    companion = os.path.splitext(transcript_path)[0]
    if os.path.isdir(companion):
        for dirpath, _, names in os.walk(companion):
            for name in sorted(names):
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, os.path.dirname(companion))
                add("companion", src, os.path.join("transcript", rel))
    for k, f in enumerate(files, 1):
        if os.path.isfile(f):
            add("file", f, os.path.join("files", "%03d-%s" % (k, safe_name(f))))
    manifest = {"tool": "claude-compact-rewind", "version": __version__, "format": 1, "reason": reason,
                "created": dt.datetime.now(dt.timezone.utc).isoformat(), "session_id": session_id,
                "entries": entries}
    manifest.update(extra or {})
    with open(os.path.join(dest, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return dest, manifest


def read_manifest(backup_dir):
    p = os.path.join(backup_dir, "manifest.json")
    if not os.path.isfile(p):
        raise ToolError("not a claude-compact-rewind backup (no manifest.json): %s" % tilde(backup_dir))
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def list_backups(session_id, backup_root=None):
    root = os.path.join(backup_root or default_backup_root(), session_id)
    if not os.path.isdir(root):
        return []
    return sorted((os.path.join(root, d) for d in os.listdir(root)
                   if os.path.isfile(os.path.join(root, d, "manifest.json"))), reverse=True)


# --------------------------------------------------------------------------- sessions

def scan_session(path):
    """Light scan (no full parse) for the session list: cwd, title, compactions."""
    info = {"path": path, "size": os.path.getsize(path), "mtime": os.path.getmtime(path),
            "cwd": None, "title": None, "boundaries": []}
    last_b = None
    with open(path, "rb") as fh:
        for n, line in enumerate(fh):
            if info["cwd"] is None and b'"cwd"' in line:
                try:
                    info["cwd"] = json.loads(line).get("cwd")
                except ValueError:
                    pass
            if b'"custom-title"' in line:
                try:
                    r = json.loads(line)
                    if r.get("type") == "custom-title":
                        info["title"] = r.get("customTitle")
                except ValueError:
                    pass
            if b'"compact_boundary"' in line or (last_b is not None and b'"informational"' in line):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if is_boundary(r):
                    cm = r.get("compactMetadata") or {}
                    last_b = {"line": n + 1, "ts": r.get("timestamp"), "trigger": cm.get("trigger"),
                              "pre": cm.get("preTokens"), "post": cm.get("postTokens"),
                              "precomputed": bool(cm.get("precomputed")), "idle": False}
                    info["boundaries"].append(last_b)
                elif r.get("type") == "system" and r.get("subtype") == "informational" and last_b is not None:
                    txt = str(r.get("content") or "")
                    if "idleCompaction" in txt or "were away" in txt:
                        last_b["idle"] = True
    return info


def compacted_sessions():
    out = []
    for f in session_files():
        try:
            info = scan_session(f)
        except OSError:
            continue
        if info["boundaries"]:
            out.append(info)
    return out


def when(value):
    """'today 16:03', 'yesterday 09:12' or '2030-01-31 16:03' (local time), from an ISO string or an epoch."""
    d = dt.datetime.fromtimestamp(value) if isinstance(value, (int, float)) else parse_ts(value)
    if d is None:
        return "?"
    d = d.astimezone() if d.tzinfo else d
    days = (dt.date.today() - d.date()).days
    return {0: "today", 1: "yesterday"}.get(days, d.strftime("%Y-%m-%d")) + d.strftime(" %H:%M")


def compaction_kind(b):
    if b["trigger"] == "manual":
        return "/compact"
    return "idle" if b["idle"] else "auto"


def session_name(title, cwd, path):
    if title:
        return title
    return os.path.basename(cwd.rstrip("/\\")) if cwd else os.path.splitext(os.path.basename(path))[0][:8]


def print_sessions(infos, verbose=False):
    width = term_width()
    now = time.time()
    for k, info in enumerate(infos, 1):
        b = info["boundaries"][-1]
        busy = "  (in use)" if now - info["mtime"] < MIN_IDLE_SECONDS else ""
        P(clip("%3d  %s%s" % (k, session_name(info["title"], info["cwd"], info["path"]), busy), width), "bold")
        P("     " + clip("compacted %s · %s · %s → %s tokens%s%s" % (
            when(b["ts"]), compaction_kind(b), ktok(b["pre"]), ktok(b["post"]),
            "  (+%d earlier)" % (len(info["boundaries"]) - 1) if len(info["boundaries"]) > 1 else "",
            ("  · " + tilde(info["cwd"])) if info["cwd"] else ""), width - 5), "dim")
        if verbose:
            P("     " + tilde(info["path"]), "dim")
            for j, x in enumerate(info["boundaries"]):
                P("     [%d] line %d  %s  %s  %s → %s%s" % (j, x["line"], local_ts(x["ts"]), x["trigger"],
                                                         ktok(x["pre"]), ktok(x["post"]),
                                                         "  precomputed" if x["precomputed"] else ""), "dim")


def resolve_session(arg):
    if arg is None:
        return pick_session()
    arg = os.path.expanduser(arg)
    if os.path.isfile(arg):
        return os.path.abspath(arg)
    m = re.fullmatch(r"#?(\d{1,4})", arg)
    if m:
        infos = compacted_sessions()
        k = int(m.group(1))
        if not 1 <= k <= len(infos):
            raise ToolError("there is no session %d (the list has %d)" % (k, len(infos)))
        return infos[k - 1]["path"]
    hits = [f for f in session_files() if os.path.basename(f).startswith(arg)]
    if not hits:
        raise ToolError("no session matches %r" % arg)
    if len(hits) > 1:
        raise ToolError("%r matches %d sessions: give more characters" % (arg, len(hits)))
    return hits[0]


def pick_session():
    infos = compacted_sessions()
    if not infos:
        raise ToolError("no compacted session found in %s" % tilde(projects_dir()))
    if not interactive():
        raise ToolError("which session? Give its number from `list`, its id or its file")
    shown = infos[:9]
    P("Compacted sessions, most recent first:", "bold")
    print_sessions(shown)
    if len(infos) > len(shown):
        P("     … %d older ones: `list --all`, then pass the number" % (len(infos) - len(shown)), "dim")
    P()
    while True:
        ans = input(Style.paint("Which session? [1] ", "bold")).strip() or "1"
        if ans.lower().startswith("q"):
            raise ToolError("cancelled, nothing was changed")
        if ans.isdigit() and 1 <= int(ans) <= len(shown):
            P()
            return shown[int(ans) - 1]["path"]
        P("  Type a number from 1 to %d (q to quit)." % len(shown), "yellow")


def ask(question, assume_yes=False):
    if assume_yes:
        return True
    if not interactive():
        raise ToolError("not running in a terminal: add --yes to confirm")
    return input(Style.paint(question + " [y/N] ", "bold")).strip().lower() in ("y", "yes", "o", "oui")


# --------------------------------------------------------------------------- commands

def cmd_list(args):
    infos = compacted_sessions()
    if not infos:
        P("No compacted session in %s" % tilde(projects_dir()))
        return 0
    print_sessions(infos if args.all else infos[:15], args.verbose)
    if not args.all and len(infos) > 15:
        P("     … %d more: list --all" % (len(infos) - 15), "dim")
    P()
    P("To rewind one: python %s <number>" % PROG, "dim")
    return 0


def cmd_show(args):
    t = Transcript(resolve_session(args.session))
    b = choose_boundary(t, args.compaction)
    cut = auto_cut(t, b)
    P('"%s"' % session_name(t.title, t.cwd, t.path), "bold")
    if args.full:
        lo, hi = 0, len(t.recs)
    else:
        lo, hi = max([x for x in t.boundaries if x < b] or [0]), t.next_boundary(b)
    items = build_items(t, lo, hi, show_all=args.all)
    if not args.full:
        bi = next((k for k, it in enumerate(items) if it.first == b), len(items))
        items = items[max(0, bi - args.before):bi + 1 + args.after]
    marks = {}
    point = next((it for it in reversed(items) if cut is not None and it.last < cut), None)
    if point is not None:
        marks[point.last] = "◀ default point"
    page_lines([render_item(t, it, term_width(), marks) for it in items], args.page_size)
    P()
    P("The number on the left is a line. To go back to another point:", "dim")
    P("  python %s rewind %s --at <line>" % (PROG, t.session_id[:8]), "dim")
    return 0


def last_answer(t, cut):
    return next((t.recs[i] for i in range(cut - 1, -1, -1) if t.recs[i] and t.recs[i].get("type") == "assistant"
                 and not t.recs[i].get("isSidechain") and text_of(t.recs[i]).strip()), None)


FILE_GROUPS = (("ok", "can be put back"), ("manual", "need a manual check"),
               ("created", "new (left as they are)"), ("unchanged", "already as they were"))


def files_phrase(actions, sub_edits):
    counts = {}
    for a in actions:
        key = a.status if a.status in ("ok", "created", "unchanged") else "manual"
        counts[key] = counts.get(key, 0) + 1
    known = {norm(a.path) for a in actions}
    extra = {norm(p) for _, p in sub_edits} - known
    if extra:
        counts["manual"] = counts.get("manual", 0) + len(extra)
    return sum(counts.values()), ", ".join("%d %s" % (counts[k], label) for k, label in FILE_GROUPS if k in counts)


def chain_status(sim, mode):
    sound = not sim["orphan_lines"] and not sim["broken_parent"]
    full = sound and sim["root_is_first_record"] and not sim["boundary_in_chain"] and not sim["summary_in_chain"]
    partial = sound and mode == "truncate" and sim["root_is_boundary"]
    return full, partial


HOW = {"idle": "while you were away (idle compaction)", "manual": "with /compact",
       "precomputed": "automatically (summary prepared in advance)", "auto": "automatically (context limit reached)"}


def print_summary(t, plan, actions, sub_edits, sim, full, partial):
    width = term_width()
    b = plan.boundary
    cm = t.compact_meta(b)
    pad = " " * 12
    P('"%s"%s' % (session_name(t.title, t.cwd, t.path), ("  ·  " + tilde(t.cwd)) if t.cwd else ""), "bold")
    P("Compacted %s, %s: %s → %s tokens" % (when(t.recs[b].get("timestamp")), HOW[boundary_cause(t, b)[0]],
                                            ktok(cm.get("preTokens")), ktok(cm.get("postTokens"))))
    if len(t.boundaries) > 1:
        P("(this session was compacted %d times; --compaction picks another one)" % len(t.boundaries), "dim")
    P()
    if plan.mode == "truncate":
        last_ts = next((r.get("timestamp") for r in reversed(t.recs[:plan.cut]) if r and r.get("timestamp")), None)
        P("Go back to  " + ("the end of Claude's answer of %s" % when(last_ts) if plan.at is None
                            else "line %d (%s)" % (plan.cut, when(last_ts))), "bold")
        asst = last_answer(t, plan.cut)
        if asst is not None:
            P(pad + Style.paint(clip(text_of(asst), width - 13, tail=True), "green"))
        n = len(plan.removed_prompts)
        if n:
            P("You lose    %d message%s you sent after that point (saved to a file, to send again):"
              % (n, "" if n == 1 else "s"))
            for i in plan.removed_prompts[:5]:
                r = t.recs[i]
                P(pad + clip("%s  %s" % (when(r.get("timestamp")).split(" ")[-1], text_of(r)), width - 13), "cyan")
            if n > 5:
                P(pad + "… and %d more" % (n - 5), "dim")
        else:
            P("You lose    only what Claude did after that point (none of your messages)")
        if actions or sub_edits:
            total, phrase = files_phrase(actions, sub_edits)
            P("Files       %d changed after that point: %s" % (total, phrase))
    else:
        P("Remove      the compaction only, keep everything after it (both parts joined)", "bold")
    if full:
        P("Result      full conversation, about %s tokens  ✔" % ktok(plan.estimate), "green")
    elif partial:
        P("Result      conversation from the previous compaction (%s) on, about %s tokens  ✔"
          % (when(t.recs[sim["root_line"] - 1].get("timestamp")), ktok(plan.estimate)), "green")
        P(pad + "(--compaction %d goes back before that one too)" % max(0, t.boundaries.index(b) - 1), "dim")
    else:
        P("Result      ✖ the conversation would be incomplete (-v for details)", "red", "bold")
        if plan.mode == "restitch" and sim["boundary_in_chain"]:
            P(pad + "another compaction is in the way: add --compaction all", "dim")
    for n in plan.notes:
        P("⚠ " + n, "yellow")


def print_details(t, plan, actions, sub_edits, sim, d):
    P()
    P("Details", "bold")
    P("  file      %s (%s, %d lines)" % (tilde(t.path), human_size(t.size), len(t.raw)))
    P("  session   %s" % t.session_id)
    if plan.mode == "truncate":
        P("  keeps     lines 1..%d unchanged (an exact prefix of the file), drops %d" % (plan.cut, len(plan.removed)))
    else:
        P("  drops     lines %s; %d line(s) relinked, all others unchanged byte for byte"
          % (", ".join(str(i + 1) for i in plan.removed), len(plan.modified)))
    if plan.removed_prompts:
        P("  removed messages, in full:")
        for i in plan.removed_prompts:
            P("  ----- line %d · %s -----" % (i + 1, local_ts(t.recs[i].get("timestamp"))), "cyan")
            print(text_of(t.recs[i]))
    if actions or sub_edits:
        print_files(actions, sub_edits)
    print_simulation(sim)
    print_diagnostic(d)


def removed_messages_text(t, plan):
    out = ["Messages removed by claude-compact-rewind on %s" % time.strftime("%Y-%m-%d %H:%M"),
           'Session: "%s"' % session_name(t.title, t.cwd, t.path),
           "Send them again in the same order. [image] marks an image to attach again.", ""]
    for k, i in enumerate(plan.removed_prompts, 1):
        out += ["----- %d/%d · %s -----" % (k, len(plan.removed_prompts), local_ts(t.recs[i].get("timestamp"))),
                text_of(t.recs[i]), ""]
    return "\n".join(out)


def cmd_rewind(args):
    t = Transcript(resolve_session(args.session))
    plan = build_plan(t, "restitch" if args.keep_after else "truncate", args.compaction, args.at)
    plan.at = args.at
    actions, sub_edits = [], []
    if plan.mode == "truncate":
        actions = plan_files(t, plan.cut)
        sub_edits = subagent_edits_after(t, (t.recs[plan.cut - 1] or {}).get("timestamp"))
    sim = simulate(plan.lines)
    full, partial = chain_status(sim, plan.mode)
    d = diagnose(t, plan.boundary, plan.estimate)
    print_summary(t, plan, actions, sub_edits, sim, full, partial)
    if d["tips"]:
        P()
        print_tips(d)
    if args.verbose:
        print_details(t, plan, actions, sub_edits, sim, d)
    if not (full or partial) and not args.force:
        raise ToolError("nothing was changed, because the result would be broken (-v shows why)")
    if args.out:  # inspection and tests: write elsewhere, no guard, no backup
        if norm(args.out) == norm(t.path):
            raise ToolError("--out must be another file")
        atomic_write(args.out, b"".join(plan.lines), mode_from=t.path)
        return 0 if _verify_written(args.out, plan, sim) else 3
    P()
    if args.dry_run:
        P("Dry run: nothing was changed.", "cyan")
        return 0
    if not ask("Rewind this session?", args.yes):
        P("Nothing was changed.")
        return 1
    ok_files = [a for a in actions if a.status == "ok" or (args.force_files and a.status == "unverified")]
    do_files = []
    if ok_files:
        if args.files:
            do_files = ok_files
        elif args.yes:
            P("Files left as they are (add --files to put them back too).", "dim")
        else:
            P("  " + ", ".join(os.path.basename(a.path) for a in ok_files[:6]) + (" …" if len(ok_files) > 6 else ""),
              "dim")
            if ask("Also put back these %d file%s as they were at that point?"
                   % (len(ok_files), "" if len(ok_files) == 1 else "s")):
                do_files = ok_files
    wait_until_safe(t.path, args.no_process_check)
    with open(t.path, "rb") as fh:
        if fh.read() != b"".join(t.raw):
            raise ToolError("the session changed in the meantime: nothing was changed, run again")
    dest, _ = make_backup(t.session_id, t.path, [a.path for a in do_files], "restore", args.backup_dir,
                          {"title": session_name(t.title, t.cwd, t.path)})
    saved = None
    if plan.removed_prompts:
        saved = os.path.join(dest, "removed-messages.txt")
        with open(saved, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(removed_messages_text(t, plan))
    P("✔ Backup saved", "green")
    atomic_write(t.path, b"".join(plan.lines), mode_from=t.path)
    good = _verify_written(t.path, plan, sim)
    put_back = 0
    for a in do_files:
        if sha256_file(a.path) != a.current_sha:
            P("⚠ %s changed in the meantime: left as it is" % tilde(a.path), "yellow")
            continue
        with open(a.backup_path, "rb") as fh:
            atomic_write(a.path, fh.read(), mode_from=a.path)
        if sha256_file(a.path) == a.target_sha:
            put_back += 1
        else:
            good = False
            P("✖ %s: content differs after writing (undo restores it)" % tilde(a.path), "red")
    if do_files:
        P("✔ %d file%s put back" % (put_back, "" if put_back == 1 else "s"), "green")
    if saved:
        P()
        P("Your %d removed message%s, to send again:" % (len(plan.removed_prompts),
                                                         "" if len(plan.removed_prompts) == 1 else "s"))
        P("  " + tilde(saved), "cyan")
    P()
    P("Now:", "bold")
    P('  1. Open Claude and the session. If asked, choose "Resume full session as-is".')
    P("  2. " + ("Send your messages again." if saved else "Carry on."))
    P()
    P("Changed your mind?  python %s undo" % PROG, "dim")
    return 0 if good else 3


def _verify_written(path, plan, sim):
    with open(path, "rb") as fh:
        data = fh.read()
    good = data == b"".join(plan.lines) and simulate(data.splitlines(keepends=True)) == sim
    if good:
        P("✔ Session rewound (re-read and checked)", "green")
    else:
        P("✖ The written file differs from what was planned: run `undo`", "red", "bold")
    return good


def all_backups(backup_root=None):
    found = []
    for m in glob.glob(os.path.join(backup_root or default_backup_root(), "*", "*", "manifest.json")):
        try:
            found.append((read_manifest(os.path.dirname(m)), os.path.dirname(m)))
        except (OSError, ValueError, ToolError):
            continue
    return sorted(found, key=lambda x: x[0].get("created", ""), reverse=True)


def pick_backup(args):
    if args.target:
        target = os.path.expanduser(args.target)
        if os.path.isfile(os.path.join(target, "manifest.json")):
            return os.path.abspath(target)
        try:
            sid = os.path.splitext(os.path.basename(resolve_session(target)))[0]
        except ToolError:
            sid = target
        found = [x for x in all_backups(args.backup_dir) if x[0].get("session_id") == sid]
    else:
        found = all_backups(args.backup_dir)
    found = [x for x in found if x[0].get("reason") == "restore"] or found
    if not found:
        raise ToolError("no rewind to undo (backups are in %s)" % tilde(args.backup_dir or default_backup_root()))
    return found[0][1]


def cmd_undo(args):
    if args.list:
        backups = all_backups(args.backup_dir)
        if not backups:
            P("No backup in %s" % tilde(args.backup_dir or default_backup_root()))
        for m, d in backups:
            P('%s  "%s"  %s' % (when(m.get("created")), m.get("title") or m.get("session_id", "?")[:8],
                                "rewind" if m.get("reason") == "restore" else "state before an undo"), "bold")
            P("     " + tilde(d), "dim")
        return 0
    backup = pick_backup(args)
    m = read_manifest(backup)
    bad = [e["backup"] for e in m["entries"] if not os.path.isfile(os.path.join(backup, e["backup"]))
           or sha256_file(os.path.join(backup, e["backup"])) != e["sha256"]]
    if bad:
        raise ToolError("this backup is damaged (checksum mismatch: %s): nothing was changed" % ", ".join(bad[:3]))
    todo = [e for e in m["entries"] if not (os.path.isfile(e["path"]) and sha256_file(e["path"]) == e["sha256"])]
    title = m.get("title") or m.get("session_id", "?")[:8]
    if not todo:
        P('"%s" is already as it was before the rewind of %s. Nothing to do.' % (title, when(m.get("created"))))
        return 0
    nfiles = sum(1 for e in todo if e["role"] == "file")
    P('Undo the rewind of "%s" made %s' % (title, when(m.get("created"))), "bold")
    P("  puts the session%s back exactly as before (backup checked)"
      % (" and %d file%s" % (nfiles, "" if nfiles == 1 else "s") if nfiles else ""))
    if args.verbose:
        for e in todo:
            P("  %-10s %s" % (e["role"], tilde(e["path"])), "dim")
        P("  backup: " + tilde(backup), "dim")
    P()
    if not ask("Undo?", args.yes):
        P("Nothing was changed.")
        return 1
    transcript = next((e["path"] for e in m["entries"] if e["role"] == "transcript"), None)
    if transcript and os.path.isfile(transcript):
        wait_until_safe(transcript, args.no_process_check)
        make_backup(m["session_id"], transcript, [e["path"] for e in m["entries"] if e["role"] == "file"],
                    "pre-undo", args.backup_dir, {"title": title})
    good = True
    for e in todo:
        src = os.path.join(backup, e["backup"])
        with open(src, "rb") as fh:
            atomic_write(e["path"], fh.read(), mode_from=e["path"] if os.path.exists(e["path"]) else src)
        if sha256_file(e["path"]) != e["sha256"]:
            good = False
            P("✖ %s differs after writing" % tilde(e["path"]), "red")
    if good:
        P("✔ Back as before the rewind (checked). The state you just left is kept as a backup too.", "green")
    return 0 if good else 3


# --------------------------------------------------------------------------- CLI

PROG = "claude_compact_rewind.py"
COMMANDS = ("rewind", "list", "show", "undo")


def compaction_arg(value):
    if value == "all":
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number (0 = first compaction, -1 = last) or `all`")


def build_parser():
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Bring a Claude Code session back to how it was before it was compacted. "
                    "Run it without arguments and answer the questions.",
        epilog="Commands: rewind (default), list, show, undo. `%s <command> -h` for their options. "
               "Tested with %s." % (PROG, TESTED_WITH))
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="no colors (also NO_COLOR)")
    sub = ap.add_subparsers(dest="command", metavar="command")
    sess = "session: its number in `list`, its id (or the start of it), or its .jsonl file"

    p = sub.add_parser("rewind", parents=[common], help="rewind a session (default command)")
    p.add_argument("session", nargs="?", help=sess + "; asked when omitted")
    p.add_argument("--at", type=int, metavar="LINE", help="go back to this line instead (see `show`)")
    p.add_argument("--files", action="store_true", help="also put back the files Claude changed, without asking")
    p.add_argument("--dry-run", action="store_true", help="only show what would happen")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-v", "--verbose", action="store_true", help="show every detail")
    adv = p.add_argument_group("advanced")
    adv.add_argument("--keep-after", action="store_true",
                     help="keep what happened after the compaction too (removes only the summary)")
    adv.add_argument("--compaction", type=compaction_arg, metavar="N",
                     help="which compaction (0 = first, -1 = last; default: last automatic); `all` with --keep-after")
    adv.add_argument("--force-files", action="store_true", help="also put back files changed outside Claude")
    adv.add_argument("--force", action="store_true", help="write even if the check finds a broken result")
    adv.add_argument("--no-process-check", action="store_true", help="do not check that Claude is closed")
    adv.add_argument("--backup-dir", metavar="DIR", help="where backups go (default ~/.claude/compact-rewind-backups)")
    adv.add_argument("--out", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_rewind)

    p = sub.add_parser("list", parents=[common], help="compacted sessions, most recent first")
    p.add_argument("--all", action="store_true", help="all of them (default: 15)")
    p.add_argument("-v", "--verbose", action="store_true", help="files and every compaction")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", parents=[common], help="the messages around the compaction, one per line")
    p.add_argument("session", nargs="?", help=sess)
    p.add_argument("--compaction", type=int, metavar="N", help="which compaction (default: last automatic)")
    p.add_argument("--before", type=int, default=30, help="messages before the compaction (default 30)")
    p.add_argument("--after", type=int, default=10, help="messages after it (default 10)")
    p.add_argument("--full", action="store_true", help="the whole session")
    p.add_argument("--all", action="store_true", help="also tool results, attachments and system records")
    p.add_argument("--page-size", type=int, default=max(10, shutil.get_terminal_size((100, 24)).lines - 2),
                   help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("undo", parents=[common], help="undo the last rewind")
    p.add_argument("target", nargs="?", help="a session (its last rewind) or a backup folder; default: the last rewind")
    p.add_argument("--list", action="store_true", help="list the backups")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-process-check", action="store_true", help="do not check that Claude is closed")
    p.add_argument("--backup-dir", metavar="DIR", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_undo)
    return ap


def main(argv=None):
    setup_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "rewind")  # `tool`, `tool 3` and `tool --dry-run` all mean rewind
    args = build_parser().parse_args(argv)
    Style.setup(args.no_color)
    try:
        return args.func(args)
    except ToolError as e:
        msg = str(e)
        P("✖ " + msg[:1].upper() + msg[1:], "red", "bold")
        return 2
    except (KeyboardInterrupt, EOFError):
        P("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
