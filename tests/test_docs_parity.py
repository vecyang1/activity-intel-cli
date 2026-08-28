"""Every CLI command printed in the docs must actually parse.

This exists because a README shipped here naming `search "<q>" --json` with no
city argument — a command that exits 2 the moment anyone runs it. Documentation
is the one interface nothing asserts on, and the decidable slice of it is small
and worth pinning: *if a doc names a command, that command must parse.*

Wording, ordering and how much to explain stay judgement. This test only asks
whether argparse accepts the invocation.

It reports the number of commands it graded, so a selector that silently
narrows later — a renamed doc, a moved directory — shows up as a count that
dropped rather than as continued green.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import contextlib
import io
import pathlib
import re
import shlex
import unittest

from activityintel import cli

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Every Markdown file in the repo, not one hand-listed file: the rot lands in
# whichever doc nobody opened.
DOC_FILES = sorted(ROOT.glob("*.md")) + sorted(ROOT.glob("docs/*.md"))

# Both shipped forms, bare and shell-wrapped. Two narrowings already happened
# here and each was caught only by the printed denominator:
#
#   11 -> 9   when docs moved to `zsh -lc 'cd / && activity-intel doctor'` and
#             the pattern was still anchored to line-start.
#   then      a character class excluding quotes captured `search` out of
#             `search "cooking class" hanoi` — the count stayed at 11 while a
#             subject was silently truncated, which no count-watching can see.
#
# So: take the rest of the line, strip an inline comment, then strip the shell
# wrapper's closing quote. Quotes INSIDE the arguments survive, because shlex
# needs them.
INVOCATION = re.compile(
    r"(?:^|&&\s*)\s*(?:python3 -m activityintel\.cli|activity-intel)\s+(.+?)\s*$",
    re.M)

# Placeholders a doc may legitimately use in an example.
PLACEHOLDER = re.compile(r"<[^>]+>")


def _commands() -> list[tuple[str, str]]:
    found = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for m in INVOCATION.finditer(text):
            args = m.group(1).strip()
            # Strip a trailing shell comment. Docs annotate examples inline, and
            # grading the annotation as an argument fails commands that are fine.
            args = re.split(r"\s+#", args, maxsplit=1)[0].strip()
            # A trailing quote belongs to the `zsh -lc '...'` wrapper, not to the
            # command. Balanced quotes inside the arguments are left alone.
            if args.endswith(("'", '"')) and args.count(args[-1]) % 2 == 1:
                args = args[:-1].strip()
            if args.startswith("#") or not args:
                continue
            found.append((path.name, args))
    return found


class DocumentedCommandsParse(unittest.TestCase):
    def test_every_documented_invocation_parses(self):
        cmds = _commands()
        self.assertGreaterEqual(
            len(cmds), 6,
            f"only found {len(cmds)} documented commands across "
            f"{[p.name for p in DOC_FILES]} — the extractor probably stopped "
            f"matching, which is a silently narrowed denominator, not a pass")

        parser = cli.build_parser()
        failures = []
        for source, raw in cmds:
            # Substitute placeholders so <query> style examples are still graded.
            argv = [PLACEHOLDER.sub("placeholder", tok) for tok in shlex.split(raw)]
            try:
                with contextlib.redirect_stderr(io.StringIO()), \
                     contextlib.redirect_stdout(io.StringIO()):
                    parser.parse_args(argv)
            except SystemExit as exc:
                # argparse exits for two opposite reasons and this used to
                # treat them as one. Code 2 is "I could not parse that" — the
                # defect this gate exists for. Code 0 is `--help` or
                # `--version`: parsed fine, answered, exited. Grading a
                # documented `--help` as a failure is the gate refusing a
                # command that works, which is how a correct gate teaches
                # people to delete it. Latent until v1.4.1, when the first doc
                # named `--help`.
                if (exc.code or 0) != 0:
                    failures.append(f"{source}: `{raw}` (exit {exc.code})")

        self.assertEqual(failures, [],
                         "documented commands that do not parse:\n  "
                         + "\n  ".join(failures))
        print(f"\n[docs-parity] graded {len(cmds)} documented commands "
              f"across {len(DOC_FILES)} files")

    def test_extractor_still_captures_whole_invocations(self):
        """Guards the extractor itself: a lossy pattern keeps the count and
        truncates every subject, which no count-watching can see."""
        for sample, expected in (
            ('python3 -m activityintel.cli catalog hanoi --match cooking --limit 10',
             "catalog hanoi --match cooking --limit 10"),
            ('activity-intel compare hanoi --ignore-robots --threshold 0.3',
             "compare hanoi --ignore-robots --threshold 0.3"),
            # The truncation that a stable count could not reveal.
            ('activity-intel search "cooking class" hanoi',
             'search "cooking class" hanoi'),
        ):
            m = INVOCATION.search(sample)
            self.assertIsNotNone(m, sample)
            self.assertEqual(m.group(1), expected)

    def test_extractor_grades_both_forms_present_in_the_docs(self):
        """If the docs stop using one form the count must move, not stay flat."""
        raws = {raw for _src, raw in _commands()}
        self.assertTrue(raws, "no documented commands found at all")

    def test_no_captured_subject_is_a_bare_subcommand_that_needs_arguments(self):
        """Catches truncation directly, not via the count.

        `search`, `catalog` and `compare` all require a positional. If one is
        captured with none, the extractor cut the line short — and the parser
        check would then fail for a reason that has nothing to do with the docs.
        """
        for src, raw in _commands():
            if raw.split()[0] in ("search", "catalog", "compare"):
                self.assertGreater(
                    len(raw.split()), 1,
                    f"{src}: captured a bare `{raw}` — the extractor truncated "
                    f"the line, probably at a quote")

    def test_a_shell_wrapped_invocation_is_captured_whole(self):
        m = INVOCATION.search("zsh -lc 'cd / && activity-intel doctor "
                              "--ignore-robots'")
        self.assertIsNotNone(m)
        self.assertIn("doctor --ignore-robots", m.group(1))


class LauncherRunsFromAnywhere(unittest.TestCase):
    """The shipped artifact, exercised from where a reader actually stands.

    Measured 2026-08-27: a documented command was pasted into a reply without
    its `cd` and produced `ModuleNotFoundError: No module named 'activityintel'`.
    Every check in this repo passed, because every one of them ran from the repo
    root. This one deliberately does not.
    """

    LAUNCHER = ROOT / "bin" / "activity-intel"

    def test_launcher_exists_and_is_executable(self):
        import os
        self.assertTrue(self.LAUNCHER.is_file(), f"missing {self.LAUNCHER}")
        self.assertTrue(os.access(self.LAUNCHER, os.X_OK),
                        f"{self.LAUNCHER} is not executable")

    def test_it_runs_from_a_foreign_working_directory(self):
        import json
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as elsewhere:
            proc = subprocess.run(
                [str(self.LAUNCHER), "sources"], cwd=elsewhere,
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"launcher failed outside the repo:\n{proc.stderr}")
        payload = json.loads(proc.stdout)
        self.assertIn("sources", payload)

    def test_it_runs_through_a_symlink_in_another_directory(self):
        """How it is actually installed: ~/.local/bin/activity-intel -> here."""
        import json
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as elsewhere:
            link = pathlib.Path(elsewhere) / "activity-intel"
            os.symlink(self.LAUNCHER, link)
            proc = subprocess.run([str(link), "sources"], cwd="/",
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"launcher failed through a symlink:\n{proc.stderr}")
        self.assertIn("sources", json.loads(proc.stdout))

    def test_a_detached_launcher_refuses_with_a_remedy(self):
        """Copied out of its repo, it must say so rather than fail obscurely."""
        import shutil
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as elsewhere:
            orphan = pathlib.Path(elsewhere) / "activity-intel"
            shutil.copy2(self.LAUNCHER, orphan)
            proc = subprocess.run([str(orphan), "sources"], cwd="/",
                                  capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ln -sf", proc.stderr)

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


if __name__ == "__main__":
    unittest.main()
