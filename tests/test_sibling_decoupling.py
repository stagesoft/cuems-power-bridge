# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The platform package holds none of this package's Python any more.

Feature 002's other half lives in `../cuems-common`. These tests read that
sibling checkout when it is present and skip when it is not, so the guarantee
is checked wherever both repositories are available — which is every
development box and the build host — without making the suite depend on it.

What they pin is not style but the two properties the move was for: no import
of ours over there, and one lock spanning both stages rather than one per
stage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SIBLING = Path(__file__).resolve().parents[2] / "cuems-common"
POWEROFF = SIBLING / "usr" / "bin" / "cuems-cluster-poweroff"
DISPLAYS_ON = SIBLING / "usr" / "bin" / "cuems-displays-on"

pytestmark = pytest.mark.skipif(
    not POWEROFF.is_file(), reason="../cuems-common checkout not present")


def test_the_poweroff_script_holds_no_python_of_ours():
    """Any `from cuemspowerbridge import …` over there is a heredoc that came
    back — the boundary is argv and exit codes now."""
    src = POWEROFF.read_text()
    assert "from cuemspowerbridge" not in src
    assert "import cuemspowerbridge" not in src
    assert "<<'PY'" not in src and '<<"PY"' not in src


def test_displays_on_holds_no_python_of_ours():
    src = DISPLAYS_ON.read_text()
    assert "cuemspowerbridge" not in src
    assert "cuems-power-bridge-config --get" in src      # the query replaced it


def test_the_helper_is_invoked_as_a_module_through_the_configured_interpreter():
    """Not a PATH command: that is what keeps the conffile's override and the
    missing-package guard working, and what leaves exactly one command that
    powers the venue down."""
    src = POWEROFF.read_text()
    assert '"$venv_python" -u -m cuemspowerbridge.scripts.cluster_poweroff' in src
    assert 'cuems-power-bridge-cluster-poweroff' not in src


def test_one_lock_spans_both_stages():
    """T023a. Two stages are two processes; a lock taken per process would
    leave the gap between them open for another power-off to start in."""
    src = POWEROFF.read_text()
    acquire = src.index("flock -n 9")
    first_stage = src.index("--stage displays")
    second_stage = src.index("--stage nodes")
    assert acquire < first_stage < second_stage, "the lock must be taken before both stages"
    # …and never released in between: fd 9 stays open until the script exits.
    between = src[first_stage:second_stage]
    assert "flock -u" not in between and "exec 9>&-" not in between
    # both stages are told the caller holds it (invocations, not prose)
    invocations = [l for l in src.splitlines()
                   if "--lock-held" in l and not l.lstrip().startswith("#")]
    assert len(invocations) == 2, invocations


def test_the_wrapper_keeps_its_own_guards():
    """The usage logic is the wrapper's, and the move must not have taken it."""
    src = POWEROFF.read_text()
    for guard in ("enabled", "reboot.target", "poweroff.target",
                  "systemctl stop cuems-displays-on.service", "nodes_off",
                  "ordering probe", "venv_python"):
        assert guard in src, f"the wrapper lost its {guard!r} guard"


def test_the_transition_path_never_requests_the_running_show_guard():
    """The product must always be able to power the venue off mid-show."""
    src = POWEROFF.read_text()
    # the guard is assembled only under --force
    assert 'if [ "$force" = yes ]; then\n    guard_args="--refuse-if-running"' in src
    # and nothing else ever passes it
    passes = [l for l in src.splitlines()
              if "--refuse-if-running" in l and not l.lstrip().startswith("#")]
    assert len(passes) == 1, passes


def test_a_transaction_still_exits_zero_when_a_stage_reports_failure():
    """An ExecStop that fails would fail the stop it belongs to."""
    src = POWEROFF.read_text()
    assert "explain()" in src
    # the only non-zero exits are the manual path and argument errors
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("exit ") and stripped not in ("exit 0",):
            assert "force" in src[max(0, src.index(line) - 400):src.index(line)], (
                f"unexpected non-zero exit on a transaction path: {stripped}")
