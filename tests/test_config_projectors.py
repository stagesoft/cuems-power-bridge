# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Config coercion/validation for the projector fields."""

import pytest

from cuemspowerbridge.config import Config, _parse


def test_fractional_timeout_parses_as_float():
    # Regression: a float field must accept "1.5" (int() would crash).
    cfg = Config()
    _parse("projector_command_timeout_s = 1.5\n", cfg)
    assert cfg.projector_command_timeout_s == 1.5


def test_integer_timeout_still_parses():
    cfg = Config()
    _parse("projector_command_timeout_s = 8\n", cfg)
    assert cfg.projector_command_timeout_s == 8.0


def test_validate_rejects_nonpositive_timeout():
    cfg = Config()
    cfg.projector_command_timeout_s = 0
    with pytest.raises(ValueError):
        cfg.validate()


def test_projector_keys_land_in_extras():
    cfg = Config()
    _parse("projector.1.host = 10.0.0.1\nprojector.1.name = Main\n", cfg)
    assert cfg.extras["projector.1.host"] == "10.0.0.1"
    assert cfg.extras["projector.1.name"] == "Main"
