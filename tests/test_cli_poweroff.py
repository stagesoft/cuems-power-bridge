# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The internal power-off helper: exit codes, and the guard that must never
reach a power-off transaction.

The invariant under test throughout: **the product must always be able to
power the venue off mid-show.** The running-show guard exists for manual
rehearsals, is *requested* by the caller, and fails open in every case where
it cannot know.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cuemspowerbridge.scripts import cluster_poweroff as cli

MODULE = Path("src/cuemspowerbridge/scripts/cluster_poweroff.py")


class _Args:
    """Only what the guard reads."""

    def __init__(self, **kw):
        self.refuse_if_running = False
        self.while_playing = False
        self.dry_run = False
        self.bridge_url = "http://127.0.0.1:8478"
        self.__dict__.update(kw)


# --------------------------------------------------------------------------
# The guard's truth table (FR-015, FR-015b)
# --------------------------------------------------------------------------


def test_a_transaction_never_asks(monkeypatch):
    """Not requested ⇒ no probe at all. This is the product path: the wall
    switch, the power button, systemctl poweroff."""
    def explode(*a, **kw):
        raise AssertionError("the transition path must never probe the daemon")

    monkeypatch.setattr(cli, "_project_is_playing", explode)
    assert cli._guard(_Args(refuse_if_running=False)) is None


def test_requested_and_playing_refuses(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_project_is_playing", lambda url, timeout=3.0: True)
    assert cli._guard(_Args(refuse_if_running=True)) == cli.EXIT_REFUSED
    assert "REFUSED" in capsys.readouterr().out


def test_the_override_proceeds_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_project_is_playing", lambda url, timeout=3.0: True)
    assert cli._guard(_Args(refuse_if_running=True, while_playing=True)) is None
    out = capsys.readouterr().out
    assert "PLAYING" in out and "proceeding" in out       # recorded in the journal


def test_requested_and_idle_proceeds(monkeypatch):
    monkeypatch.setattr(cli, "_project_is_playing", lambda url, timeout=3.0: False)
    assert cli._guard(_Args(refuse_if_running=True)) is None


def test_an_unreachable_daemon_fails_open(monkeypatch, capsys):
    """The same "cannot know" the transition path lives in permanently."""
    monkeypatch.setattr(cli, "_project_is_playing", lambda url, timeout=3.0: None)
    assert cli._guard(_Args(refuse_if_running=True)) is None
    assert "WARNING" in capsys.readouterr().out


def test_an_unknown_engine_state_fails_open(monkeypatch):
    """A wedged engine is the recovery case the manual run exists for;
    refusing there would disable the tool exactly when it is needed."""
    import json as _json

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"engine_state": "unknown"}).encode()

    monkeypatch.setattr(cli, "urlopen", lambda *a, **kw: _R())
    assert cli._project_is_playing("http://x") is None


def test_a_rehearsal_skips_the_guard(monkeypatch, capsys):
    def explode(*a, **kw):
        raise AssertionError("a dry run cannot harm a show; do not probe")

    monkeypatch.setattr(cli, "_project_is_playing", explode)
    assert cli._guard(_Args(refuse_if_running=True, dry_run=True)) is None
    assert "dry run" in capsys.readouterr().out


def test_the_probe_reads_engine_state(monkeypatch):
    import json as _json

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"engine_state": "running"}).encode()

    monkeypatch.setattr(cli, "urlopen", lambda *a, **kw: _R())
    assert cli._project_is_playing("http://x") is True


def test_a_probe_failure_is_not_a_refusal(monkeypatch):
    def boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "urlopen", boom)
    assert cli._project_is_playing("http://x") is None


# --------------------------------------------------------------------------
# The exit-code contract, and what the helper may not contain
# --------------------------------------------------------------------------


def test_exit_codes_are_the_documented_ones():
    assert (cli.EXIT_OK, cli.EXIT_USAGE, cli.EXIT_PRECONDITION,
            cli.EXIT_REFUSED, cli.EXIT_STUCK) == (0, 1, 3, 4, 5)


def test_the_helper_never_arms_the_relay_or_powers_this_host_off():
    """FR-001a — it runs during a transition the machine is already
    committed to; systemd finishes the job."""
    tree = ast.parse(MODULE.read_text())
    toks = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    toks |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for forbidden in ("shelly", "arm_timer", "controller_poweroff_cmd",
                      "create_subprocess_exec"):
        assert forbidden not in toks


def test_force_is_not_a_flag_here():
    """The word already means "run outside a poweroff transaction" in the
    wrapper and "ignore a running project, include unadopted" on the HTTP
    route. A third meaning would be indefensible."""
    src = MODULE.read_text()
    assert '"--force"' not in src and "'--force'" not in src


@pytest.mark.parametrize("flag", [
    "--stage", "--node-wait", "--projector-timeout", "--include-unadopted",
    "--dry-run", "--pre-pass", "--lock-held", "--refuse-if-running",
    "--while-playing",
])
def test_every_contracted_flag_exists(flag):
    assert f'"{flag}"' in MODULE.read_text()


def test_the_docstring_states_the_exit_contract_and_that_it_is_internal():
    doc = ast.get_docstring(ast.parse(MODULE.read_text())) or ""
    assert "Not an operator command" in doc
    for code in ("0", "1", "3", "4", "5"):
        assert f"\n{code}   " in doc or f"\n{code}    " in doc


# --------------------------------------------------------------------------
# The configuration query (FR-016)
# --------------------------------------------------------------------------


def _config_get(argv, monkeypatch):
    """Run the query's main() and return (exit code, stdout)."""
    import io
    import sys as _sys

    from cuemspowerbridge.scripts import config_get

    monkeypatch.setattr(_sys, "argv", ["cuems-power-bridge-config", *argv])
    out = io.StringIO()
    monkeypatch.setattr(_sys, "stdout", out)
    try:
        config_get.main()
    except SystemExit as e:
        return (e.code or 0), out.getvalue()
    return 0, out.getvalue()


def test_the_query_prints_a_bare_value(monkeypatch):
    code, out = _config_get(["--get", "projector_power_on_on_start"], monkeypatch)
    assert code == 0 and out.strip() in ("true", "false")


def test_an_unknown_key_is_an_error(monkeypatch):
    code, _ = _config_get(["--get", "not_a_key"], monkeypatch)
    assert code == 3


def test_an_explicit_missing_config_is_an_error(monkeypatch):
    """Answering with a default would tell the caller the value it asked
    about is set to something it is not."""
    code, _ = _config_get(["--get", "shared_token", "--config", "/nonexistent"], monkeypatch)
    assert code == 3


def test_an_empty_value_is_not_an_error(monkeypatch):
    """`cuems-displays-on` branches on the empty string to decide whether to
    send an auth header at all."""
    code, out = _config_get(["--get", "shared_token"], monkeypatch)
    assert code == 0 and out.endswith("\n")


def test_the_key_is_the_argument_never_the_value():
    """A token must not reach argv, where ps would show it."""
    src = Path("src/cuemspowerbridge/scripts/config_get.py").read_text()
    assert "--get" in src and "--set" not in src
