# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""install_shelly_mjs._patched_code: literals patched, drift guarded, ASCII.

Guards the fragile string-replace patching against template drift — the only
logic in the installer that can silently break and would otherwise surface
only when flashing a real Shelly.
"""

import pytest

from cuemspowerbridge.scripts.install_shelly_mjs import (
    DEFAULT_GRACE_S,
    _load_template,
    _patched_code,
)


def test_bundled_template_patches_all_five():
    code = _patched_code(_load_template(None), "http://10.16.8.2:8478", "secret",
                         toggle_id=201, force=False, cancel_grace=8)
    assert 'let BRIDGE = "http://10.16.8.2:8478";' in code
    assert 'let TOKEN  = "secret";' in code
    assert "let TOGGLE_ID = 201;" in code
    assert "let FORCE  = false;" in code
    assert "let CANCEL_GRACE_S = 8;" in code
    assert "let CANCEL_GRACE_S = 5;" not in code


def test_force_default_literal_kept_when_force():
    # FORCE=true is the template default; --safe is what patches it to false.
    code = _patched_code(_load_template(None), "http://x:8478", "", 200, force=True)
    assert "let FORCE  = true;" in code


def test_default_grace_single_sourced_with_template():
    # The bundled template's literal must equal DEFAULT_GRACE_S, else every
    # default-value install would raise on the pre-replace guard.
    t = _load_template(None)
    assert f"let CANCEL_GRACE_S = {DEFAULT_GRACE_S};" in t
    code = _patched_code(t, "http://x:8478", "", 200, True)  # default grace -> clean no-op
    assert f"let CANCEL_GRACE_S = {DEFAULT_GRACE_S};" in code


def test_patched_output_is_ascii():
    code = _patched_code(_load_template(None), "http://x:8478", "tok", 200, True, 3)
    assert all(ord(c) < 128 for c in code)


def test_token_literal_drift_raises():
    # TOKEN is now guarded too (was the asymmetric gap). Two-space literal.
    drifted = _load_template(None).replace('let TOKEN  = "REPLACE-ME";',
                                           'let TOKEN = "REPLACE-ME";')
    with pytest.raises(RuntimeError):
        _patched_code(drifted, "http://x:8478", "tok", 200, True)


def test_cancel_grace_literal_drift_raises():
    drifted = _load_template(None).replace(
        f"let CANCEL_GRACE_S = {DEFAULT_GRACE_S};", "let CANCEL_GRACE_S = 99;")
    with pytest.raises(RuntimeError):
        _patched_code(drifted, "http://x:8478", "tok", 200, True)  # default grace, literal gone


def test_toggle_id_literal_drift_raises():
    drifted = _load_template(None).replace("let TOGGLE_ID = 200;",
                                           "let TOGGLE_ID=200;")
    with pytest.raises(RuntimeError):
        _patched_code(drifted, "http://x:8478", "tok", 200, True)


def test_force_literal_drift_raises():
    # Two-space literal, like TOKEN. A reformat must not ship FORCE unpatched
    # (a --safe install silently left at FORCE=true would kill a running show).
    drifted = _load_template(None).replace("let FORCE  = true;", "let FORCE = true;")
    with pytest.raises(RuntimeError):
        _patched_code(drifted, "http://x:8478", "tok", 200, False)


def test_non_ascii_template_raises():
    bad = _load_template(None) + "\n// stray em-dash — here\n"
    with pytest.raises(RuntimeError):
        _patched_code(bad, "http://x:8478", "tok", 200, True)
