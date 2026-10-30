# taikocam

Play [OpenTaiko](https://github.com/0auBSQ/OpenTaiko) with bright-tipped sticks
and sheets of paper. A webcam watches the stick tips and turns hits into real
`D` / `F` / `J` / `K` key presses, so the game needs no patching — it just sees
a keyboard.

Built because nothing like it existed: there are webcam colour-stick drum kits
that play sounds in a browser, and DIY Arduino taiko drums that emulate a
keyboard, but nothing that connects a camera to a rhythm game's input.

## How the keys get sent

taikocam creates a **virtual keyboard in the kernel** through `/dev/uinput`.
Because the events originate in the kernel, Wayland, X11 and GNOME all treat
them as a genuine keyboard — no `ydotool`, no X11-only `xdotool`, and no
compositor-specific hacks. On many distros (Fedora and Nobara included) the
desktop user already has an ACL on `/dev/uinput`, so **no root is needed**. If
not, `setup.sh` prints the one-time udev rule.

## Physical setup

This part decides whether it feels good, so it's worth getting right.

```
     camera (desk height, looking back at you)
         |
         v
    [ka] [don] [don] [ka]      <- four sheets of paper in a row
        you, holding two sticks
```

Put the camera **in front of you at roughly desk height — not overhead.** A hit
is then a *downward* movement in the image, which is cheap and reliable to
detect. An overhead camera cannot really see a hit at all: the stick travels
straight along the lens axis, and only its apparent size changes.

Tape a patch of one bright colour to both stick tips. Both sticks can be the
**same** colour — which lane you hit is decided by where the tip is, not by
which stick it is.

## Install

```sh
git clone https://github.com/stagfoo/taikocam
cd taikocam
./setup.sh
./taikocam --selftest --no-keys --headless     # verify, no camera needed
```

`setup.sh` makes a venv with OpenCV and reuses your distro's `python3-evdev`
(building evdev from pip needs kernel headers, so inheriting it is easier).

## Use

```sh
./taikocam --list-cameras
./taikocam --calibrate-colour    # hold a stick tip in the box, press SPACE
./taikocam --calibrate-zones     # click the paper line, then the 3 splits
./taikocam                       # play
./taikocam --no-keys             # watch hits without sending keys
```

In the window: `c` recalibrate colour, `z` recalibrate zones, `k` toggle key
sending, `[` / `]` lower/raise the strike-speed threshold, `q` quit.

Config lives in `~/.config/taikocam/config.json`. The default lanes are
OpenTaiko's standard layout, left to right: `D` left rim, `F` left centre,
`J` right centre, `K` right rim. Change `lanes` in the config for other games —
any `KEY_*` name from `evdev` works.

## How a hit is detected

A hit fires when a tracked blob is **descending faster than a threshold** and
**crosses into the trigger band** around the paper line. Firing on the crossing
rather than on the bounce keeps latency as low as the frame rate allows.

Two guards stop retriggering: a per-lane refractory window, and a re-arm height
the tip must rise above before it can fire again — otherwise a stick resting on
the paper would machine-gun.

## Tuning

The overlay shows frame rate, processing time, per-stick vertical velocity and a
log of recent hits, so a problem is visible rather than guessed at.

| Symptom | Fix |
| --- | --- |
| Missed hits | Lower the threshold with `[`. The velocity shown next to each tip tells you what your real strikes measure. |
| Double hits | Raise `refractory_ms`, or raise `rearm_px` so the stick must lift further. |
| Wrong lane | Rerun `--calibrate-zones`. |
| Tip not tracked | Rerun `--calibrate-colour` under the light you actually play in. The mask preview is inset top-right. |
| Jittery tracking | Raise `min_area` to ignore small bright specks. |

## The honest limitation: timing

**Frame rate is timing resolution.** At 30 fps each frame is 33 ms, so hits
quantise to 33 ms steps with latency on top — looser than taiko really wants.

1. Get 60 fps. taikocam requests MJPG, which is the only mode most webcams reach
   high frame rates in. `--list-cameras` reports what you actually got.
2. Keep the resolution low. 640x480 is plenty to find a blob.
3. Compensate the *constant* part in the game's own input-offset setting. That
   cannot fix jitter, only average lag.

If it still feels soft, the fix is to stop taking timing from the camera at all:
a mic — or a piezo disc taped under the paper — catches the strike transient
within a few milliseconds, while the camera only decides *which* lane was hit.
That decouples timing precision from frame rate entirely, and is the natural
next step for this project.

## Requirements

Linux, a webcam, Python 3, OpenCV, NumPy, and python-evdev (only needed to send
keys; `--no-keys` runs without it).

## License

MIT
