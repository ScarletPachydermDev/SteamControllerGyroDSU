#!/usr/bin/env python3
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
import math
import os
import select
import socket
import struct
import sys
import threading
import time
import zlib

VENDOR_ID = "28de"
PRODUCT_ID = "1305"

REPORT_SIZE = 54
# Bytes 1-2 of a report: a counter the controller increments per frame.
COUNTER_OFFSET = 1
ACCEL_OFFSET = 34
GYRO_OFFSET = 40
ACCEL_UNITS_PER_G = 16384.0
GYRO_UNITS_PER_DEG = 16.0
# Raw counts this small are sensor noise, not motion; same idea as
# SteamDeckGyroDSU's own gyro deadzone. Without it a resting controller
# slowly drifts the aim in games that integrate the rate.
GYRO_DEADZONE = 8
# No reports for this long means the handle has gone stale (see
# read_reports) and the device needs reopening.
STALE_AFTER = 1.0
# Reports still arriving, but carrying the same motion values over and over,
# means the sensor is asleep rather than the handle being dead: wake it again
# (see WAKE_REPORT) after this long.
ASLEEP_AFTER = 2.0

# The controller leaves its motion sensor idle until something asks for it,
# and then keeps repeating one frozen sample in every report: a game reads
# that as a stick held hard over. Steam's own gyro setting wakes it, but
# relying on that means the sensor sleeps again the moment a game's Steam
# Input config has no use for gyro, and Steam's gyro output then fights the
# emulator's. Writing Valve's own gyro-enable report wakes it directly.
# Taken from SteamDeckGyroDSU's HidApiDev::EnableGyro (a Valve SET_SETTINGS
# with the gyro-mode register) and confirmed to work on the 2026 controller:
# a frozen stream went from 1 distinct sample in 3 seconds to 749.
WAKE_REPORT = bytes([0x00, 0x87, 0x0f, 0x30, 0x18, 0x00, 0x07, 0x07, 0x00,
                     0x08, 0x07, 0x00, 0x31, 0x02, 0x00, 0x18, 0x00] + [0] * 48)


def wake_motion(node):
    """Ask the controller to start reporting motion. Best effort: a
    controller that refuses the write still serves whatever it was already
    sending."""
    try:
        with open(node, "wb", buffering=0) as f:
            f.write(WAKE_REPORT)
        return True
    except OSError:
        return False

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
    """Every readable hidraw node belonging to the controller. Picks by USB
    ids rather than a fixed device number, which moves between boots."""
    found = []
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
                found.append(node)
    return found


def _deadzone(value):
    return 0 if abs(value) < GYRO_DEADZONE else value


