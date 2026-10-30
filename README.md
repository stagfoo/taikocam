# taikocam

Play OpenTaiko with bright-tipped sticks and paper targets. A webcam watches the
stick tips; hits become real `D` / `F` / `J` / `K` key presses.

## Why it works without root

`/dev/uinput` on this machine already carries an ACL granting the desktop user
read/write, so taikocam creates a kernel-level virtual keyboard directly. No
`sudo`, no `ydotool`, and because the events come from the kernel, Wayland and
GNOME treat them as a genuine keyboard — which is exactly what OpenTaiko needs.

## Physical setup (this part decides whether it feels good)

    camera (at desk height, looking back at you)
        |
        v
    [ka] [don] [don] [ka]      <- four sheets of paper in a row
       you, holding two sticks

Put the camera **in front of you at roughly desk height**, not overhead. A hit
is then a *downward* movement in the image, which is cheap and reliable to
detect. An overhead camera cannot see a hit at all — the stick moves straight
along the lens axis, and only its apparent size changes.

Tape a patch of one bright colour to both stick tips. Both sticks can be the
*same* colour: which lane you hit is decided by where the tip is, not by which
stick it is.

## Use

    ./taikocam --selftest --no-keys     # verify the pipeline, no camera needed
    ./taikocam --list-cameras
    ./taikocam --calibrate-colour       # hold a tip in the box, press SPACE
    ./taikocam --calibrate-zones        # click the paper line, then 3 splits
    ./taikocam                          # play
    ./taikocam --no-keys                # watch hits without injecting keys

In the window: `c` recalibrate colour, `z` recalibrate zones, `k` toggle key
injection, `[` / `]` lower/raise the strike speed threshold, `q` quit.

Config lives at `~/.config/taikocam/config.json`.

## Tuning

The overlay shows frame rate, processing time, per-stick vertical velocity, and
a log of recent hits, so a problem is visible rather than guessed at.

- **Missed hits** — lower the threshold with `[`. The velocity number next to
  each tip tells you what your real strikes measure.
- **Double hits** — raise `refractory_ms`, or raise `rearm_px` so the stick must
  lift further before it can fire again.
- **Wrong lane** — rerun `--calibrate-zones`.
- **Mushy timing** — see below.

## The honest limitation: timing

Frame rate *is* timing resolution. At 30 fps each frame is 33 ms, so hits
quantise to 33 ms steps with latency on top. Taiko wants tighter than that.

1. Force 60 fps. taikocam already requests MJPG, which is the only mode most
   webcams reach high frame rates in. `--list-cameras` reports what you got.
2. Use a low resolution. 640x480 is plenty to find a blob and keeps frame time
   down.
3. Compensate the *constant* part in OpenTaiko's own input-offset setting. That
   cannot fix jitter, only average lag.

If it still feels soft, the fix is to stop taking timing from the camera: a mic
(or a piezo taped under the paper) detects the strike transient within a few
milliseconds, while the camera only decides *which* lane. That decouples timing
precision from frame rate, and is the upgrade path if this version feels loose.
