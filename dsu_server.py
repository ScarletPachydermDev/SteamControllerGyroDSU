"""A DSU (CemuHook motion protocol) server for the 2026 Steam Controller,
so emulators can use its real gyroscope instead of Steam Input's emulated
one.

Why this exists: Steam Input can turn gyro into a mouse or a stick, but a
game that asks the controller itself for motion (Breath of the Wild's gyro
shrines, its bow aiming) gets nothing from that. Emulators accept real
motion over DSU, a UDP protocol on port 26760. kmicki/SteamDeckGyroDSU
serves the Steam Deck's own IMU that way; this serves the Steam Machine's
controller, which that project does not know about.

Everything about the controller's report format below was measured live on
a Steam Machine (2026-09-20), not guessed:

  * The controller is USB 28de:1305 ("Valve Software Steam Controller
    Puck"), and its input stream is the one hidraw node that carries
    traffic; the receiver's other nodes are spare wireless channels and
    stay silent.
  * Reports are 54 bytes at ~268/s. Reading them needs no root (SteamOS
    puts an ACL on the device) and works while Steam is running and
    holding the controller.
  * Accelerometer: three signed 16-bit fields at offsets 34/36/38. At
    rest their magnitude is 16487, i.e. 1.006 g at 16384 units per g,
    which is what fixes that scale.
  * Gyroscope: three signed 16-bit fields at offsets 40/42/44, ±5 at
    rest and swinging over ±20000 when rotated. Scale is 16 units per
    degree/second, matching what SteamDeckGyroDSU uses for the Deck's own
    IMU; a hand-turned 180 degrees measured 12% off that, which is
    within the error of turning by hand and below what a motion
    sensitivity slider absorbs anyway.

Run it directly (`python3 dsu_server.py`), or as a user service. It exits
if the controller disappears, so a service can simply restart it.
"""
import glob
import os
import socket
import struct
import sys
import threading
import time
import zlib

VENDOR_ID = "28de"
PRODUCT_ID = "1305"

REPORT_SIZE = 54
ACCEL_OFFSET = 34
GYRO_OFFSET = 40
ACCEL_UNITS_PER_G = 16384.0
GYRO_UNITS_PER_DEG = 16.0
# Raw counts this small are sensor noise, not motion; same idea as
# SteamDeckGyroDSU's own gyro deadzone. Without it a resting controller
# slowly drifts the aim in games that integrate the rate.
GYRO_DEADZONE = 8

HOST = "127.0.0.1"
PORT = 26760

_MAGIC_CLIENT = b"DSUC"
_MAGIC_SERVER = b"DSUS"
_VERSION = 1001
_MSG_VERSION = 0x100000
_MSG_PORTS = 0x100001
_MSG_DATA = 0x100002

# A client that has not asked for anything for this long is gone. Nothing
# in the protocol says goodbye, so this is the only way to stop sending.
_CLIENT_TIMEOUT = 5.0
_SLOT_MAC = b"\x00\x00\x00\x00\x00\x01"