class GyroBias:
    """Track and remove the gyro's resting bias.

    A gyro at rest does not read zero; it reads a small constant offset, and
    a constant rate is exactly what a game integrates into an aim that
    drifts to one side for ever. Measured on this controller at up to 46
    raw units, roughly 3 degrees/second, which sails straight through a
    small deadzone. This is the same problem the console itself has, which
    is why some games ask you to rest the controller on a flat surface
    before they start.

    Rest has to be established on all three counts, or the estimate eats
    real movement: the accelerometer feels 1 g and nothing else, it is not
    changing, and the gyro is reading small. The accelerometer alone is not
    enough, because turning a level controller about the vertical axis keeps
    it at exactly 1 g while genuinely rotating."""

    STILL_ACCEL_TOLERANCE = 0.06   # g away from gravity alone
    # Loose enough to count a controller held in the hands as still, since
    # that is where it spends its life and a tremor is not rotation anyone
    # is aiming with; tight enough to exclude deliberate movement.
    STILL_ACCEL_CHANGE = 300       # raw units between consecutive reports
    STILL_GYRO_LIMIT = 250         # raw units, about 15 degrees/second
    SETTLE_SAMPLES = 60            # about a quarter second at 265 reports/s
    SMOOTHING = 0.05               # how fast the estimate follows
    # The first estimate has to land fast: until it does, every game is
    # integrating a bias into drifting aim.
    FIRST_SMOOTHING = 0.4

    def __init__(self):
        self.bias = [0.0, 0.0, 0.0]
        self._still = 0
        self._prev_accel = None
        self._calibrated = False

    def update(self, raw_accel, raw_gyro):
        """Feed one report, then return the gyro with the bias removed."""
        magnitude = math.sqrt(sum((v / ACCEL_UNITS_PER_G) ** 2 for v in raw_accel))
        steady = self._prev_accel is not None and all(
            abs(a - b) < self.STILL_ACCEL_CHANGE for a, b in zip(raw_accel, self._prev_accel))
        self._prev_accel = raw_accel
        if (abs(magnitude - 1.0) < self.STILL_ACCEL_TOLERANCE and steady
                and max(abs(v - self.bias[i]) for i, v in enumerate(raw_gyro)) < self.STILL_GYRO_LIMIT):
            self._still += 1
        else:
            self._still = 0
        if self._still > self.SETTLE_SAMPLES:
            rate = self.SMOOTHING if self._calibrated else self.FIRST_SMOOTHING
            for i in range(3):
                self.bias[i] += (raw_gyro[i] - self.bias[i]) * rate
            self._calibrated = True
        return tuple(raw_gyro[i] - self.bias[i] for i in range(3))


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


# The controller reports its three sensor axes in a different order from
# the one DSU uses, so they have to be reordered rather than passed
# through. Which raw field is which was established by holding the
# controller in known positions and reading the gravity vector on a Steam
# Machine (2026-09-20):
#
#   raw 0 (offset 34): front-to-back, reads -1.00 g with the front edge down
#   raw 1 (offset 36): left-to-right, reads +0.61 g tilted right
#   raw 2 (offset 38): vertical,      reads +1.00 g lying flat
#
# DSU wants X left-to-right, Y front-to-back, Z vertical, and its gyro as
# pitch/yaw/roll about those same three. So accelerometer axes reorder to
# (1, 0, 2), and the gyro follows: pitch turns about the left-right axis,
# yaw about the vertical one, roll about front-to-back.
# Signs follow DSU's own directions, which are the opposite of this
# controller's on the two horizontal axes; established in a game rather than
# on paper (tilting right steered left until X was negated).
# Overridable while tuning: DSU_MAP="accel_axes:signs,gyro_axes:signs",
# e.g. DSU_MAP="1,0,2:-1,1,1,1,2,0:1,1,1".
_ACCEL_AXES = (1, 0, 2)
_ACCEL_SIGNS = (-1.0, -1.0, 1.0)
# Measured the same way as the accelerometer, one rotation at a time with the
# controller in hand (Steam Machine, 2026-09-20): rolling the right side down
# shows on raw field 2 negative, dipping the nose on field 1 negative, and
# turning flat to the right on field 0 negative. DSU wants pitch, yaw, roll,
# so the fields reorder to (1, 0, 2), with roll negated to make a right-side-
# down roll positive.
# DSU's third gyro slot is roll and its second is yaw, but this controller's
# fields land the other way round: proven in Breath of the Wild, where aim
# followed a wheel-roll until yaw and roll were swapped, then followed
# turning the controller like a torch, which is what the game expects.
_GYRO_AXES = (1, 2, 0)
_GYRO_SIGNS = (1.0, -1.0, -1.0)

if os.environ.get("DSU_MAP"):
    _a, _g = os.environ["DSU_MAP"].split(",", 1)[0], os.environ["DSU_MAP"]
    _parts = os.environ["DSU_MAP"].split(",")
    _ACCEL_AXES = tuple(int(x) for x in _parts[0].split(":")[0].split("|"))
    _ACCEL_SIGNS = tuple(float(x) for x in _parts[0].split(":")[1].split("|"))
    _GYRO_AXES = tuple(int(x) for x in _parts[1].split(":")[0].split("|"))
    _GYRO_SIGNS = tuple(float(x) for x in _parts[1].split(":")[1].split("|"))


