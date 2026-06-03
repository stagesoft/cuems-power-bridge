# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Legacy stand-alone client: loads a project (via editor /ws) and sends GO
(via engine /realtime). Kept for backwards compatibility with operator
scripts that use it directly; the modern path is the cuems-power-bridge
daemon which maintains a persistent connection.
"""

import asyncio
import websockets
import json
from pythonosc import osc_message_builder, osc_message
import argparse

project_id_file = '/etc/cuems/project_id'

default_project = "a56bd00e-d71b-11ee-b5f5-408d5c84af19"


def _read_project_id():
    try:
        with open(project_id_file, "r") as my_file:
            return my_file.read().strip()
    except FileNotFoundError as e:
        print(f'can not open project_id file {e}')
        return default_project


async def load_and_go(project, host):
    project_load = {"action": "project_ready", "value": project}
    message = json.dumps(project_load)
    ok_response = {"type": "project_ready", "value": project}

    go_msg = osc_message_builder.OscMessageBuilder(address="/engine/command/go")
    go_msg.add_arg(1)
    build_go_msg = go_msg.build()
    go_ok_msg = "/engine/command/go"

    uri_ws = f"ws://{host}/ws"
    async with websockets.connect(uri_ws) as websocket:
        await websocket.send(message)
        response = json.loads(await websocket.recv())
        print(response)
        while response != ok_response:
            response = json.loads(await websocket.recv())
            print(response)
            await asyncio.sleep(5)
        print("----------response was ok, continue----------")

    uri_realtime = f"ws://{host}/realtime"
    async with websockets.connect(uri_realtime) as websocket:
        await websocket.send(build_go_msg.dgram)
        response = osc_message.OscMessage(await websocket.recv())
        print(response.address, end=" ")
        print(response.params)
        while response.address != go_ok_msg:
            response = osc_message.OscMessage(await websocket.recv())
            print(response.address, end=" ")
            print(response.params)
        print("----------Engine is running, exit----------")


def main_cli():
    parser = argparse.ArgumentParser(description='Load project and send GO.')
    parser.add_argument('project_id', nargs='?', type=str,
                        help='project uuid', default=None)
    parser.add_argument('--host', default='master.local',
                        help='controller mDNS/IP (default: master.local)')
    args = parser.parse_args()
    project = args.project_id if args.project_id else _read_project_id()
    asyncio.run(load_and_go(project, args.host))


if __name__ == "__main__":
    main_cli()
