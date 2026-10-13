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
so: Vitest exits 0 when every file it collected passes, and the override happened
to reach every test file in the tree only because nobody had yet written one in
the other ten extensions.

Narrowing the glob has the same shape and is the mutation this module was
measured against. Dropping ``tsx`` from it leaves ``npx vitest run --silent`` —
the command both CIs run — reporting ``Test Files 66 passed (66)``,
``Tests 891 passed (891)`` and exit 0, where the unmutated tree reports 113 and
1348. Forty-seven test files and 457 tests did not execute, and no other gate in
the repository objected (#1365).

So the assertion here is not about the glob's spelling. It is that **no test file
in the repository is outside what Vitest will collect**, with both sides derived:

* the file universe comes from ``git ls-files`` plus untracked-but-not-ignored
  files under ``src/ui``, so a test file a developer has just written counts, and
  a gitignored one does not;
* what counts as a test file comes from Vitest's own ``defaultInclude`` — the
  twelve-extension, two-stem convention ``**/*.{test,spec}.?(c|m)[jt]s?(x)``
  spells out — rather than from this repository's current habit of ``.test.ts``;
* what is collected is computed from the ``include``/``exclude`` the config
  actually applies, falling back to Vitest's defaults where the config states
  neither.

⚠️ **"The configured globs" is a weaker question than "what Vitest will
collect", and the difference is five measured escapes.** The first version of
this module read ``vite.config.js`` as text and compared patterns. Each of these
left it green while ``npx vitest run --silent`` exited 0 over 66 of 113 files:

#. a **commented-out** wide pattern left in the ``include`` array above a narrow
   live one — the thing a developer actually types when narrowing a glob for an
   afternoon;
#. a sibling ``src/ui/vitest.config.js``, which **takes precedence** over
   ``vite.config.js`` and which the parse never looked for;
#. ``exclude: [...].concat([...])``, where a regex anchored on ``exclude:\\s*\\[``
   reads the first array and not the second;
#. a **second** ``test:`` key later in the object, where JavaScript takes the last
   and a regex takes the first;
#. ``--config narrow.config.js`` added to the vitest step in **both** CI configs,
   which no assertion about this file can see.

Each is closed below, and the shape of the remedy is the same in every case: ask
what Vitest will do, not what the file says. Comments are stripped before
parsing; a non-literal pattern array and a duplicate ``test:`` key are refused
rather than half-read; a config file that would outrank this one fails the gate by
name; the CI invocations are required to carry no option that narrows discovery;
and where Node is available the parse is checked against ``vitest list
--filesOnly``, which is Vitest's own answer.

Three bounds, stated so a green run is not over-read. This checks *discovery*, not
that a collected file contains a passing test or any test at all — Vitest reports
a file with no tests as an error, which is that half. A test file that does not
follow the naming convention at all (a plain module under ``__tests__/``) is
invisible to Vitest's default rule too, so it is outside this universe and not
reported: the convention is the thing being held, not every file that might
contain an assertion.

⚠️ And the third: **the three calibrations below need Node and the UI's npm
dependencies, and neither CI's package-test job installs them**, so there they
skip while the gate assertions run. The practical consequence is one specific
hole, measured: narrowing the ``include`` *and* :data:`TEST_EXTENSIONS` to the
same shorter list leaves this module green in CI, because the universe and the
pattern were narrowed together. That takes a coordinated edit in two files, which
is a far more visible act than a one-line glob change — which is the trade being
made here rather than a gap nobody noticed. Everything that catches a change to
the config *alone* runs with no Node at all.
"""

from __future__ import annotations

import functools
import glob as globlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = REPO_ROOT / "src" / "ui"
CONFIG = UI_ROOT / "vite.config.js"
NODE_MODULES = UI_ROOT / "node_modules"
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
GITHUB_CI = REPO_ROOT / ".github" / "workflows" / "developer-tests.yml"

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

#: What Vitest applies for each key when the config states it nowhere — its own
#: ``defaultInclude`` and ``defaultExclude``, in the brace syntax this module can
#: resolve. Modelling the tool's behaviour, not a carve-out from any gate here:
#: deleting the override in ``vite.config.js`` is a *safe* change, because the
#: default is a superset of it for this tree, and a check that reported it as a
#: tree full of uncollected files would be wrong about the most robust
#: configuration available, which is how a gate teaches people to ignore it.
VITEST_DEFAULTS = {
    "include": (f"**/*.{{{','.join(TEST_STEMS)}}}.{{{','.join(TEST_EXTENSIONS)}}}",),
    "exclude": ("**/node_modules/**", "**/.git/**"),
}

