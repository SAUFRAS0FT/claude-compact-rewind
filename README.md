# claude-compact-rewind

Claude Code compacted your session and you lost its context? This tool puts the session back
**as it was just before the compaction**: same session, still in the Claude Desktop sidebar,
with the full conversation.

One Python file, nothing to install, Windows / macOS / Linux.

## Quick start

1. **Quit Claude completely** (tray / menu bar icon › Quit, not just the window).
2. Run:

   ```bash
   python claude_compact_rewind.py
   ```

   Pick the session, read the summary, answer `y`.
3. Reopen Claude and the session. If it asks, choose **Resume full session as-is**.
   Send again the messages listed by the tool (they are saved in a text file).

Changed your mind?

```bash
python claude_compact_rewind.py undo
```

What it looks like:

```text
"Refactor the parser"  ·  ~/projects/demo
Compacted today 16:03, while you were away (idle compaction): 296k → 27k tokens

Go back to  the end of Claude's answer of today 14:42
            …12 tests added, all passing. Shall I update the README too?
You lose    2 messages you sent after that point (saved to a file, to send again):
            16:03  Yes, update the README and add a usage section
            16:20  Also mention the new --strict flag
Files       1 changed after that point: 1 can be put back
Result      full conversation, about 294k tokens  ✔

⚠ Idle compaction is on, so this can happen again. To turn it off, add
  "idleCompaction": false to ~/.claude/settings.json

Rewind this session? [y/N]
```

Nothing is changed before you answer `y`, and a backup is made first.

## The problem

Claude Code **compacts** long sessions: it replaces the conversation with a short summary. That frees
room, but Claude forgets the details you built up together. It can happen when you did not expect it:

- **Idle compaction.** If a big session (over 200K tokens by default) stays idle for about an hour,
  Claude Code writes a summary in the background and applies it to your **next** message. You see
  *"Compacted using a summary written while you were away"*. This ignores your auto-compact limit.
- **The auto-compact limit was not the one you thought.** An update, another settings file or an
  environment variable can change it, and the session compacts earlier than planned.

`/rewind` in Claude cannot go back across a compaction. But the old messages are still in the session
file on disk: this tool makes Claude load them again.

