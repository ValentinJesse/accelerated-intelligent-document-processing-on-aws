#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Produce one coverage report per measurable tree, for the ratchet to read.

One invocation per tree, not one over the repository. Several packages ship their own
``tests/conftest.py`` and pytest imports them all as the module ``tests.conftest``, so a
single run across two roots is an import collision —
:mod:`scripts.run_all_tests` shells out per root for the same reason.

Each tree's report lands at ``<tree>/test-reports/coverage-<name>.xml``, which is where
``scripts/check_coverage_debt.py`` looks for it, with the run's JUnit XML beside it at
``coverage-<name>-results.xml``. The ratchet compares a report only when that record says
the run finished: a run whose xdist workers errored writes a well-formed coverage report
whose numbers are an artefact, and nothing inside the report itself says so.

Several of the files these suites cover are executed as **subprocesses** — a gate script
run the way a developer runs it, a git hook run the way git runs it — and the coverage of
a subprocess is collected only if that process starts coverage for itself.
:func:`subprocess_coverage_env` is the whole of that wiring, and every pytest invocation
this script makes goes through it.

⚠️ **The figures are only reproducible if the AWS environment is neutralised**, so every
invocation goes through :func:`hermetic_aws_env` as well. A suite that can resolve real
credentials measures different lines from one that cannot, and because botocore caches the
credentials it first resolved onto a process-global session, *which test ran first in a
worker process* decides the answer — so the figure moved run to run at a fixed worker
count on a developer machine and not at all on a runner, which has nothing to resolve.
That reasoning is written out at :data:`HERMETIC_AWS_UNSET`, with the measurement.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "check_coverage_debt", REPO_ROOT / "scripts" / "check_coverage_debt.py"
)
assert _spec and _spec.loader
_ccd = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage_debt"] = _ccd
_spec.loader.exec_module(_ccd)


#: The configuration a subprocess started by a measured suite runs coverage under.
#:
#: Named so that nothing discovers it implicitly: `coverage` and `pytest-cov` search for
#: `.coveragerc`, `setup.cfg`, `tox.ini` and `pyproject.toml`, and this repository has
#: coverage settings in none of them — every option in force is one a command line spells
#: out. Keeping that true means this file is reached only through the environment variable
#: below.
SUBPROCESS_RC = REPO_ROOT / "scripts" / "coverage_subprocess.rc"

#: Every AWS environment variable :data:`HERMETIC_AWS` removes, and the three it pins.
#:
#: This is the one pytest invocation in the repository that did not go through
#: `$(PYTEST_HERMETIC)`, because it is made from Python rather than from a `make` recipe,
#: and `scripts/tests/test_offline_suites_are_hermetic.py` scopes its "every line goes
#: through the wrapper" check to the `test-packages-cicd` recipe — so this caller sat
#: outside the universe of the control rather than being carved out of it. Nobody decided
#: it should be unprotected; the question was never asked about it.
#:
#: **Leaving it out made the coverage figures it produces nondeterministic**, which is
#: worse here than a failing suite would be, because the figures are a ratchet's input and
#: `--write` records them permanently. `boto3.DEFAULT_SESSION` is created by the first bare
#: `boto3.client(...)` in a process and **caches the credentials it resolved**, so a
#: `monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")` applied after that point is ignored
#: by every later client in the same process. Whether a `try: boto3...get_caller_identity()
#: / except:` branch is covered therefore depends on which test ran *first in its process*
#: — and xdist's `--dist load` partition of tests across workers varies with the worker
#: count and run to run. Measured on `idp_feature_sdk` (461 tests, 0 errors, 0 skips in
#: every cell): `pack.py` at 99.44% or 100.00% from one run to the next at a fixed `-n 4`,
#: and under this neutralisation 100.00% at every worker count tried. Issue #1377.
#:
#: Spelled out here and **derived from the makefile in the test**, not parsed at run time:
#: `test_offline_suites_are_hermetic.py` reads `HERMETIC_AWS` out of
#: `make/hermetic_aws.mk` and fails if this mapping stops covering it, so the two cannot
#: drift while both stay green.
HERMETIC_AWS_UNSET = (
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
)

#: The sources no amount of unsetting reaches: the shared config files, and IMDS.
HERMETIC_AWS_SET = {
    "AWS_CONFIG_FILE": "/dev/null",
    "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    "AWS_EC2_METADATA_DISABLED": "true",
}


