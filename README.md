# SteamControllerGyroDSU

A DSU (CemuHook motion protocol) server for the **2026 Steam Controller**,
so emulators can use its real gyroscope.

Steam Input can turn gyro into a mouse or a stick, but some games ask the
controller itself for motion and get nothing from that. Breath of the
Wild's gyro shrines and its bow aiming are the usual examples. Emulators
accept real motion over DSU, and this serves it.

It is the Steam Machine's counterpart to
[SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU), which does
the same job for the Steam Deck's built-in IMU and does not know about this
controller.

## Requirements

Python 3, and a 2026 Steam Controller (USB `28de:1305`). No root: SteamOS
already grants your user access to the controller's HID device. It works
while Steam is running and holding the controller.

## Use

```sh
python3 dsu_server.py
```

It serves on `127.0.0.1:26760`, the port emulators expect. Then point your
emulator's motion source at that address:

| Emulator | Setting |
| --- | --- |
| Ryujinx / Ryubing | motion backend `CemuHook`, host `127.0.0.1`, port `26760` |
| Cemu | motion source `DSU` |
| Dolphin, Eden | their own DSU / CemuHook motion option |

To keep it running, install it as a user service:

```sh
install -Dm755 dsu_server.py ~/.local/bin/steam-controller-dsu
install -Dm644 steam-controller-dsu.service ~/.config/systemd/user/
systemctl --user enable --now steam-controller-dsu
```

## The controller's report format

Measured on a Steam Machine, not taken from any specification. Reports are
54 bytes at about 268 per second, on whichever of the receiver's HID nodes
carries traffic; the others are spare wireless channels and stay silent.

| Data | Offset | Format | Scale |
| --- | --- | --- | --- |
| Accelerometer | 34, 36, 38 | 3 × signed 16-bit LE | 16384 per g |
| Gyroscope | 40, 42, 44 | 3 × signed 16-bit LE | 16 per degree/second |

The accelerometer scale is solid: at rest the three fields have a magnitude
of 16487, which is 1.006 g. The gyro scale matches what SteamDeckGyroDSU
uses for the Deck's own IMU; a hand-turned 180 degrees measured about 12%
off it, which is within the error of turning by hand, and below what any
motion sensitivity slider absorbs. Buttons and sticks are deliberately not
served, since Steam already delivers those as an ordinary gamepad.

Axis order and signs are passed through as the controller reports them. If
a game feels inverted, invert it in the emulator.

## Credit

The protocol work and the approach come from
[SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU) by kmicki.

## Licence

MIT