#: Vitest config filenames that outrank ``vite.config.js``. Vitest reads a
#: ``vitest.config.*`` in preference to a ``vite.config.*``, so one of these
#: appearing beside it silently replaces every pattern asserted here.
OUTRANKING_CONFIG_NAMES = tuple(
    f"vitest.config.{extension}"
    for extension in ("ts", "mts", "cts", "js", "mjs", "cjs")
) + tuple(
    f"vite.config.{extension}" for extension in ("ts", "mts", "cts", "mjs", "cjs")
)

#: CLI options that change which files Vitest discovers. A CI step carrying one of
#: these decides discovery at the call site, where nothing in this repository is
#: looking. ``--silent``, ``--reporter`` and the rest do not appear here because
#: they change output, not the file set.
DISCOVERY_NARROWING_OPTIONS = (
    "--config",
    "-c",
    "--root",
    "-r",
    "--dir",
    "--include",
    "--exclude",
    "--project",
    "--workspace",
    "--shard",
    "--changed",
    "--related",
)

pytestmark = pytest.mark.unit


def _strip_js_comments(text: str) -> str:
    """``text`` with ``//`` and ``/* */`` comments blanked, strings left alone.

    Blanked rather than deleted so every offset and line number is preserved, and
    string-aware because a `//` inside a quoted pattern is not a comment. This is
    the first of the five escapes above: a commented-out wide glob left in the
    ``include`` array reads to a plain regex exactly like a live one, so the gate
    saw the pattern it wanted and Vitest used the narrow one underneath it.
    """
    out = list(text)
    index = 0
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
            out[index:end] = " " * (end - index)
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) if end == -1 else end + 2
            for position in range(index, end):
                if text[position] != "\n":
                    out[position] = " "
            index = end
            continue
        index += 1
    return "".join(out)


@functools.lru_cache(maxsize=1)
def _config_text() -> str:
    return _strip_js_comments(CONFIG.read_text(encoding="utf-8"))


