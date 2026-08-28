"""The decidable half of "this repo is AGPL-3.0 and says so consistently".

Licensing is mostly judgement and stays in NOTICE and README as prose. Four
claims in it are not judgement at all, and each one is the kind that rots
silently because nothing ever asserts on it:

  * the LICENSE file is the canonical AGPL-3.0 text and not, say, GPL-3.0 —
    the two differ by one section and look identical at a glance;
  * `pyproject.toml`, `NOTICE` and the package docstring agree about which
    licence this is;
  * every source adapter's host is named in NOTICE, so a source cannot be
    merged with the AGPL silently implying it covers whatever that source
    fetches (AGENTS.md rule 9, made decidable);
  * no shipped file tells a reader to run a path inside the author's home
    directory. That one is not hypothetical: until this release the README's
    only documented install was `ln -sf /Users/<owner>/Documents/…`, which is
    correct on exactly one machine on earth and was invisible for as long as
    the only reader was standing on it.

Each test reports the number of subjects it graded, so a selector that narrows
later shows up as a count that dropped rather than as continued green.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import pathlib
import re
import os
import subprocess
import sys
import unittest
import unittest.mock

import activityintel
from activityintel import cli

ROOT = pathlib.Path(__file__).resolve().parent.parent
LICENSE = ROOT / "LICENSE"
NOTICE = ROOT / "NOTICE"
PYPROJECT = ROOT / "pyproject.toml"

SPDX = "AGPL-3.0-or-later"

# Canonical GNU AGPL-3.0, 19 November 2007. Pinned by digest because the whole
# point of shipping a licence is that its text is exact: a truncated copy, a
# smart-quoted copy, or a GPL-3.0 copy pasted by mistake all still "look like a
# licence" in review. This digest is also what GitHub's own licensee classifier
# already recognises as agpl-3.0 on the sibling repos that ship it.
AGPL3_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"


class LicenseTextIsCanonical(unittest.TestCase):
    def test_license_file_is_the_canonical_agpl3_text(self):
        import hashlib
        self.assertTrue(LICENSE.is_file(), "LICENSE is missing")
        digest = hashlib.sha256(LICENSE.read_bytes()).hexdigest()
        self.assertEqual(
            digest, AGPL3_SHA256,
            "LICENSE is not the canonical AGPL-3.0 text. If this was a "
            "deliberate relicence, update AGPL3_SHA256 and NOTICE together; if "
            "it was not, restore the file.")

    def test_it_is_affero_not_plain_gpl(self):
        """The distinguishing clause, asserted directly.

        A digest pin catches any change; it does not explain what the file is.
        GPL-3.0 mentions the Affero licence in section 13 too, so grepping for
        the word 'Affero' does not separate them — the *network interaction*
        obligation does.
        """
        text = LICENSE.read_text(encoding="utf-8")
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", text)
        self.assertIn("13. Remote Network Interaction", text)
        self.assertIn("interacting with it remotely through a computer network",
                      text)


class DeclarationsAgree(unittest.TestCase):
    """One licence, stated in four places, which is three chances to disagree."""

    def test_pyproject_notice_and_package_all_say_agpl_3_or_later(self):
        subjects = {
            "pyproject.toml": PYPROJECT.read_text(encoding="utf-8"),
            "NOTICE": NOTICE.read_text(encoding="utf-8"),
            "activityintel/__init__.py": (
                ROOT / "activityintel" / "__init__.py").read_text(encoding="utf-8"),
        }
        missing = [name for name, text in subjects.items()
                   if "Affero" not in text and SPDX not in text]
        self.assertEqual(missing, [],
                         f"these files do not state the licence: {missing}")
        self.assertIn(f'license = "{SPDX}"', subjects["pyproject.toml"])
        print(f"\n[licence-parity] graded {len(subjects)} declaration sites")

    def test_pyproject_ships_the_license_files(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        m = re.search(r"license-files\s*=\s*\[([^\]]*)\]", text)
        self.assertIsNotNone(m, "pyproject declares no license-files")
        listed = set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertEqual(
            listed, {"LICENSE", "NOTICE"},
            "a wheel that omits NOTICE ships the code licence without the "
            "third-party data terms, which is the half people actually get "
            "wrong")

    def test_changelog_top_version_matches_the_package_version(self):
        """Two literals for one version agree the day they are typed."""
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"^##\s*v(\d+\.\d+\.\d+)", changelog, re.M)
        self.assertIsNotNone(m, "no versioned heading in CHANGELOG.md")
        self.assertEqual(
            m.group(1), activityintel.__version__,
            "CHANGELOG's newest release and activityintel.__version__ disagree")


class EverySourceHasItsDataTermsWrittenDown(unittest.TestCase):
    """AGENTS.md rule 9, as a gate rather than a request.

    The AGPL grants nothing about the listings a source fetches. A source
    adapter merged without a line in NOTICE leaves that unstated, which reads
    as though the licence covers it.
    """

    def test_each_adapter_host_is_named_in_notice(self):
        import importlib
        notice = NOTICE.read_text(encoding="utf-8")
        modules = sorted(
            p.stem for p in (ROOT / "activityintel" / "sources").glob("*.py")
            if p.stem != "__init__")
        self.assertGreaterEqual(
            len(modules), 3,
            f"only found {modules} — the selector narrowed, which is not a pass")

        missing = []
        for name in modules:
            mod = importlib.import_module(f"activityintel.sources.{name}")
            host = getattr(mod, "HOST", None)
            self.assertIsNotNone(host, f"{name} declares no HOST")
            # Match on the registrable domain: NOTICE keys its sections on
            # `klook.com`, while the adapter fetches `www.klook.com`.
            domain = ".".join(host.split(".")[-2:])
            if domain not in notice:
                missing.append(f"{name} ({host} -> {domain})")

        self.assertEqual(
            missing, [],
            "sources with no data terms in NOTICE: " + ", ".join(missing))
        print(f"[licence-parity] graded {len(modules)} source adapters "
              f"against NOTICE")


class NothingShippedPointsIntoTheAuthorsHomeDirectory(unittest.TestCase):
    """The bug publication exposed, pinned so it cannot come back.

    Ranges over every shipped text file rather than the one README where it
    happened, because the next copy of that path will be pasted somewhere else.
    """

    # `/Users/<name>/` and `/home/<name>/` — an absolute path into somebody's
    # account. `~/` is fine: it resolves for whoever runs it, which is the
    # entire difference.
    HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")

    # Accounts that are obviously nobody: the fake paths this file and
    # tools/mutate.py need in order to prove the detector fires.
    PLACEHOLDERS = frozenset({"someone", "ci", "you", "user", "youruser",
                              "username", "me", "example"})

    # ...and the exemption is scoped to the test harness, because a blanket
    # allowlist is a hole rather than a nuance. Measured immediately: with
    # placeholders excused everywhere, tools/mutate.py's own mutant — which
    # rewrites the README's install line to `/Users/someone/…` — went from
    # CAUGHT to ESCAPED. Documentation must contain no absolute home path at
    # all; `/Users/someone/Documents/…` in a README is not less broken for
    # naming a fictional person, it is a command that works for nobody.
    VECTOR_FILES = ("tests/", "tools/")

    def _subjects(self) -> list[pathlib.Path]:
        """Every published text file — discovered, not listed.

        The first version of this test globbed six hand-written patterns and
        covered neither `tests/` nor `tools/`, which is where all three
        absolute paths in the repo actually live. It reported 22 files and
        passed, and the count could never have revealed the gap: a selector
        that never looked somewhere does not report a smaller number, it
        reports a confident one. Walking the tree fixes the denominator at the
        cost of needing an explicit skip list, which is the right trade —
        a skip is visible in the diff, a missing glob is not.
        """
        # Ask git what could be published rather than what happens to be on
        # disk. Measured 2026-08-28: leaving a build venv and an .egg-info in
        # the tree moved this denominator from 40 to 43 between two runs of the
        # same gate — a count that changes with local clutter cannot be read as
        # evidence about what ships. `--others --exclude-standard` still honours
        # .gitignore, so a brand-new file (where a fresh leak actually lives) is
        # in scope while build artefacts are not.
        tracked = self._git_publishable()
        skip_dirs = {".git", "__pycache__", "tests/fixtures", "build", "dist",
                     ".venv", "venv"}
        keep_suffix = {".md", ".toml", ".py", ".cfg", ".txt", ".yml", ".yaml"}
        out: list[pathlib.Path] = []
        for p in sorted(ROOT.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if tracked is not None and rel.as_posix() not in tracked:
                continue
            if rel.suffix == ".egg-info" or ".egg-info" in rel.parts[0]:
                continue
            if any(part in skip_dirs for part in rel.parts):
                continue
            if str(rel.parent).startswith("tests/fixtures"):
                continue
            # Extensionless files count too: `bin/activity-intel` is exactly the
            # kind of file that carries an install path, and LICENSE/NOTICE are
            # the documents this module exists to grade.
            extensionless_root = len(rel.parts) == 1 and p.suffix == ""
            if (p.suffix not in keep_suffix
                    and rel.parts[0] != "bin"
                    and not extensionless_root):
                continue
            # progress.md is the operator's own log, deliberately unpublished
            # (.git/info/exclude), and the one place an estate path is correct.
            if rel.name == "progress.md":
                continue
            out.append(p)
        return out

    @staticmethod
    def _git_publishable() -> set[str] | None:
        """Paths git would let out of here, or None when git cannot answer.

        None means "fall back to walking the tree" — an sdist extraction has no
        .git. The mode is printed with the denominator so a reader can tell a
        narrowed selector from a small repository.
        """
        try:
            out = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "--cached", "--others",
                 "--exclude-standard"],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return {line for line in out.stdout.splitlines() if line}

    def test_no_shipped_file_hardcodes_a_home_directory(self):
        subjects = self._subjects()
        self.assertGreaterEqual(
            len(subjects), 15,
            f"only found {len(subjects)} shipped files — the selector "
            f"narrowed, which is not a pass")

        offenders, placeholders = [], 0
        for path in subjects:
            rel = str(path.relative_to(ROOT))
            is_vector_file = rel.startswith(self.VECTOR_FILES)
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), 1):
                m = self.HOME_PATH.search(line)
                if not m:
                    continue
                if is_vector_file and m.group(1) in self.PLACEHOLDERS:
                    placeholders += 1      # a deliberate fake in the harness
                    continue
                offenders.append(f"{rel}:{line_no}")

        self.assertEqual(
            offenders, [],
            "absolute home paths in shipped files — correct on exactly one "
            "machine:\n  " + "\n  ".join(offenders))
        mode = ("git-publishable" if self._git_publishable() is not None
                else "tree-walk (no git here)")
        print(f"[licence-parity] graded {len(subjects)} shipped files for "
              f"machine-specific paths via {mode} ({placeholders} placeholder "
              f"paths allowed)")

    def test_the_detector_actually_fires(self):
        """A gate nobody has seen red is not evidence."""
        # Assembled from fragments, never written as a literal path. A scanner
        # that spells its own positive samples out flags its own source file —
        # and the obvious repair, exempting that file, carves out the one file
        # guaranteed to accumulate exactly the strings it hunts for.
        real = self.HOME_PATH.search(
            'ln -sf "/' + 'Users' + '/vecsatfox' + 'mailcom/Documents/x/bin/y"')
        self.assertIsNotNone(real)
        self.assertNotIn(real.group(1), self.PLACEHOLDERS,
                         "a real account name must not be excused")
        fake = self.HOME_PATH.search("/home/ci/checkout/bin/x")
        self.assertIsNotNone(fake)
        self.assertIn(fake.group(1), self.PLACEHOLDERS)
        self.assertIsNone(self.HOME_PATH.search(
            'ln -sf "$PWD/bin/activity-intel" ~/.local/bin/activity-intel'))

    def test_a_placeholder_in_documentation_is_still_a_failure(self):
        """The exemption must not become a hole.

        `/Users/someone/…` in a README is not a milder version of the bug —
        it is a command that works for nobody at all. Only the harness may use
        a fake account, and only to prove the detector fires.
        """
        self.assertTrue("README.md".startswith(self.VECTOR_FILES) is False)
        self.assertTrue("tests/test_license_and_packaging.py"
                        .startswith(self.VECTOR_FILES))
        self.assertTrue("tools/mutate.py".startswith(self.VECTOR_FILES))
        for doc in ("README.md", "AGENTS.md", "docs/SOURCES.md",
                    "bin/activity-intel", "pyproject.toml"):
            self.assertFalse(
                doc.startswith(self.VECTOR_FILES),
                f"{doc} must never be allowed a placeholder home path")

    def test_the_subject_list_reaches_the_directories_it_used_to_miss(self):
        """Guards the denominator itself, which no count could reveal."""
        rel = {str(p.relative_to(ROOT)) for p in self._subjects()}
        for expected in ("README.md", "NOTICE", "pyproject.toml",
                         "bin/activity-intel", "docs/SOURCES.md",
                         "activityintel/cli.py",
                         "tests/test_license_and_packaging.py",
                         "tools/mutate.py"):
            self.assertIn(expected, rel,
                          f"{expected} is outside the guard's reach")
        self.assertNotIn("progress.md", rel, "the private log must stay exempt")
        self.assertFalse([r for r in rel if r.startswith("tests/fixtures")],
                         "captured responses are data, not shipped text")

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


class TestFilesRunWhicheverWayTheyAreInvoked(unittest.TestCase):
    """`unittest.main()` calls `sys.exit()`, so anything defined below it never
    runs under direct invocation.

    `unittest discover` imports the module instead, so `__name__` is not
    `"__main__"`, the entry block does not fire, and every class is collected.
    That is the trap: the documented command finds them and
    `python3 tests/test_x.py` silently finds fewer, and the two numbers are
    never compared. Measured 2026-08-28 in this repo — three classes appended
    below the entry block of `test_policy_and_compare.py`, 184 tests under
    discover and 181 under direct invocation, suite green both times. Appending
    is what puts them there, and appending is what a tool does by default.
    """

    # A regex, not one exact spelling: single quotes or different spacing would
    # silently escape grading, and this guard's whole value is its denominator.
    ENTRY = re.compile(r"(?m)^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:")

    def test_nothing_is_defined_below_the_entry_block(self):
        graded, offenders = 0, []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            m = self.ENTRY.search(text)
            if m is None:
                continue
            graded += 1
            tail = text[m.end():]
            for i, line in enumerate(tail.splitlines()):
                if line.startswith(("def ", "class ", "@")):
                    offenders.append(f"{path.name}: {line.strip()[:60]}")
        self.assertGreaterEqual(graded, 4, "selector found almost no test files")
        self.assertEqual(
            offenders, [],
            "defined below `unittest.main()` — invisible to direct "
            "invocation:\n  " + "\n  ".join(offenders))
        print(f"[entry-block] graded {graded} test files")


class UsageNamesACommandThatRuns(unittest.TestCase):
    """The usage line is the one string that tells a stranger how to invoke
    this, and nothing asserted on it.

    `prog` was pinned to the literal `"python3 -m activityintel.cli"`, so every
    install route printed that form -- including the sh launcher, which exists
    *because* that form loses its `cd` in a handoff, and the pip console script,
    which is not a module invocation at all. Measured from `/` on 2026-08-28:
    the command printed its own usage happily and the command it named raised
    `ModuleNotFoundError`. Same defect as a remedy naming a flag the parser
    rejects; here the remedy named a directory the reader was not standing in.

    So the property, not the spelling: whatever the usage line calls this
    program has to run from a directory that is not the checkout. Asserting the
    exact string would pass just as well for a *different* wrong constant.
    """

    LAUNCHER = ROOT / "bin" / "activity-intel"

    def _usage_prog(self, argv, cwd, env=None):
        out = subprocess.run(argv + ["--help"], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        first = out.stdout.splitlines()[0]
        self.assertTrue(first.startswith("usage: "), first)
        return first[len("usage: "):].split()[0]

    def test_launcher_usage_names_the_launcher(self):
        prog = self._usage_prog([str(self.LAUNCHER)], cwd="/")
        self.assertEqual(prog, "activity-intel",
                         "the launcher tells the reader to run something else")

    def test_the_name_it_prints_actually_runs_from_outside_the_checkout(self):
        """`sources` is the offline smoke: no network, no credential."""
        prog = self._usage_prog([str(self.LAUNCHER)], cwd="/")
        # Resolve the printed name back to the launcher rather than trusting
        # $PATH, so the test grades this checkout and not whatever is installed.
        self.assertEqual(prog, self.LAUNCHER.name)
        out = subprocess.run([str(self.LAUNCHER), "sources"], cwd="/",
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0,
                         f"`{prog} sources` fails from /: {out.stderr[-400:]}")

    def test_the_old_hardcoded_form_really_does_fail_from_slash(self):
        """The negative control. Without it the two tests above would pass on a
        machine where the module form happens to resolve, and the defect they
        describe would read as hypothetical."""
        out = subprocess.run([sys.executable, "-m", "activityintel.cli", "--help"],
                             cwd="/", capture_output=True, text=True, timeout=120,
                             env={k: v for k, v in os.environ.items()
                                  if k != "PYTHONPATH"})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("No module named", out.stderr)

    def test_prog_is_not_hardcoded_back_to_the_module_form(self):
        """In-process guard, so a re-hardcode fails fast and cheaply."""
        env = {k: v for k, v in os.environ.items() if k != "ACTIVITY_INTEL_PROG"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            prog = cli.build_parser().prog
        self.assertNotIn("activityintel.cli", prog,
                         "prog is hardcoded again; let argparse read argv[0]")

    def test_a_renamed_symlink_reports_its_own_name(self):
        """The launcher hands off through `python3 -m`, so the name the caller
        typed only survives in the env var. Without this, the override could be
        dropped from the shim and the two tests above would still pass on the
        one spelling they happen to check."""
        env = dict(os.environ, ACTIVITY_INTEL_PROG="renamed-on-purpose")
        self.assertEqual(self._prog_under(env), "renamed-on-purpose")

    def _prog_under(self, env):
        out = subprocess.run(
            [sys.executable, "-c",
             "from activityintel.cli import build_parser; print(build_parser().prog)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        return out.stdout.strip()


if __name__ == "__main__":
    unittest.main()
