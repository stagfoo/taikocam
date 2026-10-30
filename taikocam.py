#!/usr/bin/env python3
"""
taikocam - webcam drum controller for OpenTaiko.

Bright-tipped sticks + paper targets -> real keyboard events via /dev/uinput.

Geometry (important):
    Put the camera IN FRONT of you, roughly at table height, looking back at
    you.  Lay the paper targets flat on the desk in a row.  A stick hit is then
    a DOWNWARD movement in the image, which is easy and cheap to detect.  An
    overhead camera cannot see a hit at all - the motion is straight along the
    lens axis.

So a hit is: a bright blob descending fast enough, crossing the surface line.
Which of the four targets it was is decided by the blob's x at that moment.
Firing on the crossing (not on the bounce) keeps latency as low as the camera
allows.
"""
import argparse, json, os, sys, time, math
from collections import deque

import cv2
import numpy as np

CONFIG_DIR = os.path.expanduser("~/.config/taikocam")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# OpenTaiko / TJAPlayer3 default drum layout, left to right.
DEFAULT_CONFIG = {
    "camera": 0,
    "width": 640,
    "height": 480,
    "fps": 60,
    "flip": True,                 # mirror, so moving right on screen is right
    "hsv_lo": [35, 80, 80],       # bright green tape, a safe starting point
    "hsv_hi": [85, 255, 255],
    "min_area": 60,
    "surface_y": 360,             # the paper line, in pixels
    "band": 45,                   # how thick the trigger band is
    "x_bounds": [160, 320, 480],  # 3 splits -> 4 lanes
    "lanes": [
        {"name": "L ka  (rim)",    "key": "KEY_D"},
        {"name": "L don (centre)", "key": "KEY_F"},
        {"name": "R don (centre)", "key": "KEY_J"},
        {"name": "R ka  (rim)",    "key": "KEY_K"},
    ],
    "min_down_speed": 320.0,      # px/sec downward to count as a strike
    "refractory_ms": 90,          # per lane
    "rearm_px": 25,               # must rise this far above band to re-fire
    "tap_ms": 18,                 # how long the key is held
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH)))
        except (OSError, ValueError) as exc:
            print(f"warning: ignoring bad config ({exc})", file=sys.stderr)
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"saved {CONFIG_PATH}")


# ---------------------------------------------------------------- output ----
class Drum:
    """Virtual keyboard on /dev/uinput.  No root needed when the uinput ACL
    grants the desktop user, which is the normal case on Fedora/Nobara."""

    def __init__(self, keys, enabled=True):
        self.enabled = enabled
        self.ui = None
        self.pending = []           # (release_time, code)
        if not enabled:
            return
        try:
            from evdev import UInput, ecodes
        except ImportError:
            raise SystemExit(
                "python-evdev is missing, so no key presses can be sent.\n"
                "  Fedora/Nobara : sudo dnf install python3-evdev\n"
                "  Debian/Ubuntu : sudo apt install python3-evdev\n"
                "  Arch          : sudo pacman -S python-evdev\n"
                "Or run with --no-keys to just watch hits.")
        self.ecodes = ecodes
        codes = [getattr(ecodes, k) for k in keys]
        try:
            self.ui = UInput({ecodes.EV_KEY: codes},
                             name="TaikoCam Virtual Drum", version=1)
        except (PermissionError, OSError) as exc:
            raise SystemExit(
                f"cannot open /dev/uinput ({exc}).\n"
                "Grant access once, then log out and back in:\n"
                "  echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", "
                "OPTIONS+=\"static_node=uinput\"' "
                "| sudo tee /etc/udev/rules.d/99-uinput.rules\n"
                "  sudo usermod -aG input \"$USER\"\n"
                "  sudo modprobe uinput && sudo udevadm control --reload\n"
                "Or run with --no-keys to just watch hits.")
        self.codes = {k: getattr(ecodes, k) for k in keys}

    def tap(self, keyname, hold_s):
        if not self.ui:
            return
        code = self.codes[keyname]
        self.ui.write(self.ecodes.EV_KEY, code, 1)
        self.ui.syn()
        self.pending.append((time.monotonic() + hold_s, code))

    def service(self):
        """Release keys whose hold time has elapsed."""
        if not self.ui or not self.pending:
            return
        now = time.monotonic()
        still = []
        for when, code in self.pending:
            if now >= when:
                self.ui.write(self.ecodes.EV_KEY, code, 0)
                self.ui.syn()
            else:
                still.append((when, code))
        self.pending = still

    def close(self):
        if self.ui:
            for _, code in self.pending:
                self.ui.write(self.ecodes.EV_KEY, code, 0)
            self.ui.syn()
            self.ui.close()