To avoid it next time, add to `~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on
Windows):

```json
{
  "idleCompaction": false
}
```

and set your limit with `/autocompact` inside Claude. The tool tells you which setting caused the
compaction.

## Detailed guide

### Commands

| Command | What it does |
|---|---|
| `python claude_compact_rewind.py` | guided rewind: choose a session, see the summary, confirm |
| `python claude_compact_rewind.py 2` | rewind session number 2 of the list |
| `python claude_compact_rewind.py list` | compacted sessions, most recent first |
| `python claude_compact_rewind.py show 2` | the messages around the compaction, one per line |
| `python claude_compact_rewind.py undo` | undo the last rewind |

A session can be given by its number in `list`, its id (or the start of it), or its `.jsonl` file.
Use `python3` on macOS / Linux. `--no-color` (or `NO_COLOR=1`) turns colors off.

Options of the rewind:

| Option | Effect |
|---|---|
| `--dry-run` | only show the summary |
| `-v` | every detail: lines kept, full text of the removed messages, file by file, check, settings found |
| `--at LINE` | go back to another point (line numbers come from `show`) |
| `--files` | also put back the changed files, without asking |
| `-y` | do not ask (for scripts) |
| `--keep-after` | keep what happened after the compaction too (see below) |
| `--compaction N` | another compaction of the same session (`0` = the first one) |

### What "go back" means

By default the tool goes back to **the end of the last complete answer from Claude before the
compaction**. Everything after that point is removed from the session: your later messages (listed and
saved to `removed-messages.txt` in the backup folder) and what Claude did meanwhile.

To pick another point, look at the session with `show`:

```text
L13     14:42:11 ASSIST  …12 tests added, all passing. Shall I update the README too? ◀ default point
L15     16:03:02 USER    Yes, update the README and add a usage section
L17     16:03:02 ════════════ compaction (idle) 296k → 27k tokens ════════════
```

then `python claude_compact_rewind.py rewind <session> --at 13`. The number on the left is a line of
the session file.

### Changed files

If Claude edited files after that point, the tool can put them back as they were, using the copies
Claude Code keeps itself (`~/.claude/file-history/`). A file is put back only if it is exactly what
Claude's edits made of it; if you or another program changed it since, it is reported as
"need a manual check" and left alone. Files created after that point are never deleted. Changes made
through shell commands are not tracked by Claude Code, so the tool cannot see them. `-v` shows every
file and why.

### Safety

- Nothing is written before you confirm. `--dry-run` only shows.
- It refuses to write while Claude is running (it waits for you to quit it), or if Claude wrote to the
  session less than a minute ago.
- Before writing, it backs up the session file, its folder and the files it will put back to
  `~/.claude/compact-rewind-backups/`, with checksums.
- The new session file is written atomically, then re-read and checked.
- By default the new file is the **beginning of the old one, unchanged**: an exact state that Claude
  Code had itself written to disk. No line is rewritten.
- `undo` checks the backup, saves the current state as another backup, then puts everything back.
  It never deletes anything.

Undo if, after the rewind, you get an error 400 mentioning `thinking` or `signature`, "prompt too long",
an empty or missing session, or a new compaction right away.

### Keeping what happened after the compaction

`--keep-after` does not cut anything: it only removes the compaction and its summary and joins the two
parts. You keep the work done after the compaction, but the context is then old part + new part, which
can be too large for the model (the tool warns you). A few lines are rewritten to join the parts; all
the others are copied byte for byte. With several compactions, add `--compaction all`.

### How it works

A session is a file `~/.claude/projects/<project>/<session-id>.jsonl` (`CLAUDE_CONFIG_DIR` is
respected), one JSON record per line, each linked to the previous one by `parentUuid`. A compaction
appends a `compact_boundary` record that starts a new chain, then the summary. When it opens a session,
Claude Code takes the most recent record and follows the links back, stopping at the last compaction.

The tool cuts the file just after the chosen point, so that no compaction remains, and checks the
result by simulating that loading: one chain from the first message to the chosen one, no compaction,
no broken link. Records are never re-encoded unless `--keep-after` needs it: Claude's `thinking`
blocks carry a signature, and any change makes the API reject the conversation.

### Compaction settings

| Setting | Effect |
|---|---|
| `"idleCompaction": false` | no more idle compaction |
| `autoCompactWindow` (100000 to 1000000, or `"auto"`) | auto-compact limit, in tokens |
| `modelSettings.<model>.autoCompactWindow` | same for one model; this is what `/autocompact` writes, and it wins over the line above |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | environment variable; wins over both |
| `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` | size above which idle compaction applies (default 200000) |

The tool reads `~/.claude/settings.json`, the project's `.claude/settings.json` and
`.claude/settings.local.json`, and the managed settings. It never changes them. Note that Claude
Desktop may not see the same environment variables as your terminal.

### Limitations

- The session file format is internal to Claude Code and undocumented. The tool was written for
  **Claude Code 2.1.308** and tested on Windows. Another version may differ: read the summary, and keep
  the backup until the session works.
- macOS and Linux are covered by automated tests only, not yet by a real Claude session.
- Detecting Claude: `claude.exe` on Windows, `claude` / `Claude` on macOS and Linux. A Claude Code
  started through `node` on Windows is not detected: quit it yourself.
- If the old messages are no longer in the file, nothing can be brought back; the tool says so.
- Run it from a normal terminal: from inside Claude Code, Claude is running by definition.
- Not affiliated with Anthropic.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests only use generated, fake sessions.

## License

MIT, see [LICENSE](LICENSE).