def find_controller():
    """The controller's hidraw path, or None. Picks by USB ids rather
    than a fixed device number, which moves between boots."""
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(path, "device", "uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        hid_id = ""
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                hid_id = line.split("=", 1)[1].lower()
        if VENDOR_ID in hid_id and PRODUCT_ID in hid_id:
            node = os.path.join("/dev", os.path.basename(path))
            if os.access(node, os.R_OK):
                yield node


def _deadzone(value):
    return 0 if abs(value) < GYRO_DEADZONE else value


class Motion:
    """The latest sample, shared between the reader and the server."""

    def __init__(self):
        self.lock = threading.Lock()
        self.timestamp = 0
        self.accel = (0.0, 0.0, 0.0)
        self.gyro = (0.0, 0.0, 0.0)
        self.frames = 0

    def set(self, accel, gyro):
        with self.lock:
            self.timestamp = int(time.time() * 1_000_000)
            self.accel = accel
            self.gyro = gyro
            self.frames += 1

    def get(self):
        with self.lock:
            return self.timestamp, self.accel, self.gyro


def read_reports(node, motion, stop):
    """Turn raw reports into g and degrees/second until the controller
    goes away. Axis order and signs are passed through as the controller
    reports them; a game that feels inverted is tuned in the emulator,
    which every one of them exposes, rather than guessed at here."""
    with open(node, "rb", buffering=0) as f:
        while not stop.is_set():
            report = f.read(128)
            if not report or len(report) != REPORT_SIZE:
                continue
            ax, ay, az = struct.unpack_from("<3h", report, ACCEL_OFFSET)
            gx, gy, gz = struct.unpack_from("<3h", report, GYRO_OFFSET)
            motion.set(
                (ax / ACCEL_UNITS_PER_G, ay / ACCEL_UNITS_PER_G, az / ACCEL_UNITS_PER_G),
                (_deadzone(gx) / GYRO_UNITS_PER_DEG,
                 _deadzone(gy) / GYRO_UNITS_PER_DEG,
                 _deadzone(gz) / GYRO_UNITS_PER_DEG),
            )


def _packet(msg_type, payload, server_id):
    body = struct.pack("<II", server_id, msg_type) + payload
    header = _MAGIC_SERVER + struct.pack("<HHI", _VERSION, len(body), 0)
    crc = zlib.crc32(header + body) & 0xFFFFFFFF
    return header[:8] + struct.pack("<I", crc) + body


def _slot_info(slot=0, connected=True):
    # slot, state (2 = connected), model (2 = full gyro), connection
    # type (1 = USB), MAC, battery (5 = charged).
    return struct.pack("<BBBB", slot, 2 if connected else 0, 2, 1) + _SLOT_MAC + b"\x05"


def serve(motion, stop, host=HOST, port=PORT):
    server_id = int.from_bytes(os.urandom(4), "little")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(0.5)
    clients = {}
    counter = 0
    last_sent = 0.0
    while not stop.is_set():
        try:
            request, addr = sock.recvfrom(1024)
        except socket.timeout:
            request, addr = None, None
        except OSError:
            break
        if request and request[:4] == _MAGIC_CLIENT and len(request) >= 20:
            msg_type = struct.unpack_from("<I", request, 16)[0]
            if msg_type == _MSG_VERSION:
                sock.sendto(_packet(_MSG_VERSION, struct.pack("<H", _VERSION), server_id), addr)
            elif msg_type == _MSG_PORTS:
                count = struct.unpack_from("<I", request, 20)[0] if len(request) >= 24 else 1
                for i in range(min(count, 4)):
                    slot = request[24 + i] if len(request) > 24 + i else i
                    sock.sendto(_packet(_MSG_PORTS, _slot_info(slot, slot == 0) + b"\x00",
                                        server_id), addr)
            elif msg_type == _MSG_DATA:
                clients[addr] = time.time()

        # Push motion to every subscriber at the controller's own rate.
        now = time.time()
        if clients and now - last_sent >= 1 / 250:
            last_sent = now
            timestamp, accel, gyro = motion.get()
            counter = (counter + 1) & 0xFFFFFFFF
            payload = (_slot_info() + b"\x01" + struct.pack("<I", counter)
                       # Buttons, sticks, triggers and touchpads: all zero.
                       # Steam already delivers those as a normal gamepad;
                       # this server exists purely for the motion an
                       # emulator cannot get any other way.
                       + bytes(2) + bytes(2) + bytes(4) + bytes(4) + bytes(8) + bytes(12)
                       + struct.pack("<Q", timestamp)
                       + struct.pack("<3f", *accel)
                       + struct.pack("<3f", *gyro))
            packet = _packet(_MSG_DATA, payload, server_id)
            for client, seen in list(clients.items()):
                if now - seen > _CLIENT_TIMEOUT:
                    del clients[client]
                    continue
                try:
                    sock.sendto(packet, client)
                except OSError:
                    clients.pop(client, None)
    sock.close()


def main():
    nodes = list(find_controller())
    if not nodes:
        print("No readable Steam Controller HID device found "
              f"(looking for {VENDOR_ID}:{PRODUCT_ID})", file=sys.stderr)
        return 1
    motion = Motion()
    stop = threading.Event()
    # Every candidate node gets a reader: the receiver exposes several and
    # only the live one produces reports, so this avoids having to guess
    # which. The silent ones simply block, costing a thread each.
    for node in nodes:
        threading.Thread(target=read_reports, args=(node, motion, stop), daemon=True).start()
    print(f"DSU server on {HOST}:{PORT}, reading {', '.join(nodes)}", flush=True)
    try:
        serve(motion, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