# ------------------------------------------------------------- detection ----
def find_blobs(frame, cfg):
    """Return blob centroids, largest first."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo, hi = np.array(cfg["hsv_lo"]), np.array(cfg["hsv_hi"])
    if lo[0] <= hi[0]:
        mask = cv2.inRange(hsv, lo, hi)
    else:  # hue wraps past 180 (reds)
        a = cv2.inRange(hsv, np.array([0, lo[1], lo[2]]), hi)
        b = cv2.inRange(hsv, lo, np.array([179, hi[1], hi[2]]))
        mask = cv2.bitwise_or(a, b)
    k = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if a < cfg["min_area"]:
            continue
        m = cv2.moments(c)
        if m["m00"] == 0:
            continue
        out.append((m["m10"] / m["m00"], m["m01"] / m["m00"], a))
    out.sort(key=lambda t: -t[2])
    return out[:2], mask


class Track:
    __slots__ = ("x", "y", "t", "vy", "armed")

    def __init__(self, x, y, t):
        self.x, self.y, self.t, self.vy, self.armed = x, y, t, 0.0, True


def associate(tracks, blobs, now):
    """Cheap nearest-neighbour association; at most two sticks."""
    fresh = []
    used = set()
    for tr in tracks:
        best, bd = None, 1e9
        for i, (x, y, _a) in enumerate(blobs):
            if i in used:
                continue
            d = math.hypot(x - tr.x, y - tr.y)
            if d < bd:
                best, bd = i, d
        if best is not None and bd < 180:
            x, y, _a = blobs[best]
            used.add(best)
            dt = max(now - tr.t, 1e-4)
            tr.vy = 0.6 * tr.vy + 0.4 * ((y - tr.y) / dt)
            tr.x, tr.y, tr.t = x, y, now
            fresh.append(tr)
    for i, (x, y, _a) in enumerate(blobs):
        if i not in used:
            fresh.append(Track(x, y, now))
    return fresh[:2]


def lane_for_x(x, cfg):
    i = 0
    for b in cfg["x_bounds"]:
        if x >= b:
            i += 1
    return min(i, len(cfg["lanes"]) - 1)


# ----------------------------------------------------------------- render ----
def draw_overlay(frame, cfg, tracks, hits, stats, mask=None):
    h, w = frame.shape[:2]
    sy, band = cfg["surface_y"], cfg["band"]

    # trigger band + surface line
    ov = frame.copy()
    cv2.rectangle(ov, (0, sy - band), (w, sy), (0, 90, 160), -1)
    cv2.addWeighted(ov, 0.25, frame, 0.75, 0, frame)
    cv2.line(frame, (0, sy), (w, sy), (0, 165, 255), 2)

    # lanes
    xs = [0] + list(cfg["x_bounds"]) + [w]
    now = time.monotonic()
    for i, lane in enumerate(cfg["lanes"]):
        x0, x1 = xs[i], xs[i + 1]
        cv2.line(frame, (x1, sy - band - 40), (x1, sy + 20), (70, 70, 70), 1)
        age = now - stats["last_hit_t"][i]
        if age < 0.18:                      # flash the lane that just fired
            f = ov.copy()
            cv2.rectangle(f, (x0, sy - band - 70), (x1, sy + 18),
                          (60, 220, 60), -1)
            cv2.addWeighted(f, 0.45, frame, 0.55, 0, frame)
        cv2.putText(frame, lane["name"], (x0 + 6, sy + 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (210, 210, 210), 1)
        cv2.putText(frame, lane["key"].replace("KEY_", ""),
                    ((x0 + x1) // 2 - 8, sy - band - 52),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (120, 255, 120), 2)
        cv2.putText(frame, str(stats["counts"][i]), (x0 + 6, sy + 56),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (150, 150, 150), 1)

    # sticks
    for tr in tracks:
        p = (int(tr.x), int(tr.y))
        cv2.circle(frame, p, 11, (0, 255, 255), 2)
        cv2.arrowedLine(frame, p, (p[0], int(tr.y + tr.vy * 0.08)),
                        (0, 255, 255), 2, tipLength=0.3)
        cv2.putText(frame, f"{tr.vy:+.0f}", (p[0] + 14, p[1]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1)
        if not tr.armed:
            cv2.circle(frame, p, 16, (0, 0, 255), 1)

    # diagnostics panel
    panel = [
        f"fps {stats['fps']:5.1f}   proc {stats['proc_ms']:4.1f} ms",
        f"blobs {stats['nblobs']}   tracks {len(tracks)}",
        f"hits {sum(stats['counts'])}   keys {'ON' if stats['keys'] else 'OFF (dry run)'}",
        f"min down speed {cfg['min_down_speed']:.0f} px/s",
    ]
    for i, line in enumerate(panel):
        cv2.putText(frame, line, (8, 20 + i * 18), cv2.FONT_HERSHEY_SIMPLEX,
                    0.46, (255, 255, 255), 1)
    for i, (t, name) in enumerate(list(hits)[-6:][::-1]):
        cv2.putText(frame, f"{now - t:4.1f}s  {name}", (8, h - 10 - i * 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (120, 255, 120), 1)
    cv2.putText(frame, "c colour  z zones  k keys  [ ] speed  q quit",
                (8, h - 118), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (170, 170, 170), 1)

    if mask is not None:
        small = cv2.resize(mask, (w // 5, h // 5))
        frame[8:8 + h // 5, w - 8 - w // 5:w - 8] = \
            cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
    return frame


# ----------------------------------------------------------------- camera ----
def open_camera(cfg):
    cap = cv2.VideoCapture(cfg["camera"], cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(cfg["camera"])
    if not cap.isOpened():
        return None
    # MJPG first: most webcams only reach their high frame rates in MJPG, and
    # frame rate is literally our timing resolution.
    fourcc = getattr(cv2, "VideoWriter_fourcc", None) or cv2.VideoWriter.fourcc
    cap.set(cv2.CAP_PROP_FOURCC, fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
    cap.set(cv2.CAP_PROP_FPS, cfg["fps"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # do not queue stale frames
    return cap


def list_cameras():
    found = []
    for i in range(10):
        cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                found.append((i, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                              int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                              cap.get(cv2.CAP_PROP_FPS)))
            cap.release()
    if not found:
        print("no working cameras found")
    for i, w, h, f in found:
        print(f"  camera {i}: {w}x{h} @ {f:.0f} fps")
    return found


# ------------------------------------------------------------ calibration ----
def calibrate_colour(cap, cfg):
    """Hold a stick tip in the box, press SPACE to sample its colour."""
    win = "taikocam - colour"
    cv2.namedWindow(win)
    while True:
        ok, frame = cap.read()
        if not ok:
            return False
        if cfg["flip"]:
            frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]
        bx, by, bs = w // 2 - 40, h // 2 - 40, 80
        patch = frame[by:by + bs, bx:bx + bs]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        med = np.median(hsv.reshape(-1, 3), axis=0)
        cv2.rectangle(frame, (bx, by), (bx + bs, by + bs), (0, 255, 255), 2)
        for i, line in enumerate([
                "Hold a stick TIP inside the box.",
                f"H {med[0]:.0f}  S {med[1]:.0f}  V {med[2]:.0f}",
                "SPACE = sample    q = cancel"]):
            cv2.putText(frame, line, (10, 24 + i * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
        cv2.imshow(win, frame)
        k = cv2.waitKey(1) & 0xFF
        if k == ord(" "):
            hh, ss, vv = med
            cfg["hsv_lo"] = [int(max(0, hh - 12)), int(max(60, ss * 0.5)),
                             int(max(60, vv * 0.5))]
            cfg["hsv_hi"] = [int(min(179, hh + 12)), 255, 255]
            print(f"colour range: {cfg['hsv_lo']} .. {cfg['hsv_hi']}")
            cv2.destroyWindow(win)
            return True
        if k in (ord("q"), 27):
            cv2.destroyWindow(win)
            return False


def calibrate_zones(cap, cfg):
    """Click the paper surface line, then 3 splits between the 4 targets."""
    win = "taikocam - zones"
    clicks = []
    cv2.namedWindow(win)

    def on_mouse(ev, x, y, _flags, _p):
        if ev == cv2.EVENT_LBUTTONDOWN:
            clicks.append((x, y))

    cv2.setMouseCallback(win, on_mouse)
    prompts = ["1/4  click the PAPER SURFACE (where the tips land)",
               "2/4  click between target 1 and 2",
               "3/4  click between target 2 and 3",
               "4/4  click between target 3 and 4"]
    while True:
        ok, frame = cap.read()
        if not ok:
            return False
        if cfg["flip"]:
            frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]
        if clicks:
            sy = clicks[0][1]
            cv2.line(frame, (0, sy), (w, sy), (0, 165, 255), 2)
            for (x, _y) in clicks[1:]:
                cv2.line(frame, (x, sy - 120), (x, sy + 20), (0, 255, 255), 2)
        step = min(len(clicks), 3)
        cv2.putText(frame, prompts[step], (10, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 2)
        cv2.putText(frame, "u = undo    q = cancel", (10, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.46, (180, 180, 180), 1)
        cv2.imshow(win, frame)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("u") and clicks:
            clicks.pop()
        if k in (ord("q"), 27):
            cv2.destroyWindow(win)
            return False
        if len(clicks) == 4:
            cfg["surface_y"] = int(clicks[0][1])
            cfg["x_bounds"] = sorted(int(c[0]) for c in clicks[1:])
            print(f"surface_y={cfg['surface_y']}  x_bounds={cfg['x_bounds']}")
            cv2.destroyWindow(win)
            return True


# ------------------------------------------------------------- main loops ----
def step_hits(tracks, cfg, drum, stats, hits, now):
    """Fire a key when a descending stick crosses into the band."""
    sy, band = cfg["surface_y"], cfg["band"]
    for tr in tracks:
        if tr.armed:
            crossing = (sy - band) <= tr.y <= (sy + band * 0.5)
            if crossing and tr.vy >= cfg["min_down_speed"]:
                i = lane_for_x(tr.x, cfg)
                if (now - stats["last_hit_t"][i]) * 1000.0 >= cfg["refractory_ms"]:
                    lane = cfg["lanes"][i]
                    drum.tap(lane["key"], cfg["tap_ms"] / 1000.0)
                    stats["last_hit_t"][i] = now
                    stats["counts"][i] += 1
                    hits.append((now, f"{lane['name']} -> "
                                      f"{lane['key'].replace('KEY_','')}"))
                tr.armed = False
        elif tr.y < sy - band - cfg["rearm_px"]:
            tr.armed = True            # lifted clear; ready for the next hit


def new_stats(cfg, keys_on):
    n = len(cfg["lanes"])
    return {"fps": 0.0, "proc_ms": 0.0, "nblobs": 0, "keys": keys_on,
            "counts": [0] * n, "last_hit_t": [0.0] * n}


def run(cfg, keys_on, show):
    cap = open_camera(cfg)
    if cap is None:
        print("error: could not open camera "
              f"{cfg['camera']}.  Plug a webcam in, or try --list-cameras.",
              file=sys.stderr)
        return 1
    drum = Drum([l["key"] for l in cfg["lanes"]], enabled=keys_on)
    stats = new_stats(cfg, keys_on)
    hits, tracks = deque(maxlen=40), []
    ft = deque(maxlen=30)
    print(f"running.  keys {'ENABLED' if keys_on else 'disabled (dry run)'}.  "
          "q in the window to quit.")
    try:
        while True:
            t0 = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                print("camera read failed", file=sys.stderr)
                break
            if cfg["flip"]:
                frame = cv2.flip(frame, 1)
            blobs, mask = find_blobs(frame, cfg)
            now = time.monotonic()
            tracks = associate(tracks, blobs, now)
            step_hits(tracks, cfg, drum, stats, hits, now)
            drum.service()

            stats["nblobs"] = len(blobs)
            stats["proc_ms"] = (time.monotonic() - t0) * 1000.0
            ft.append(now)
            if len(ft) > 2:
                stats["fps"] = (len(ft) - 1) / max(ft[-1] - ft[0], 1e-6)

            if show:
                draw_overlay(frame, cfg, tracks, hits, stats, mask)
                cv2.imshow("taikocam", frame)
                k = cv2.waitKey(1) & 0xFF
                if k in (ord("q"), 27):
                    break
                if k == ord("c"):
                    calibrate_colour(cap, cfg) and save_config(cfg)
                if k == ord("z"):
                    calibrate_zones(cap, cfg) and save_config(cfg)
                if k == ord("k"):
                    keys_on = not keys_on
                    drum.close()
                    drum = Drum([l["key"] for l in cfg["lanes"]], enabled=keys_on)
                    stats["keys"] = keys_on
                if k == ord("["):
                    cfg["min_down_speed"] = max(40, cfg["min_down_speed"] - 40)
                if k == ord("]"):
                    cfg["min_down_speed"] += 40
    finally:
        drum.close()
        cap.release()
        cv2.destroyAllWindows()
    return 0


def selftest(cfg, keys_on, show):
    """No camera: drive a synthetic blob down through each lane in turn and
    check that exactly the four expected keys fire, in order."""
    w, h = cfg["width"], cfg["height"]
    cfg = dict(cfg)
    cfg["surface_y"] = int(h * 0.75)
    cfg["x_bounds"] = [w // 4, w // 2, 3 * w // 4]
    cfg["hsv_lo"], cfg["hsv_hi"] = [35, 80, 80], [85, 255, 255]
    lane_x = [w // 8, 3 * w // 8, 5 * w // 8, 7 * w // 8]

    drum = Drum([l["key"] for l in cfg["lanes"]], enabled=keys_on)
    stats = new_stats(cfg, keys_on)
    hits, tracks = deque(maxlen=40), []
    fired = []
    dt = 1.0 / 60.0
    try:
        for li, cx in enumerate(lane_x):
            # descend from above the band to the surface, then lift away
            ys = list(range(int(h * 0.15), cfg["surface_y"] + 1, 14)) + \
                 list(range(cfg["surface_y"], int(h * 0.15), -14))
            for y in ys:
                frame = np.zeros((h, w, 3), np.uint8)
                cv2.circle(frame, (cx, y), 13, (60, 230, 60), -1)
                blobs, mask = find_blobs(frame, cfg)
                now = time.monotonic()
                tracks = associate(tracks, blobs, now)
                before = sum(stats["counts"])
                step_hits(tracks, cfg, drum, stats, hits, now)
                drum.service()
                if sum(stats["counts"]) > before:
                    fired.append(hits[-1][1])
                if show:
                    stats["nblobs"] = len(blobs)
                    draw_overlay(frame, cfg, tracks, hits, stats, mask)
                    cv2.imshow("taikocam selftest", frame)
                    if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                        return 1
                time.sleep(dt)
            tracks = []
    finally:
        drum.close()
        if show:
            cv2.destroyAllWindows()

    expect = [l["key"].replace("KEY_", "") for l in cfg["lanes"]]
    got = [f.split("-> ")[-1] for f in fired]
    print("\n  expected:", expect)
    print("  fired   :", got)
    print("  per lane:", stats["counts"])
    ok = got == expect
    print("\n  RESULT  :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calibrate-colour", "--calibrate-color",
                    dest="cal_colour", action="store_true")
    ap.add_argument("--calibrate-zones", dest="cal_zones", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="verify detection + key injection with no camera")
    ap.add_argument("--list-cameras", action="store_true")
    ap.add_argument("--no-keys", action="store_true",
                    help="show hits but inject nothing (dry run)")
    ap.add_argument("--headless", action="store_true", help="no preview window")
    ap.add_argument("--camera", type=int)
    ap.add_argument("--show-config", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if args.camera is not None:
        cfg["camera"] = args.camera
    if args.show_config:
        print(json.dumps(cfg, indent=2))
        return 0
    if args.list_cameras:
        list_cameras()
        return 0
    if args.selftest:
        return selftest(cfg, not args.no_keys, not args.headless)

    if args.cal_colour or args.cal_zones:
        cap = open_camera(cfg)
        if cap is None:
            print(f"error: no camera {cfg['camera']}", file=sys.stderr)
            return 1
        changed = False
        if args.cal_colour:
            changed |= calibrate_colour(cap, cfg)
        if args.cal_zones:
            changed |= calibrate_zones(cap, cfg)
        cap.release()
        cv2.destroyAllWindows()
        if changed:
            save_config(cfg)
        return 0

    return run(cfg, not args.no_keys, not args.headless)


if __name__ == "__main__":
    sys.exit(main())
