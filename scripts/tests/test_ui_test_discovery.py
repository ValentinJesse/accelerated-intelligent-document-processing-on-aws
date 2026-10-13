# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every UI test file in the repository is one the UI test gate collects.

Both CIs run the web UI's Vitest suite — ``cd src/ui && npm ci && npx vitest run
--silent``, in GitLab's ``ui_tests`` job and GitHub's, neither of them allowed to
fail — so the suite *runs* where a regression would be caught. What nothing
checked is whether it runs the tests that exist.

``src/ui/vite.config.js`` overrides Vitest's file discovery, and an override is a
list of patterns rather than a rule. The value was
``['src/**/*.test.{ts,tsx}']``, two extensions out of the twelve Vitest's own
convention accepts and four out of the ``src/**/*.{js,jsx,ts,tsx}`` the UI's
eslint script lints — so ``Foo.test.jsx`` was a file the linter read, the
typechecker read, and the test runner silently did not. Nothing would have said
so: Vitest exits 0 when every file it collected passes, and the override
happened to reach every test file in the tree only because nobody had yet
written one in the other ten extensions.

Narrowing the glob has the same shape and is the mutation this module was
measured against. Dropping ``tsx`` from it leaves ``npx vitest run --silent`` —
the command both CIs run — reporting ``Test Files 66 passed (66)``,
``Tests 891 passed (891)`` and exit 0, where the unmutated tree reports 113 and
1348. Forty-seven test files and 457 tests did not execute, and no other gate in
the repository objected (#1365).

So the assertion here is not about the glob's spelling. It is that **no test file
in the repository is outside what the configured globs collect**, with both sides
derived:

* the file universe comes from ``git ls-files`` plus untracked-but-not-ignored
  files under ``src/ui``, so a test file a developer has just written counts, and
  a gitignored one does not;
* what counts as a test file comes from Vitest's own ``defaultInclude`` — the
  twelve-extension, two-stem convention ``**/*.{test,spec}.?(c|m)[jt]s?(x)``
  spells out — rather than from this repository's current habit of ``.test.ts``;
* what is collected is computed by expanding the configured ``include`` and
  ``exclude`` out of the config file and resolving them against the tree.

Three bounds, stated so a green run is not over-read. This checks *discovery*,
not that a collected file contains a passing test or any test at all — Vitest
reports a file with no tests as an error, which is that half. It reads the
``test`` block of ``vite.config.js`` as text, and fails loudly if it cannot find
one rather than passing with an empty pattern list. And a test file that does not
follow the naming convention at all (a plain module under ``__tests__/``) is
invisible to Vitest's default rule too, so it is outside this universe and not
reported: the convention is the thing being held, not every file that might
contain an assertion.
"""

from __future__ import annotations

import functools
import glob as globlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = REPO_ROOT / "src" / "ui"
CONFIG = UI_ROOT / "vite.config.js"
NODE_MODULES = UI_ROOT / "node_modules"

#: Vitest's own default test-file pattern, as exported by ``vitest/config`` as
#: ``defaultInclude``. :func:`test_the_naming_convention_is_vitests_own` reads it
#: back from the installed package and fails if it has moved, which is the only
#: way the two literals below can be kept honest across a Vitest upgrade.
VITEST_DEFAULT_INCLUDE = "**/*.{test,spec}.?(c|m)[jt]s?(x)"

#: ``{test,spec}`` and the expansion of ``?(c|m)[jt]s?(x)``. Spelled out because
#: this module has to answer "is this a test file" without Node — the suite it
#: lives in runs where ``src/ui/node_modules`` may not exist — and calibrated
#: against Vitest's real matcher where Node is available.
TEST_STEMS = ("test", "spec")
TEST_EXTENSIONS = (
    "js",
    "jsx",
    "ts",
    "tsx",
    "cjs",
    "cjsx",
    "cts",
    "ctsx",
    "mjs",
    "mjsx",
    "mts",
    "mtsx",
)

pytestmark = pytest.mark.unit


def _vitest_test_block() -> str:
    """The body of the ``test: { ... }`` key in ``src/ui/vite.config.js``.

    Scoped to that block on purpose: the same file has an ``include`` under the
    React plugin and another under ``optimizeDeps``, and reading either of those
    as the test-discovery pattern would produce a confident wrong answer. A
    failure to find the block is an assertion error rather than an empty result.
    """
    text = CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s*test:\s*\{", text, re.MULTILINE)
    assert match, (
        f"{CONFIG.relative_to(REPO_ROOT)} has no `test: {{` block, so Vitest's "
        "file discovery for the UI suite is configured somewhere this check does "
        "not read. Point it at the new location rather than deleting it: with no "
        "block found there is no pattern list to compare the tree against."
    )
    depth = 0
    start = match.end() - 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("unbalanced braces in the vite config's test block")


def _configured_globs(key: str) -> list[str]:
    """The string literals of ``key: [...]`` inside the ``test`` block."""
    block = _vitest_test_block()
    match = re.search(rf"\b{re.escape(key)}:\s*\[(.*?)\]", block, re.DOTALL)
    if not match:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", match.group(1))


def _expand_braces(pattern: str) -> list[str]:
    """``a.{b,c}.{d,e}`` → the four brace-free patterns.

    Python's ``glob`` has no brace expansion and picomatch — which is what Vitest
    uses — does, so the patterns have to be expanded before they can be resolved
    here. :func:`test_the_glob_expansion_agrees_with_vitests_matcher` is what
    keeps this from being a second, divergent matcher.
    """
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    out: list[str] = []
    for alternative in match.group(1).split(","):
        out.extend(_expand_braces(f"{head}{alternative}{tail}"))
    return out


def _resolve(patterns: list[str]) -> set[str]:
    """Every existing path under ``src/ui`` that any of ``patterns`` matches."""
    found: set[str] = set()
    for pattern in patterns:
        for expanded in _expand_braces(pattern):
            found.update(globlib.glob(expanded, root_dir=str(UI_ROOT), recursive=True))
    return {Path(path).as_posix() for path in found}


@functools.lru_cache(maxsize=1)
def _collected() -> set[str]:
    """What the configured globs collect: ``include`` minus ``exclude``."""
    return _resolve(_configured_globs("include")) - _resolve(
        _configured_globs("exclude")
    )


def _git(*args: str) -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return [line for line in result.stdout.splitlines() if line]


@functools.lru_cache(maxsize=1)
def _test_files_in_the_tree() -> set[str]:
    """Test-named files under ``src/ui``, relative to it.

    Tracked files **and** untracked ones git does not ignore: a test file written
    five minutes ago is exactly the case this is for, and ``node_modules``,
    ``dist`` and ``build`` are ignored, so they drop out without a prune list of
    their own.
    """
    prefix = "src/ui/"
    names = {
        f".{stem}.{extension}" for stem in TEST_STEMS for extension in TEST_EXTENSIONS
    }
    paths = _git("ls-files", "--", prefix) + _git(
        "ls-files", "--others", "--exclude-standard", "--", prefix
    )
    return {
        path[len(prefix) :]
        for path in paths
        if path.startswith(prefix) and path.endswith(tuple(names))
    }


# --------------------------------------------------------------------------- #
# vacuity floors — either side empty makes the gate below meaningless
# --------------------------------------------------------------------------- #
def test_the_configured_globs_and_the_file_universe_are_both_non_empty() -> None:
    include = _configured_globs("include")
    assert include, (
        "no `include` patterns were read out of the vite config's test block. An "
        "empty pattern list would make the comparison below vacuously true while "
        "Vitest fell back to its own defaults — or collected nothing."
    )
    files = _test_files_in_the_tree()
    assert files, (
        "no test-named file was found under src/ui, so this module would assert "
        "nothing about a suite of 113 files. Either the naming convention in "
        "TEST_STEMS/TEST_EXTENSIONS no longer describes this tree, or the git "
        "listing is not reaching src/ui."
    )
    assert _collected(), (
        "the configured globs resolve to no existing file, so the UI suite "
        "collects nothing"
    )


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_every_test_file_in_the_ui_tree_is_collected() -> None:
    """A test file the runner does not collect is a test that does not exist.

    It is worse than a deleted one: the file is in the tree, it is linted and
    typechecked, it is read in review as covering something, and it never runs.
    """
    missed = sorted(_test_files_in_the_tree() - _collected())
    assert not missed, (
        f"{len(missed)} test file(s) under src/ui are not collected by the "
        f"`test.include`/`test.exclude` globs in "
        f"{CONFIG.relative_to(REPO_ROOT)}:\n  "
        + "\n  ".join(missed)
        + f"\n\nconfigured include: {_configured_globs('include')}"
        + f"\nconfigured exclude: {_configured_globs('exclude')}"
        + "\n\nEach of these is a file `npx vitest run` — the command both CIs "
        "run — will not execute, and it will exit 0 without mentioning them. "
        "Widen the glob, or move the file to where the glob reaches."
    )


def test_no_exclude_pattern_removes_a_test_file() -> None:
    """The same hole from the other side, named separately because the remedy is.

    An ``exclude`` entry added to silence one slow or flaky suite takes out
    everything else its pattern matches, and the count it changes is not printed
    anywhere.
    """
    excluded = sorted(
        _test_files_in_the_tree() & _resolve(_configured_globs("exclude"))
    )
    assert not excluded, (
        "these test files are matched by an `exclude` pattern in "
        f"{CONFIG.relative_to(REPO_ROOT)} and will not run:\n  " + "\n  ".join(excluded)
    )


# --------------------------------------------------------------------------- #
# calibration against Vitest's own matcher, where Node is available
# --------------------------------------------------------------------------- #
def _node(script: str) -> str:
    if shutil.which("node") is None or not NODE_MODULES.is_dir():
        pytest.skip(
            "src/ui/node_modules or node is absent, so Vitest's own matcher "
            "cannot be consulted. This skips a CALIBRATION of the two literals "
            "in this module, not the gate above, which runs either way."
        )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["node", "-e", script],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"node probe failed: {result.stderr[-2000:]}"
    return result.stdout.strip()


def test_the_naming_convention_is_vitests_own() -> None:
    """``defaultInclude`` is the authority for what a test file is called.

    Holding the literal rather than a count: if a Vitest upgrade broadens the
    convention, the universe this module derives would silently stop covering
    the new spelling, and a file in it would be uncollected *and* unreported.
    """
    reported = _node(
        "import('vitest/config').then(m =>"
        " console.log(JSON.stringify(m.defaultInclude)))"
    )
    assert json.loads(reported) == [VITEST_DEFAULT_INCLUDE], (
        f"Vitest's defaultInclude is now {reported}, not "
        f"[{VITEST_DEFAULT_INCLUDE!r}]. TEST_STEMS and TEST_EXTENSIONS in this "
        "module are that pattern expanded by hand; re-derive them from the new "
        "one before changing this literal."
    )


def test_the_expanded_extension_set_is_what_vitest_matches() -> None:
    """Every spelling Vitest's pattern accepts, and no more.

    ``?(c|m)[jt]s?(x)`` is twelve extensions, several of which nobody writes —
    and the ones nobody writes are the ones a hand-written list drops. Compared
    against picomatch, which is the matcher Vitest resolves the pattern with.
    """
    probe = (
        "const pm = require('picomatch');\n"
        f"const match = pm({VITEST_DEFAULT_INCLUDE!r});\n"
        "const stems = ['test', 'spec', 'tests', 'check'];\n"
        "const exts = ['js','jsx','ts','tsx','cjs','cjsx','cts','ctsx',"
        "'mjs','mjsx','mts','mtsx','mdx','json','md','jsz','csx'];\n"
        "const hits = [];\n"
        "for (const s of stems) for (const e of exts)"
        " if (match('a/b.' + s + '.' + e)) hits.push(s + '|' + e);\n"
        "console.log(JSON.stringify(hits));\n"
    )
    matched = {tuple(hit.split("|")) for hit in json.loads(_node(probe))}
    expected = {
        (stem, extension) for stem in TEST_STEMS for extension in TEST_EXTENSIONS
    }
    assert matched == expected, (
        "TEST_STEMS x TEST_EXTENSIONS is not what Vitest's pattern matches.\n"
        f"  only Vitest: {sorted(matched - expected)}\n"
        f"  only here  : {sorted(expected - matched)}"
    )


def test_the_glob_expansion_agrees_with_vitests_matcher() -> None:
    """This module's brace expansion against picomatch, on the real patterns.

    The gate above rests on a second implementation of pattern matching, written
    here because the suite it lives in cannot count on Node. Two implementations
    of one rule is the shape that drifts, so where Node *is* available they are
    required to classify every test file in the tree identically.
    """
    include = _configured_globs("include")
    exclude = _configured_globs("exclude")
    files = sorted(_test_files_in_the_tree())
    probe = (
        "const pm = require('picomatch');\n"
        f"const include = {json.dumps(include)};\n"
        f"const exclude = {json.dumps(exclude)};\n"
        f"const files = {json.dumps(files)};\n"
        "const inc = include.map(p => pm(p));\n"
        "const exc = exclude.map(p => pm(p));\n"
        "console.log(JSON.stringify(files.filter(f =>"
        " inc.some(m => m(f)) && !exc.some(m => m(f)))));\n"
    )
    assert set(json.loads(_node(probe))) == set(files) & _collected(), (
        "picomatch and this module's glob expansion disagree about which test "
        "files the configured patterns collect, so the gate above is measuring "
        "something other than what Vitest will do"
    )
