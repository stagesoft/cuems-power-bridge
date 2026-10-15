# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Relay fixture format, and the generator for the hand-built MODELS.

A fixture is a sequence of records, each:
    kind   1 byte   b"F" engine frame | b"C" WS connect | b"D" WS disconnect
    t_ms   uint32 BE, ms since the start of the capture
    length uint32 BE
    data   `length` bytes (the raw WS binary frame for "F", empty otherwise)

`model_*.bin` are hand-built from the engine code (ControllerEngine.py at tag
v0.1.0rc6-3 for the released order, origin/rc_1 2026-10-15 for the rc_1
order: load_project, _clear_playback_state, stop_script,
_on_ws_client_connect). They are MODELS, not captures. Captures from a live
engine (`capture_*.bin`, plan step R1) use the same format.

`capture_rc1_111.bin` is the live R1 capture from .111 (2026-10-15; engine
hot-patched to the rc_1 load order): unload video-test, load audio-test, GO,
natural end of its one AudioCue (b3c23544…), STOP, GO, STOP mid-play. Every
frame is byte-for-byte as the engine sent it, except that the once-a-second
`timecode` frames were removed and gaps longer than 3 s were shortened to 3 s
(the natural end came 224 s after GO), so the replay takes seconds. The full
capture is kept in cuems-RELATIONS/baselines/companion-relay/r1/.