def read_reports(motion, stop):
    """Feed motion from the controller, reopening it whenever the stream
    stops.

    Reopening is the whole point of this loop, not defensiveness. Starting a
    game makes Steam re-enumerate the controller, and a file handle opened
    before that keeps blocking in read() forever without any error: the node
    is still there and still readable, it just never delivers another report
    on the old handle. A server that opens the device once therefore goes on
    serving one frozen sample, which a game reads as a stick held hard over.
    Seen exactly that way on a Steam Machine (2026-09-20): Mario Kart 8
    steered to one side by itself while the same reports were still flowing
    to any freshly opened handle.

    So: poll, and if nothing arrives for a moment, drop the handle and look
    the device up again."""
    bias = GyroBias()
    while not stop.is_set():
        nodes = find_controller()
        if not nodes:
            time.sleep(1)
            continue
        handles = {}
        for node in nodes:
            wake_motion(node)
            try:
                handles[open(node, "rb", buffering=0)] = node
            except OSError:
                pass
        if not handles:
            time.sleep(1)
            continue
        poller = select.poll()
        for handle in handles:
            poller.register(handle, select.POLLIN)
        last_good = time.time()
        last_change = time.time()
        last_motion = None
        counters = {}
        try:
            while not stop.is_set():
                # Staleness is measured from the last usable report, not
                # from poll going quiet: a handle the controller has moved
                # on from still reports itself readable and then returns
                # nothing at all, so waiting for poll to fall silent waits
                # forever while the loop spins.
                if time.time() - last_good > STALE_AFTER:
                    break
                # Same motion values in report after report: the sensor has
                # gone back to sleep, so ask it again.
                if time.time() - last_change > ASLEEP_AFTER:
                    for node in handles.values():
                        wake_motion(node)
                    last_change = time.time()
                for fd, event in poller.poll(STALE_AFTER * 1000):
                    handle = next(h for h in handles if h.fileno() == fd)
                    if event & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                        raise OSError("device went away")
                    report = handle.read(REPORT_SIZE * 2)
                    if not report or len(report) != REPORT_SIZE:
                        continue
                    # Every report carries an incrementing counter, and only
                    # a live stream advances it. The receiver's other nodes
                    # hand back one unchanging frame as fast as they are
                    # asked, which is indistinguishable from real data by
                    # size or content alone; taking it as the newest sample
                    # is what left a game steering to one side while the
                    # real stream was fine (Steam Machine, 2026-09-20).
                    counter = struct.unpack_from("<H", report, COUNTER_OFFSET)[0]
                    if counters.get(handle) == counter:
                        continue
                    counters[handle] = counter
                    last_good = time.time()
                    sample = report[ACCEL_OFFSET:GYRO_OFFSET + 6]
                    if sample != last_motion:
                        last_motion = sample
                        last_change = last_good
                    raw_accel = struct.unpack_from("<3h", report, ACCEL_OFFSET)
                    raw_gyro = struct.unpack_from("<3h", report, GYRO_OFFSET)
                    unbiased = bias.update(raw_accel, raw_gyro)
                    motion.set(
                        tuple(sign * raw_accel[i] / ACCEL_UNITS_PER_G
                              for i, sign in zip(_ACCEL_AXES, _ACCEL_SIGNS)),
                        tuple(sign * _deadzone(unbiased[i]) / GYRO_UNITS_PER_DEG
                              for i, sign in zip(_GYRO_AXES, _GYRO_SIGNS)),
                    )
        except OSError:
            pass
        finally:
            for handle in handles:
                try:
                    handle.close()
                except OSError:
                    pass


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
    # One reader for all of the controller's nodes: the receiver exposes
    # several and only the live one produces reports, so the reader polls
    # them all rather than guessing which.
    threading.Thread(target=read_reports, args=(motion, stop), daemon=True).start()
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
