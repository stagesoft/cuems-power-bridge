# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Display-device power control subpackage.

A "display device" is anything the bridge powers on/off over the network:
a projector (PJLink / Epson ESC-VP), an HDMI monitor (CEC), etc. Each
control protocol is a `DisplayDriver`; `DisplayManager` fans commands out
across the configured fleet. Phase 1 ships the PJLink driver only — see
`pjlink.py` — but the registry in `manager.py` is the single extension
point for adding more protocols later.
"""