Regenerate the models with:  python3 tests/fixtures/relay/relayfix.py
"""

from __future__ import annotations

import struct
from pathlib import Path

from pythonosc.osc_message_builder import OscMessageBuilder

HERE = Path(__file__).resolve().parent
S = "/engine/status/"
X1 = "11111111-1111-4111-8111-111111111111"   # show_x only
SH = "22222222-2222-4222-8222-222222222222"   # in both shows
Y1 = "33333333-3333-4333-8333-333333333333"   # show_y only


def osc(address, value):
    b = OscMessageBuilder(address)
    b.add_arg(value)
    return b.build().dgram


def read(path) -> list[tuple[str, int, bytes]]:
    data = Path(path).read_bytes()
    out, i = [], 0
    while i < len(data):
        kind = data[i:i + 1].decode()
        t_ms, n = struct.unpack(">II", data[i + 1:i + 9])
        out.append((kind, t_ms, data[i + 9:i + 9 + n]))
        i += 9 + n
    return out


class _W:
    def __init__(self):
        self.t = 0
        self.recs: list[tuple[str, int, bytes]] = []

    def wait(self, ms):
        self.t += ms

    def c(self):
        self.recs.append(("C", self.t, b""))

    def d(self):
        self.recs.append(("D", self.t, b""))

    def f(self, key, value):
        self.recs.append(("F", self.t, osc(S + key, value)))
        self.t += 1

    def dump(self, running, armed, load, nextcue, cues=None, enabled=None, names=None):
        for k, v in (("running", running), ("armed", armed), ("load", load),
                     ("nextcue", nextcue)):
            self.f(k, v)
        self.f("cluster_warning", '{"load_id": 0, "missing": [], "unreachable": []}')
        for u, v in (cues or {}).items():
            self.f(f"cue/{u}", v)
        for u, v in (enabled or {}).items():
            self.f(f"cue_enabled/{u}", v)
        for u, v in (names or {}).items():
            self.f(f"cue_name/{u}", v)

    def load(self, name, cues, *, rc1, names=True):
        self.f("timecode", 0)
        self.f("armed", "no")
        self.f("nextcue", "")
        if rc1:
            self.f("load", "")
            self.wait(300)
        for u in cues:
            self.f(f"cue/{u}", 0)
        for u in cues:
            self.f(f"cue_enabled/{u}", 1)
        self.f("load", name)
        if names:
            for i, u in enumerate(cues):
                self.f(f"cue_name/{u}", f"Cue {i} ñ")
        self.wait(500)
        self.f("armed", "yes")
        self.f("nextcue", cues[0])

    def play_through(self, cues):
        self.f("running", "yes")
        for u in cues:
            self.wait(100)
            self.f(f"cue/{u}", 1)
            self.wait(400)
            self.f(f"cue/{u}", 100)

    def stop(self, cues):
        self.f("running", "no")
        self.f("timecode", 0)
        self.f("armed", "no")
        self.f("nextcue", "")
        for u in cues:
            self.f(f"cue/{u}", 0)
        self.wait(300)
        self.f("armed", "yes")

    def write(self, name):
        out = bytearray()
        for kind, t, data in self.recs:
            out += kind.encode() + struct.pack(">II", t, len(data)) + data
        (HERE / name).write_bytes(bytes(out))


def model_released_show():
    """<= rc6-3: idle engine, load show_x, play it through, STOP, switch
    straight to show_y (no unload), unload."""
    w = _W()
    w.c()
    w.dump("no", "no", "", "")
    w.wait(2500)                                 # past the edge window
    w.load("show_x", [X1, SH], rc1=False)
    w.wait(200)
    w.play_through([X1, SH])
    w.wait(200)
    w.stop([X1, SH])
    w.wait(200)
    w.load("show_y", [SH, Y1], rc1=False)        # direct X -> Y
    w.wait(200)
    w.f("load", "")                              # unload
    w.write("model_released_show.bin")


def model_rc1_show():
    """rc_1: same story with the head `load ""` on every load."""
    w = _W()
    w.c()
    w.dump("no", "no", "", "")
    w.wait(2500)
    w.load("show_x", [X1, SH], rc1=True)
    w.wait(200)
    w.play_through([X1, SH])
    w.wait(200)
    w.stop([X1, SH])
    w.wait(200)
    w.load("show_y", [SH, Y1], rc1=True)
    w.wait(200)
    w.f("load", "")
    w.write("model_rc1_show.bin")


def model_rc1_failed_load():
    """rc_1: show_x loaded and played; a load of show_y fails after the head
    `load ""` (old cue_status kept by the engine); the bridge reconnects and
    the dump re-sends show_x's cue_status under load ""; then show_x loads."""
    w = _W()
    w.c()
    w.dump("no", "no", "", "")
    w.wait(2500)
    w.load("show_x", [X1, SH], rc1=True)
    w.play_through([X1])
    w.stop([X1, SH])
    w.f("timecode", 0)
    w.f("armed", "no")
    w.f("nextcue", "")
    w.f("load", "")                              # failed load stops here
    w.wait(500)
    w.d()
    w.wait(1000)
    w.c()
    w.dump("no", "no", "", "", cues={X1: 0, SH: 0}, enabled={X1: 1, SH: 1},
           names={X1: "Cue 0 ñ", SH: "Cue 1 ñ"})
    w.wait(2500)
    w.load("show_x", [X1, SH], rc1=True)
    w.write("model_rc1_failed_load.bin")


def model_rc1_reconnect_in_load_window():
    """rc_1 known limit: the link drops right after show_y's head `load ""`;
    the reconnect dump still carries show_x's cue_status (X1 at 100), held,
    then committed with show_y's burst."""
    w = _W()
    w.c()
    w.dump("no", "no", "", "")
    w.wait(2500)
    w.load("show_x", [X1, SH], rc1=True)
    w.play_through([X1])
    w.f("running", "no")                         # (as after a STOP, minus the resets)
    w.f("timecode", 0)
    w.f("armed", "no")
    w.f("nextcue", "")
    w.f("load", "")                              # head of show_y's load
    w.d()
    w.wait(1000)
    w.c()
    w.dump("no", "no", "", "", cues={X1: 100, SH: 0})
    for u in (SH, Y1):
        w.f(f"cue/{u}", 0)
    w.f("load", "show_y")
    w.write("model_rc1_reconnect_in_load_window.bin")


if __name__ == "__main__":
    model_released_show()
    model_rc1_show()
    model_rc1_failed_load()
    model_rc1_reconnect_in_load_window()