def hermetic_aws_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with the ambient AWS environment neutralised, as ``HERMETIC_AWS`` does.

    Mutates and returns ``env`` so it composes with
    :func:`subprocess_coverage_env` in either order.
    """
    for name in HERMETIC_AWS_UNSET:
        env.pop(name, None)
    env.update(HERMETIC_AWS_SET)
    return env


def subprocess_coverage_env(
    data_dir: Path, source_dir: Path, env: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment a pytest invocation needs for its subprocesses to be measured.

    A suite that runs the code it covers as a **subprocess** — `scripts/hooks/` run the
    way git runs them, a gate script run the way a developer runs it — measures nothing
    about that code unless the subprocess starts coverage itself. What starts it is the
    `.pth` file `coverage` installs into `site-packages`: it runs
    `coverage.process_startup()` in every Python process, and that call returns
    immediately unless ``COVERAGE_PROCESS_START`` names a configuration file. Nothing
    else supplies it. `pytest-cov` used to, through a bootstrap of its own
    (``COV_CORE_SOURCE``), and no longer ships one — so the collection is this
    repository's to arrange, at the point where it asks for coverage.

    Three variables are load-bearing and they fail differently:

    * ``COVERAGE_PROCESS_START`` decides *whether* the subprocess measures anything.
    * ``COVERAGE_FILE`` decides whether what it measured is *found*. The data file is
      resolved against the writing process's own working directory, so a subprocess
      started with ``cwd=tmp_path`` writes its data into a directory that is deleted
      before anything combines it. Pointing both the parent and its children at one
      absolute path is what makes the location independent of where a test chose to run.
    * ``COVERAGE_SUBPROCESS_SOURCE`` decides *what* it measures, and the tree's own source
      directory is the answer. `process_startup` builds its Coverage from the rc file
      alone, so a child given no source measures every file it imports, the parent's
      `combine()` merges all of it, and the report grows from one tree to whatever the
      children touched. Measured over the **whole** `scripts` tree (4,906 tests): 1,189
      files and a whole-tree total of 31.52%, against 51 files and 82% with the bound. One
      test file does not show it -- the children of most of these suites import nothing
      outside the tree -- which is why the probe in
      `scripts/tests/test_coverage_all.py` makes its children import a module that is
      outside it on purpose. A ratchet cannot use such a report, and `--write` would record
      every one of those files. Like the data file it has to be **absolute**, which `run`
      guarantees by deriving it from `REPO_ROOT` rather than from `tree.cwd`; the
      `resolve()` below only normalises what it is given.

    Measured on a probe whose only execution is a subprocess with a `cwd` of its own:
    0.00% with neither of the first two variables, 0.00% with ``COVERAGE_PROCESS_START``
    alone, 100.00% with both. `scripts/tests/test_coverage_all.py` runs that probe against
    this function rather than asserting that the variables are spelled a particular way.
    """
    out = dict(os.environ if env is None else env)
    out["COVERAGE_PROCESS_START"] = str(SUBPROCESS_RC)
    out["COVERAGE_FILE"] = str(Path(data_dir).resolve() / ".coverage")
    out["COVERAGE_SUBPROCESS_SOURCE"] = str(Path(source_dir).resolve())
    return out


def run(tree, python: str, parallel: bool) -> tuple[str, int, float | None]:
    report = _ccd.report_path(tree)
    report.parent.mkdir(parents=True, exist_ok=True)
    # The JUnit XML is what lets the ratchet tell a finished run from one whose workers
    # errored: a coverage report carries no record of its own session, so without this
    # the ratchet has to treat every report here as unverifiable. Its path is derived
    # from the report's, never spelled out twice -- `check_coverage_debt.run_record_path`
    # is the single answer to where it goes.
    record = _ccd.run_record_path(report)
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if parallel:
        cmd += ["-n", "auto"]
    cmd += [
        f"--cov={tree.cov}",
        f"--cov-report=xml:{report}",
        "--cov-report=",
        f"--junitxml={record}",
        *tree.args,
    ]
    cwd = REPO_ROOT / tree.cwd
    # What the children measure has to be the same tree the parent measures, spelled
    # absolutely: `--cov=<tree.cov>` is resolved by pytest-cov against this cwd, and a
    # child does not have this cwd.
    source = cwd if tree.cov == "." else cwd / tree.cov
    result = subprocess.run(
        cmd, cwd=cwd, env=hermetic_aws_env(subprocess_coverage_env(cwd, source))
    )
    total = None
    if report.is_file():
        try:
            total = _ccd.report_total(report)
        except Exception:  # pragma: no cover - malformed report
            total = None
    # 5 == no tests collected for the marker, which is not a failure of this script.
    return tree.name, (0 if result.returncode in (0, 5) else result.returncode), total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", action="append", help="measure just this tree (repeatable)"
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="do not pass -n auto for ANY tree (trees that declare serial are always serial)",
    )
    args = parser.parse_args()

    python = os.environ.get("PYTHON") or sys.executable
    trees = _ccd.TREES
    if args.only:
        missing = [n for n in args.only if n not in _ccd.TREES_BY_NAME]
        if missing:
            print(f"unknown tree(s): {missing}. Known: {[t.name for t in trees]}")
            return 2
        trees = tuple(_ccd.TREES_BY_NAME[n] for n in args.only)

    failures, results = [], []
    for tree in trees:
        print(
            f"\n=== coverage: {tree.name} ({tree.cwd}, --cov={tree.cov}) ===",
            flush=True,
        )
        # A tree that declares `serial` is never run in parallel, whatever the flag
        # says: for those, `-n auto` does not just cost time, it reports coverage that is
        # wrong in a way nothing downstream can detect. `--serial` can force the rest
        # serial too, but it cannot force a serial tree parallel.
        parallel = not args.serial and not tree.serial
        name, code, total = run(tree, python, parallel=parallel)
        results.append((name, total))
        if code:
            failures.append(name)

    print("\n" + "=" * 62)
    for name, total in results:
        print(
            f"  {name:<26} {total:>6.2f}%"
            if total is not None
            else f"  {name:<26}  (no report)"
        )
    if failures:
        print(
            f"\n⚠️  tests failed in: {', '.join(failures)} — the reports above may be partial"
        )
    print("\nNow run: python3 scripts/check_coverage_debt.py")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
