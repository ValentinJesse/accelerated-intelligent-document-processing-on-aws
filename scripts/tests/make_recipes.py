# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read the command a ``make`` target would run, and run a probe through it.

Two test modules assert things about what ``lib/idp_common_pkg``'s pytest recipes
hand the interpreter — one about the AWS environment they remove
(``test_offline_suites_are_hermetic.py``), one about the ``PYTHONPATH`` they pin
(``test_first_party_pythonpath.py``). Both questions are about the *expanded*
command rather than about the text of the recipe, and that distinction is the
reason this helper exists rather than another substring search.

A recipe line is a spelling. ``$(PYTEST_FIRST_PARTY) -m "integration"`` and a
hand-written ``env PYTHONPATH=lib/idp_common_pkg python3 -m pytest`` read
identically to a check that looks for the wrapper's name, and only one of them
pins the checkout. ``make --dry-run`` answers the question the assertion is
actually about: what will run. Then :func:`probe_command` keeps everything up to
the ``-m pytest`` and substitutes an interpreter one-liner for the suite, so the
*environment* that command establishes can be measured without running the tests
it would run.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

#: The spelling both Makefiles use to start pytest. :func:`probe_command` splits
#: the command here, so a recipe that invoked the ``pytest`` console script
#: instead would reach a loud assertion rather than a silently skipped probe.
PYTEST_MARKER = "-m pytest"


def dry_run_command(target: str, directory: Path) -> str:
    """The command ``make <target>`` would run, as ``make`` itself expands it.

    ``--dry-run`` prints the recipe without executing it, which matters here: the
    targets this is used on run test suites, one of them against real AWS.

    The last non-empty line is taken, so a target whose recipe starts with a
    conditional install step (``test-unit-cicd``) still yields its pytest line.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["make", "--no-print-directory", "--dry-run", target],
        cwd=str(directory),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"`make --dry-run {target}` failed in {directory}: {result.stderr}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"`make --dry-run {target}` printed no command in {directory}"
    return lines[-1]


def probe_command(command: str, code: str) -> str:
    """``command`` with its pytest invocation replaced by ``python -c code``.

    Everything before ``-m pytest`` is the part under test — the ``env``
    manipulation, the variable assignments and the interpreter — and it is kept
    verbatim, including shell parameter expansions, which is why callers run the
    result through ``sh -c`` rather than as an argv.
    """
    assert PYTEST_MARKER in command, (
        f"the recipe's command does not contain {PYTEST_MARKER!r}, so this probe "
        f"cannot tell which part of it is the environment and which is the suite:\n"
        f"  {command}\n"
        "If the recipe now starts pytest some other way, update PYTEST_MARKER "
        "deliberately — do not delete the assertion, which is what this failing "
        "loudly instead of skipping is for."
    )
    prefix = command[: command.index(PYTEST_MARKER)]
    return f"{prefix} -c {shlex.quote(code)}"


def run_probe(
    command: str, code: str, *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run :func:`probe_command` under ``sh``, returning the completed process."""
    return subprocess.run(  # noqa: S603 - argv is fixed; the text comes from our makefile
        ["sh", "-c", probe_command(command, code)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        env=env,
        timeout=120,
    )