def _balanced(text: str, start: int) -> str:
    """The ``{...}`` beginning at ``start``, braces balanced."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces from offset {start} in {CONFIG}")


@functools.lru_cache(maxsize=1)
def _vitest_test_block() -> str:
    """The body of the one ``test: { ... }`` key in ``src/ui/vite.config.js``.

    Scoped to that key on purpose: the same file has an ``include`` under the React
    plugin and another under ``optimizeDeps``, and reading either of those as the
    test-discovery pattern would produce a confident wrong answer.

    **Exactly one**, and that is the fourth escape. JavaScript takes the last of
    two duplicate keys and a regex takes the first, and esbuild reports the
    duplicate as a warning that nothing here reads — ``vite.config.js`` is outside
    the UI's eslint scope — so a second ``test:`` block silently replaced this one.
    """
    text = _config_text()
    starts = [
        match.end() - 1 for match in re.finditer(r"^\s*test:\s*\{", text, re.MULTILINE)
    ]
    assert len(starts) == 1, (
        f"{CONFIG.relative_to(REPO_ROOT)} has {len(starts)} `test: {{` blocks, "
        "expected exactly one. With none, Vitest's file discovery for the UI suite "
        "is configured somewhere this check does not read; with two, JavaScript "
        "uses the last and this check would read the first. Point this at the real "
        "one rather than deleting the assertion."
    )
    return _balanced(text, starts[0])


def _configured_globs(key: str) -> list[str] | None:
    """The string literals of ``key: [...]`` in the ``test`` block, or None.

    ``None`` means the key is absent, which is a real and safe state — Vitest then
    uses its own defaults. What is *not* tolerated is a value this cannot read in
    full: ``exclude: [...].concat([...])`` satisfies a regex anchored on the first
    ``[`` while Vitest applies both arms, so anything other than a flat array of
    string literals fails here rather than being half-read.
    """
    block = _vitest_test_block()
    match = re.search(rf"(?:^|[{{,\s]){re.escape(key)}:\s*", block)
    if not match:
        return None
    rest = block[match.end() :]
    assert rest.startswith("["), (
        f"`{key}` in the vite config's test block is not a literal array: "
        f"{rest[:60]!r}. This check can only read a flat list of string literals; "
        "a computed value would be read in part and applied in full."
    )
    close = rest.find("]")
    assert close != -1, f"unterminated `{key}` array in {CONFIG}"
    body = rest[1:close]
    assert re.fullmatch(r"(?:\s*(?:'[^']*'|\"[^\"]*\")\s*,?)*\s*", body), (
        f"`{key}` holds something other than string literals: {body!r}"
    )
    tail = rest[close + 1 :].lstrip()
    assert not tail.startswith("."), (
        f"`{key}` is an array expression, not an array: {rest[: close + 20]!r}. "
        "The part after the first `]` is applied by Vitest and not read here."
    )
    return re.findall(r"""['"]([^'"]+)['"]""", body)


def _effective_globs(key: str) -> list[str]:
    """What Vitest will apply for ``key``: the config's value, or its own default."""
    configured = _configured_globs(key)
    if configured is not None:
        return configured
    return list(VITEST_DEFAULTS[key])


def _expand_braces(pattern: str) -> list[str]:
    """``a.{b,c}.{d,e}`` → the four brace-free patterns.

    Python's ``glob`` has no brace expansion and picomatch — which is what Vitest
    uses — does, so the patterns have to be expanded before they can be resolved
    here. :func:`test_the_collected_set_is_the_one_vitest_reports` is what keeps
    this from being a second, divergent matcher.
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
    """What Vitest will collect: the effective include minus the effective exclude."""
    return _resolve(_effective_globs("include")) - _resolve(_effective_globs("exclude"))


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
def test_the_effective_globs_and_the_file_universe_are_both_non_empty() -> None:
    assert _effective_globs("include"), (
        "no include patterns are in effect for the UI suite — neither in the vite "
        "config's test block nor from VITEST_DEFAULTS — so the comparison "
        "below would be vacuously true while Vitest collected nothing."
    )
    files = _test_files_in_the_tree()
    assert files, (
        "no test-named file was found under src/ui, so this module would assert "
        "nothing about the UI suite. Either the naming convention in "
        "TEST_STEMS/TEST_EXTENSIONS no longer describes this tree, or the git "
        "listing is not reaching src/ui."
    )
    assert _collected(), (
        "the effective globs resolve to no existing file, so the UI suite "
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
        f"include/exclude in effect for the Vitest suite:\n  "
        + "\n  ".join(missed)
        + f"\n\neffective include: {_effective_globs('include')}"
        + f"\neffective exclude: {_effective_globs('exclude')}"
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
    excluded = sorted(_test_files_in_the_tree() & _resolve(_effective_globs("exclude")))
    assert not excluded, (
        "these test files are matched by an `exclude` pattern in effect for the "
        "Vitest suite and will not run:\n  " + "\n  ".join(excluded)
    )


def test_no_config_file_outranks_the_one_this_module_reads() -> None:
    """Vitest prefers ``vitest.config.*`` to ``vite.config.*``.

    So adding one beside it silently replaces every pattern asserted above — the
    second of the five escapes, and the same shape as
    ``test_first_party_pythonpath.test_makefile_is_the_file_a_recipe_would_actually_read``:
    a check is faithful only while the file it reads is the file the tool loads.

    The directory is listed rather than probed by name, because ``Path.exists()``
    is case-insensitive on macOS and would answer True for ``vite.config.js``
    itself.
    """
    present = sorted(
        name for name in os.listdir(UI_ROOT) if name in OUTRANKING_CONFIG_NAMES
    )
    assert not present, (
        f"{present} sit beside {CONFIG.name} and Vitest loads one of them in "
        "preference to it, so the include/exclude this module asserts are not the "
        "ones the suite will use. Put the test configuration in the file Vitest "
        "will actually read."
    )


def _ci_vitest_invocations() -> list[tuple[str, str]]:
    """``(config name, line)`` for every uncommented ``npx vitest run`` in both CIs."""
    found: list[tuple[str, str]] = []
    for path in (GITLAB_CI, GITHUB_CI):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().lstrip("-").strip()
            if stripped.startswith("#"):
                continue
            if "npx vitest run" in stripped:
                found.append((path.name, stripped))
    return found


def test_neither_ci_narrows_discovery_at_the_call_site() -> None:
    """The fifth escape: ``--config narrow.config.js`` on the step itself.

    Everything above is about a file in the tree, and a CLI option beats the file.
    ``scripts/tests/test_ci_gate_parity.py`` requires the substring ``npx vitest
    run`` to appear in both configs, which an invocation carrying its own config
    file satisfies; discovery is then decided at the call site, where nothing was
    looking.

    Both forms are refused: an option from :data:`DISCOVERY_NARROWING_OPTIONS`, and
    a bare positional argument, which Vitest reads as a filename filter.
    """
    invocations = _ci_vitest_invocations()
    assert len(invocations) >= 2, (
        f"expected an `npx vitest run` step in both CI configs, found "
        f"{len(invocations)}: {invocations}. Either the UI suite has stopped "
        "running in one of them — which test_ci_gate_parity.py is the gate for — "
        "or it is now invoked in a form this check cannot see."
    )
    offenders: list[str] = []
    for name, line in invocations:
        _, _, after = line.partition("npx vitest run")
        for word in after.split("&&")[0].split():
            if word in DISCOVERY_NARROWING_OPTIONS or any(
                word.startswith(f"{option}=") for option in DISCOVERY_NARROWING_OPTIONS
            ):
                offenders.append(f"{name}: {word} in `{line}`")
            elif not word.startswith("-"):
                offenders.append(f"{name}: positional filter {word!r} in `{line}`")
    assert not offenders, (
        "these CI invocations narrow which files Vitest discovers, so the suite "
        "runs over a set no assertion in this repository describes:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# calibration against Vitest itself, where Node is available
# --------------------------------------------------------------------------- #
def _node(script: str) -> str:
    _require_node()
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["node", "-e", script],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"node probe failed: {result.stderr[-2000:]}"
    return result.stdout.strip()


def _require_node() -> None:
    if shutil.which("node") is None or not (NODE_MODULES / "vitest").is_dir():
        pytest.skip(
            "src/ui/node_modules/vitest or node is absent, so Vitest cannot be "
            "consulted. This skips the CALIBRATION of the literals and the glob "
            "expansion in this module — not the gate, whose assertions above run "
            "with no Node at all. Neither CI's package-test job installs the UI's "
            "npm dependencies, so these three skip there and run in `ui_tests` "
            "and locally."
        )


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


def test_the_collected_set_is_the_one_vitest_reports() -> None:
    """Ask Vitest, rather than agreeing with a second implementation of it.

    The gate above rests on a parse of the config and a re-implementation of
    pattern matching, both written here because the suite it lives in cannot count
    on Node. ``vitest list --filesOnly`` is Vitest's own answer to the same
    question — through its own config resolution, its own comment handling and its
    own matcher — so where Node is available the two are required to agree exactly.
    This is what demotes the parse from a claim to an implementation detail, and
    what catches a config shape the parse reads differently from Vitest.
    """
    _require_node()
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["npx", "vitest", "list", "--filesOnly"],
        cwd=str(UI_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"`npx vitest list --filesOnly` failed: {result.stderr[-3000:]}"
    )
    reported = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith(" ")
    }
    assert reported, (
        "`vitest list --filesOnly` printed no file, so there is nothing to "
        f"compare against. stdout was {result.stdout[:500]!r}"
    )
    assert reported == _collected(), (
        "Vitest and this module disagree about which files the UI suite "
        "collects, so the gate above is measuring something other than what will "
        "run:\n"
        f"  only Vitest: {sorted(reported - _collected())}\n"
        f"  only here  : {sorted(_collected() - reported)}"
    )
