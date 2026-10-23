"""Tests on 100 % synthetic transcripts. Run: python -m unittest discover -s tests -v"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import claude_compact_rewind as ccr  # noqa: E402

REAL_RUNNING_CLAUDE = ccr.running_claude  # setUp replaces it with a stub
SID = "11111111-2222-4333-8444-555555555555"
SIG = "c3ludGhldGljLXNpZ25hdHVyZQ+/=="  # opaque thinking signature: must survive byte for byte


class Builder:
    """Builds a synthetic Claude Code transcript, one record per line."""

    def __init__(self, project):
        self.project = project
        self.recs = []
        self.tick = 0
        self.last = None  # uuid of the last chain record
        self.n = 0

    def ts(self):
        self.tick += 1
        return "2030-01-01T10:%02d:%02d.000Z" % (self.tick // 60, self.tick % 60)

    def uid(self, prefix):
        self.n += 1
        return "%s-%04d" % (prefix, self.n)

    def chain(self, rec, parent="__last__"):
        rec.setdefault("uuid", self.uid(rec["type"][:4]))
        rec["parentUuid"] = self.last if parent == "__last__" else parent
        rec.setdefault("isSidechain", False)
        rec["timestamp"] = self.ts()
        rec.setdefault("cwd", self.project)
        rec["sessionId"] = SID
        self.recs.append(rec)
        self.last = rec["uuid"]
        return rec

    def prompt(self, text, uuid=None):
        r = self.chain({"type": "user", "message": {"role": "user", "content": text}, **({"uuid": uuid} if uuid else {})})
        self.recs.append({"type": "last-prompt", "lastPrompt": text[:50], "leafUuid": r["uuid"], "sessionId": SID})
        return r

    def snapshot(self, prompt_uuid, tracked):
        self.recs.append({"type": "file-history-snapshot", "messageId": prompt_uuid, "isSnapshotUpdate": False,
                          "snapshot": {"messageId": prompt_uuid, "timestamp": self.ts(), "trackedFileBackups": {
                              k: {"backupFileName": v, "version": int(v.split("@v")[1]) if v else 1,
                                  "backupTime": self.ts()} for k, v in tracked.items()}}})

    def assistant(self, text=None, tools=(), stop="end_turn", thinking=True, mid=None):
        mid = mid or self.uid("msg")
        out = []
        if thinking:
            out.append(self.chain({"type": "assistant", "message": {
                "id": mid, "role": "assistant", "model": "claude-test-model", "stop_reason": stop,
                "content": [{"type": "thinking", "thinking": "", "signature": SIG}],
                "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000 * self.tick, "output_tokens": 20}}}))
        if text:
            out.append(self.chain({"type": "assistant", "message": {
                "id": mid, "role": "assistant", "model": "claude-test-model", "stop_reason": stop,
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000 * self.tick, "output_tokens": 20}}}))
        for name, inp in tools:
            tid = self.uid("toolu")
            out.append(self.chain({"type": "assistant", "message": {
                "id": mid, "role": "assistant", "model": "claude-test-model", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000 * self.tick, "output_tokens": 20}}}))
            self.chain({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
        if stop == "end_turn":
            owner = out[-1]["uuid"]
            self.chain({"type": "system", "subtype": "stop_hook_summary", "hookCount": 0}, parent=owner)
            self.last = self.recs[-1]["uuid"]
        return out

    def delta(self, assistant_uuid, snap_uuid, rel, backup):
        self.recs.append({"type": "file-history-delta", "messageId": assistant_uuid, "snapshotMessageId": snap_uuid,
                          "trackingPath": rel, "timestamp": self.ts(),
                          "backup": {"backupFileName": backup, "version": 1, "backupTime": self.ts()}})

    def title(self, text):
        self.recs.append({"type": "custom-title", "customTitle": text, "sessionId": SID})

    def compact(self, trigger="auto", pre=150000, post=9000, idle=False, logical=None, preserved=None):
        b = {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
             "logicalParentUuid": logical or self.last, "level": "info",
             "compactMetadata": {"trigger": trigger, "preTokens": pre, "postTokens": post}}
        if idle:
            b["compactMetadata"]["precomputed"] = True
        uuid = self.uid("bnd")
        if preserved:
            b["compactMetadata"]["preservedSegment"] = {"headUuid": preserved[0], "anchorUuid": "anchor-" + uuid,
                                                        "tailUuid": preserved[-1]}
            b["compactMetadata"]["preservedMessages"] = {"anchorUuid": "anchor-" + uuid, "uuids": list(preserved)}
        b["uuid"] = uuid
        self.chain(b, parent=None)
        self.chain({"type": "user", "isCompactSummary": True, "uuid": "anchor-" + uuid if preserved else None,
                    "message": {"role": "user", "content": "This session is being continued from a previous "
                                                           "conversation. Summary: synthetic."}})
        if not preserved:
            self.recs[-1]["uuid"] = self.uid("sum")
            self.last = self.recs[-1]["uuid"]
        self.chain({"type": "attachment", "attachment": {"type": "date"}})
        if idle:
            self.chain({"type": "system", "subtype": "informational", "level": "info",
                        "content": "Compacted using a summary written while you were away · turn off with "
                                   "\"idleCompaction\": false in settings.json"})

    def write(self, path):
        # CRLF + spaced separators: a re-serialized line can never be byte-identical by accident
        with open(path, "wb") as fh:
            for r in self.recs:
                fh.write(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\r\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccr-test-")
        self.cfg = os.path.join(self.tmp, "claude-config")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(self.project)
        self.pdir = os.path.join(self.cfg, "projects", "synthetic-project")
        os.makedirs(self.pdir)
        self.path = os.path.join(self.pdir, SID + ".jsonl")
        patches = [mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg, "COLUMNS": "100"}),
                   mock.patch.object(ccr, "managed_settings_path",
                                     return_value=os.path.join(self.tmp, "no-managed.json")),
                   mock.patch.object(ccr, "running_claude", return_value=[]),
                   mock.patch.object(ccr, "interactive", return_value=False)]  # same result in any terminal
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for k in ccr.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)  # test temp dir only

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ccr.main(list(argv))
        return code, buf.getvalue()

    def save(self, b, age=3600):
        b.write(self.path)
        old = time.time() - age
        os.utime(self.path, (old, old))
        with open(self.path, "rb") as fh:
            return fh.read().splitlines(keepends=True)

    def lines(self, path=None):
        with open(path or self.path, "rb") as fh:
            return fh.read().splitlines(keepends=True)

    # -- fixtures -----------------------------------------------------------------------------
    def simple(self):
        b = Builder(self.project)
        b.prompt("first question ✓")
        b.assistant("first answer")
        b.title("Synthetic title")
        b.prompt("second question")
        b.assistant("second answer, end of the last complete turn")
        b.prompt("third question sent after the idle period")
        b.compact(idle=True)
        b.assistant("answer written after compaction")
        b.title("Synthetic title (renamed)")
        return b

    def double(self):
        b = self.simple()
        b.prompt("fourth question")
        b.assistant("fourth answer")
        b.compact(trigger="auto", pre=170000, post=8000)
        b.prompt("fifth question")
        b.assistant("fifth answer")
        return b


class TruncateTests(Base):
    def test_auto_point_is_a_strict_prefix_ending_on_last_complete_turn(self):
        src = self.save(self.simple())
        out = os.path.join(self.tmp, "out.jsonl")
        code, log = self.run_cli("rewind", SID, "--out", out)
        self.assertEqual(code, 0, log)
        res = self.lines(out)
        self.assertEqual(res, src[:len(res)])
        last = json.loads(res[-1])
        self.assertEqual(last.get("subtype"), "stop_hook_summary")
        sim = ccr.simulate(res)
        self.assertTrue(sim["root_is_first_record"])
        self.assertFalse(sim["boundary_in_chain"] or sim["summary_in_chain"])
        self.assertEqual(sim["chain_prompts"], 2)
        self.assertIn("second answer", sim["last_assistant_text_end"])
        self.assertIn("third question sent after the idle period", log)  # removed prompt shown in full
        self.assertIn("idle compaction", log)

    def test_dry_run_writes_nothing(self):
        src = self.save(self.simple())
        code, log = self.run_cli("rewind", SID, "--dry-run")
        self.assertEqual(code, 0, log)
        self.assertEqual(self.lines(), src)
        self.assertIn("Dry run", log)
        self.assertEqual(os.listdir(self.pdir), [SID + ".jsonl"])

    def test_at_line(self):
        b = self.simple()
        src = self.save(b)
        first_answer = next(i for i, l in enumerate(src) if b"first answer" in l)
        out = os.path.join(self.tmp, "cut.jsonl")
        code, log = self.run_cli("rewind", SID, "--at", str(first_answer + 1), "--out", out)
        self.assertEqual(code, 0, log)
        res = self.lines(out)
        self.assertEqual(res, src[:first_answer + 1])
        self.assertEqual(ccr.simulate(res)["chain_prompts"], 1)

    def test_cut_point_after_boundary_is_refused(self):
        src = self.save(self.simple())
        after = len(src) - 1
        code, log = self.run_cli("rewind", SID, "--at", str(after), "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("after the compaction", log)


    def test_two_compactions_default_is_last_auto(self):
        b = self.double()
        src = self.save(b)
        out = os.path.join(self.tmp, "out.jsonl")
        code, log = self.run_cli("rewind", SID, "--out", out)
        self.assertEqual(code, 0, log)
        res = self.lines(out)
        self.assertEqual(res, src[:len(res)])
        sim = ccr.simulate(res)
        self.assertEqual(sim["boundaries_left"], 1)  # the first compaction is kept: chain starts at its summary
        self.assertTrue(sim["boundary_in_chain"])
        self.assertIn("fourth answer", sim["last_assistant_text_end"])
        code, log = self.run_cli("rewind", SID, "--compaction", "0", "--out", out)
        sim = ccr.simulate(self.lines(out))
        self.assertTrue(code == 0 and sim["root_is_first_record"] and sim["boundaries_left"] == 0, log)


    def test_pruned_pre_compaction_content_is_reported(self):
        b = Builder(self.project)
        b.compact(logical="missing-uuid")
        b.prompt("only post-compaction content")
        b.assistant("answer")
        self.save(b)
        code, log = self.run_cli("rewind", SID, "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("no longer in the file", log)


class RestitchTests(Base):
    def check_restitched(self, src, res, prompts):
        sim = ccr.simulate(res)
        self.assertTrue(sim["root_is_first_record"], sim)
        self.assertFalse(sim["boundary_in_chain"] or sim["summary_in_chain"], sim)
        self.assertEqual(sim["orphan_lines"], [])
        self.assertEqual(sim["chain_prompts"], prompts)
        original = set(src)
        changed = [l for l in res if l not in original]
        for l in res:
            r = json.loads(l)
            for blk in (r.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "thinking":
                    self.assertEqual(blk["signature"], SIG)
                    self.assertEqual(blk["thinking"], "")
        for l in changed:
            self.assertTrue(l.endswith(b"\r\n"))
            self.assertNotIn(b'": ', l)  # compact separators
        return changed

    def test_simple(self):
        src = self.save(self.simple())
        out = os.path.join(self.tmp, "out.jsonl")
        code, log = self.run_cli("rewind", SID, "--keep-after", "--out", out)
        self.assertEqual(code, 0, log)
        res = self.lines(out)
        self.assertEqual(len(res), len(src) - 2)
        self.assertEqual(len(self.check_restitched(src, res, prompts=3)), 1)

    def test_two_compactions_all(self):
        src = self.save(self.double())
        out = os.path.join(self.tmp, "out.jsonl")
        code, log = self.run_cli("rewind", SID, "--keep-after", "--out", out)
        self.assertEqual(code, 2, log)  # the other compaction still cuts the chain: refused
        self.assertFalse(os.path.exists(out))
        self.assertIn("--compaction all", log)
        code, log = self.run_cli("rewind", SID, "--keep-after", "--compaction", "all", "--out", out)
        self.assertEqual(code, 0, log)
        res = self.lines(out)
        self.assertEqual(len(res), len(src) - 4)
        self.assertEqual(len(self.check_restitched(src, res, prompts=5)), 2)

    def test_missing_logical_parent_falls_back_to_last_chain_record(self):
        b = self.simple()
        for r in b.recs:
            if r.get("subtype") == "compact_boundary":
                r["logicalParentUuid"] = "uuid-not-in-file"
        src = self.save(b)
        out = os.path.join(self.tmp, "out.jsonl")
        code, log = self.run_cli("rewind", SID, "--keep-after", "--out", out)
        self.assertEqual(code, 0, log)
        self.assertIn("relinked to L", log)
        self.check_restitched(src, self.lines(out), prompts=3)

    def test_preserved_segment(self):
        b = Builder(self.project)
        b.prompt("q1")
        b.assistant("a1")
        b.prompt("q2")
        kept = [r["uuid"] for r in b.assistant("a2, preserved verbatim")]
        kept.append(b.last)
        b.compact(preserved=kept)
        b.prompt("q3")
        b.assistant("a3")
        src = self.save(b)
        out = os.path.join(self.tmp, "out.jsonl")
        code, log = self.run_cli("rewind", SID, "--keep-after", "--out", out)
        self.assertEqual(code, 0, log)
        self.check_restitched(src, self.lines(out), prompts=3)
        code, log = self.run_cli("rewind", SID, "--out", out)  # truncate
        self.assertEqual(code, 0, log)
        self.assertIn("a2, preserved verbatim", ccr.simulate(self.lines(out))["last_assistant_text_end"])

    def test_truncate_only_option_rejected_for_all(self):
        self.save(self.double())
        code, log = self.run_cli("rewind", SID, "--compaction", "all", "--dry-run")
        self.assertEqual(code, 2)


class FileRestoreTests(Base):
    def fixture(self, cut_mid_turn=False):
        notes = os.path.join(self.project, "docs", "notes.md")
        new = os.path.join(self.project, "new.md")
        os.makedirs(os.path.dirname(notes))
        hist = os.path.join(self.cfg, "file-history", SID)
        os.makedirs(hist)
        for name, data in (("aaaa@v2", b"one\n"), ("aaaa@v3", b"two\n")):
            with open(os.path.join(hist, name), "wb") as fh:
                fh.write(data)
        b = Builder(self.project)
        p = b.prompt("create the notes")
        b.snapshot(p["uuid"], {})
        w = b.assistant(None, tools=[("Write", {"file_path": notes, "content": "one\n"})])
        b.delta(w[-1]["uuid"], p["uuid"], os.path.join("docs", "notes.md"), None)
        b.assistant("created")
        p = b.prompt("change one to two")
        b.snapshot(p["uuid"], {os.path.join("docs", "notes.md"): "aaaa@v2"})
        b.assistant(None, tools=[("Edit", {"file_path": notes, "old_string": "one", "new_string": "two"})])
        b.assistant("done: two")
        p = b.prompt("compacted turn")
        b.compact(idle=True)
        b.snapshot(p["uuid"], {os.path.join("docs", "notes.md"): "aaaa@v3"})
        w = b.assistant(None, tools=[("Edit", {"file_path": notes, "old_string": "two", "new_string": "three"}),
                                     ("Write", {"file_path": new, "content": "fresh\n"})])
        b.delta(w[-1]["uuid"], p["uuid"], "new.md", None)
        b.assistant("rewrote the notes after compaction")
        with open(notes, "wb") as fh:
            fh.write(b"three\n")
        with open(new, "wb") as fh:
            fh.write(b"fresh\n")
        return b, notes, new

    def test_plan_statuses(self):
        b, notes, new = self.fixture()
        self.save(b)
        t = ccr.Transcript(self.path)
        plan = ccr.build_plan(t, "truncate")
        actions = {os.path.basename(a.path): a for a in ccr.plan_files(t, plan.cut)}
        self.assertEqual(actions["notes.md"].status, "ok", actions["notes.md"].detail)
        self.assertEqual(actions["notes.md"].backup_name, "aaaa@v3")
        self.assertEqual(actions["new.md"].status, "created")

    def test_mid_turn_point_uses_turn_snapshot(self):
        b, notes, new = self.fixture()
        src = self.save(b)
        t = ccr.Transcript(self.path)
        second = next(i for i, l in enumerate(src) if b"change one to two" in l and b'"type": "user"' in l)
        plan = ccr.build_plan(t, "truncate", at_line=second + 1)
        a = {os.path.basename(a.path): a for a in ccr.plan_files(t, plan.cut)}["notes.md"]
        self.assertEqual((a.backup_name, a.status), ("aaaa@v2", "ok"), a.detail)

    def test_external_change_is_unverified_and_skipped(self):
        b, notes, new = self.fixture()
        self.save(b)
        with open(notes, "ab") as fh:
            fh.write(b"edited by hand\n")
        code, log = self.run_cli("rewind", SID, "--yes", "--files")
        self.assertEqual(code, 0, log)
        self.assertIn("1 need a manual check", log)
        with open(notes, "rb") as fh:
            self.assertEqual(fh.read(), b"three\nedited by hand\n")

    def test_apply_restores_files_then_rollback(self):
        b, notes, new = self.fixture()
        src = self.save(b)
        code, log = self.run_cli("rewind", SID, "--yes", "--files")
        self.assertEqual(code, 0, log)
        self.assertIn("1 file put back", log)
        self.assertIn("removed-messages.txt", log)
        with open(notes, "rb") as fh:
            self.assertEqual(fh.read(), b"two\n")
        with open(new, "rb") as fh:
            self.assertEqual(fh.read(), b"fresh\n")  # created later: never deleted
        res = self.lines()
        self.assertEqual(res, src[:len(res)])
        backups = ccr.list_backups(SID)
        self.assertEqual(len(backups), 1)
        roles = sorted(e["role"] for e in ccr.read_manifest(backups[0])["entries"])
        self.assertEqual(roles, ["file", "transcript"])
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        code, log = self.run_cli("undo", "--yes")
        self.assertEqual(code, 0, log)
        self.assertEqual(self.lines(), src)
        with open(notes, "rb") as fh:
            self.assertEqual(fh.read(), b"three\n")
        self.assertEqual(len(ccr.list_backups(SID)), 2)  # + the state left by the undo


class GuidedTests(Base):
    def test_no_argument_asks_and_rewinds(self):
        src = self.save(self.simple())
        answers = iter(["", "y"])  # session [1], then "Rewind this session?"
        with mock.patch.object(ccr, "interactive", return_value=True),                 mock.patch("builtins.input", lambda prompt="": next(answers)):
            code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        res = self.lines()
        self.assertLess(len(res), len(src))
        self.assertEqual(res, src[:len(res)])
        self.assertIn("Resume full session as-is", log)

    def test_waits_until_claude_is_closed(self):
        self.save(self.simple())
        running = iter([["claude.exe (pid 1)"], []])
        answers = iter(["y", ""])  # confirm, then Enter once Claude is closed
        with mock.patch.object(ccr, "interactive", return_value=True),                 mock.patch.object(ccr, "running_claude", lambda: next(running)),                 mock.patch("builtins.input", lambda prompt="": next(answers)):
            code, log = self.run_cli(SID)
        self.assertEqual(code, 0, log)
        self.assertIn("still running", log)


class GuardTests(Base):
    def test_refuses_when_claude_runs(self):
        src = self.save(self.simple())
        with mock.patch.object(ccr, "running_claude", return_value=["claude.exe (pid 42)"]):
            code, log = self.run_cli("rewind", SID, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("still running", log)
        self.assertEqual(self.lines(), src)
        self.assertEqual(ccr.list_backups(SID), [])

    def test_refuses_when_detection_fails(self):
        src = self.save(self.simple())
        with mock.patch.object(ccr, "running_claude", return_value=None):
            code, log = self.run_cli("rewind", SID, "--yes")
        self.assertEqual((code, self.lines()), (2, src))
        code, log = self.run_cli("rewind", SID, "--yes", "--no-process-check")
        self.assertEqual(code, 0, log)

    def test_refuses_recently_modified_file(self):
        src = self.save(self.simple(), age=5)
        code, log = self.run_cli("rewind", SID, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("wrote to this session", log)
        self.assertEqual(self.lines(), src)

    def test_non_interactive_apply_needs_yes(self):
        src = self.save(self.simple())
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            code, log = self.run_cli("rewind", SID)
        self.assertEqual((code, self.lines()), (2, src))

    def test_apply_in_place_keeps_backup_and_rollback_detects_corruption(self):
        src = self.save(self.simple())
        code, log = self.run_cli("rewind", SID, "--yes")
        self.assertEqual(code, 0, log)
        backup = ccr.list_backups(SID)[0]
        with open(os.path.join(backup, "transcript", SID + ".jsonl"), "rb") as fh:
            self.assertEqual(fh.read(), b"".join(src))
        with open(os.path.join(backup, "transcript", SID + ".jsonl"), "ab") as fh:
            fh.write(b"tampered\n")
        code, log = self.run_cli("undo", backup, "--yes")
        self.assertEqual(code, 2)
        self.assertIn("damaged", log)

    def test_incomplete_chain_is_refused_in_place(self):
        src = self.save(self.double())
        code, log = self.run_cli("rewind", SID, "--keep-after", "--compaction", "0", "--yes")
        self.assertEqual((code, self.lines()), (2, src), log)
        self.assertIn("incomplete", log)

    def test_truncate_keeping_an_earlier_compaction_is_accepted(self):
        src = self.save(self.double())
        code, log = self.run_cli("rewind", SID, "--yes")
        self.assertEqual(code, 0, log)
        self.assertIn("from the previous compaction", log)
        res = self.lines()
        self.assertEqual(res, src[:len(res)])

    def test_windows_process_parsing(self):
        out = ('"claude-helper.exe","10","Console","1","5 000 K"\n'
               '"claude.exe","4242","Console","1","90 000 K"\n')
        fake = mock.Mock(stdout=out, returncode=0)
        with mock.patch.object(ccr.os, "name", "nt"), mock.patch.object(ccr.subprocess, "run", return_value=fake):
            self.assertEqual(REAL_RUNNING_CLAUDE(), ["claude.exe (pid 4242)"])

    def test_posix_process_parsing(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(stdout="777\n" if cmd[-1] == "claude" else "", returncode=0 if cmd[-1] == "claude" else 1)
        with mock.patch.object(ccr.os, "name", "posix"), mock.patch.object(ccr.sys, "platform", "darwin"), \
                mock.patch.object(ccr.subprocess, "run", side_effect=fake):
            self.assertEqual(REAL_RUNNING_CLAUDE(), ["claude (pid 777)"])
        self.assertIn(["pgrep", "-x", "Claude"], calls)


class DisplayTests(Base):
    def test_list_shows_only_compacted_main_sessions(self):
        self.save(self.simple())
        Builder(self.project).write(os.path.join(self.pdir, "22222222-2222-4222-8222-222222222222.jsonl"))
        shutil.copy(self.path, os.path.join(self.pdir, "agent-abc.jsonl"))
        code, log = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(log.count("compacted "), 1)  # not the agent copy, not the uncompacted session
        self.assertIn("Synthetic title (renamed)", log)
        self.assertIn("idle", log)

    def test_show_is_readable(self):
        self.save(self.simple())
        code, log = self.run_cli("show", "#1")
        self.assertEqual(code, 0, log)
        self.assertNotIn("\x1b[", log)
        self.assertNotIn('{"', log)
        self.assertIn("default point", log)
        timeline = [l for l in log.splitlines() if l[:1] == "L" and l[1:2].isdigit()]
        self.assertGreater(len(timeline), 5)
        for line in timeline:
            self.assertLessEqual(len(line), 100, line)

    def test_tool_summary(self):
        self.assertEqual(ccr.tool_summary({"name": "Edit", "input": {"file_path": "/x/y/z.cs"}}), "Edit z.cs")
        self.assertEqual(ccr.tool_summary({"name": "mcp__srv__do_it", "input": {}}), "do_it")


class SettingsTests(Base):
    def test_model_settings_and_env_precedence(self):
        with open(os.path.join(self.cfg, "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"autoCompactWindow": 300000, "idleCompaction": False,
                       "modelSettings": {"claude-test-model": {"autoCompactWindow": 600000}}}, fh)
        found = ccr.compaction_settings(self.project, "claude-test-model")
        window, src, idle, _, _ = ccr.effective(found, "claude-test-model")
        self.assertEqual((window, idle), (600000, False))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "250000"}):
            window, src, _, _, _ = ccr.effective(ccr.compaction_settings(self.project, "m"), "m")
        self.assertEqual(window, 250000)

    def test_model_spellings(self):
        for spelling in ("claude-test-model-20300101", "claude-test-model[1m]", "us.anthropic.claude-test-model-v1:0",
                         "claude-test-model@20300101"):
            self.assertTrue(ccr._model_key_matches("claude-test-model", spelling), spelling)
        self.assertFalse(ccr._model_key_matches("claude-test", "claude-test-model"))

    def test_diagnostic_suggests_without_writing(self):
        self.save(self.simple())
        code, log = self.run_cli("rewind", SID, "--dry-run")
        self.assertIn('"idleCompaction": false', log)
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "settings.json")))


if __name__ == "__main__":
    unittest.main()
