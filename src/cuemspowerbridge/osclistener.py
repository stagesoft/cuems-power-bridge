# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

# Legacy per-site UDP OSC listener (afrucat venue). Pre-dates the
# cuems-power-bridge daemon. Kept in tree but superseded for new
# deployments by cuems-power-bridge, which centralises shutdown/GO/STOP
# via HTTP + WebSocket-OSC.

from pythonosc.dispatcher import Dispatcher
from pythonosc import osc_server

import os


import asyncio
import subprocess

import time
import os

INPORT = 6007
OUTPORT = 6006
IP = "192.168.2.204"

project_1 = "618caa6e-dc7c-11ee-8a69-00e04c0206a4"

project_2 = "a56bd00e-d71b-11ee-b5f5-222222222222"

project_3 = "a56bd00e-d71b-11ee-b5f5-333333333333"




project_id_file = '/etc/cuems/project_id'

def shutdown_handler(addr, *args):
    
    print("received shutdown")
    subprocess.call('/usr/local/bin/stop.sh', shell=True)
    subprocess.call(['/usr/bin/sudo /usr/sbin/shutdown -h now'], shell=True)


def restart_handler(addr, *args):
    
    print("received restart")
    subprocess.call('/usr/local/bin/stop.sh', shell=True)
    subprocess.call(['/usr/bin/sudo /usr/sbin/shutdown -r now'], shell=True)

def program_change_handler(addr, *args):

    print(f"received program id: {args[0]}")

    if args[0] != None:
        project_num = args[0]


    if project_num == 1:
            project_id = project_1
    elif project_num == 2:
            project_id = project_2
    elif project_num == 2:
            project_id = project_3


    try:

        with open(project_id_file, "w") as my_file:
            my_file.write(project_id)
    except FileNotFoundError as e:
        print(f"Something else went wrong {e}")

    time.sleep(1)

    print("received program change")
    subprocess.call('/usr/local/bin/stop.sh', shell=True)
    subprocess.call(['/usr/bin/sudo /usr/sbin/shutdown -r now'], shell=True)

def default_osc_handler(addr, *args):
    print(f'Recibido mensaje OSC no reconocido : direccion -> {addr} | datos -> {args}')


 # creamos nuestro dispatcher de mensajes OSC
dispatcher = Dispatcher()

dispatcher.map(f"/afrucat/shutdown", shutdown_handler)

dispatcher.map(f"/afrucat/restart", restart_handler)

dispatcher.map(f"/afrucat/program", program_change_handler)

 # asignamos manejador por defecto
dispatcher.set_default_handler(default_osc_handler)




server = osc_server.ThreadingOSCUDPServer(
        (IP, INPORT), dispatcher)
print("Serving on {}".format(server.server_address))
server.serve_forever()
