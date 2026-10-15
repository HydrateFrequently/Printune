#!/usr/bin/env python3
"""Run Printune on a fleet of virtual printers with different beds, a few at a time.

Each configuration (bed size, machine setup geometry, accelerometer, enclosure, wizard or
advanced tuning, filament) gets its own container from the virtual-kalico image, on
port 8100+N, with its own gcodes/logs folders under fleet/<name>/ and its own Docker
volumes. At most --jobs containers run at once (each uses about one CPU core while
printing at 20x); a container is removed again when its run is done.

For every run it reports: the driver's errors and final print state, the number of
prompts and plate changes, and the offline G-code check (outside the bed, nozzle
crashes, gantry over tall prints, extrusions onto other objects).

  python3 tools/fleet.py                 # all configurations, 8 at a time
  python3 tools/fleet.py --jobs 4 --only v0-wizard-pla,ender-adv-petg
  python3 tools/fleet.py --list

Build the image first (docker compose up -d --build in test_env). Docker is called
directly; if your login session isn't in the docker group yet, use --newgrp.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_ENV = os.path.dirname(HERE)
REPO = os.path.dirname(TEST_ENV)
FLEET = os.path.join(TEST_ENV, "fleet")

# (name, bed x, bed y, bed z, toolhead class, accelerometer, enclosed)
BEDS = [
    ("v0", 120, 120, 120, "small", 0, 0),
    ("180", 180, 180, 180, "small", 0, 0),
    ("ender", 220, 220, 250, "medium", 0, 0),
    ("235", 235, 235, 250, "medium", 0, 0),
    ("v24-250", 250, 250, 230, "large", 1, 1),
    ("v24-300", 300, 300, 280, "large", 1, 1),
    ("v24-350", 350, 350, 330, "large", 1, 1),
    ("neb370", 375, 388, 340, "large", 1, 1),
]
# machine setup per toolhead class: toolhead box (-X,+X,-Y,+Y from the nozzle), gantry height
# above the nozzle tip at Z=0, nozzle protrusion. Plausible test values, not measurements.
GEOMETRY = {"small": ("-25,25,-30,30", 30, 3), "medium": ("-35,35,-40,45", 35, 4),
            "large": ("-35,35,-45,40", 40, 3)}
BED_MARGIN = 5      # the setup's bed area: the bed minus this margin (like a bed mesh's mesh_min/max)
# every bed: wizard PLA, wizard ABS, advanced PETG; then six extra mixes
RUNS = [(b, "wizard", "PLA") for b in BEDS] + [(b, "wizard", "ABS") for b in BEDS] + \
       [(b, "advanced", "PETG") for b in BEDS]
EXTRA = [("v0", "wizard", "TPU"), ("235", "wizard", "TPU"), ("v24-300", "wizard", "TPU"),
         ("180", "advanced", "ASA"), ("v24-250", "advanced", "ASA"), ("neb370", "advanced", "ASA")]
BYNAME = {b[0]: b for b in BEDS}
RUNS += [(BYNAME[b], p, f) for b, p, f in EXTRA]

# rules are matched against the prompt title plus its text; the first rule that matches wins
PICKS_COMMON = ["heat soak=-", "temperatures=Start", "clear the plate=Plate is clear", "Retraction towers\\?=Print the",
                "Cooling test\\?=Print the", "Bridge test\\?=Print the", "Max flow tower\\?=Print the",
                "Cooling: \\d+ s=Use ", "Max flow: band=Use "]


def configs():
    out = []
    for i, (bed, proc, fil) in enumerate(RUNS, 1):
        name = "%s-%s-%s" % (bed[0], "wiz" if proc == "wizard" else "adv", fil.lower())
        out.append({"n": i, "name": name, "bed": bed, "proc": proc, "fil": fil, "port": 8100 + i})
    return out


class Docker:
    def __init__(self, newgrp):
        self.newgrp = newgrp

    def run(self, args, check=True, timeout=600):
        if self.newgrp:
            cmd = " ".join(_q(a) for a in ["docker"] + args)
            p = subprocess.run(["newgrp", "docker"], input=cmd + "\n", text=True,
                               capture_output=True, timeout=timeout)
        else:
            p = subprocess.run(["docker"] + args, text=True, capture_output=True, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError("docker %s: %s" % (" ".join(args[:3]), (p.stderr or p.stdout)[-400:]))
        return p.stdout


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'" if re.search(r"[^\w@%+=:,./-]", s) else s


def start(dk, c):
    b = c["bed"]
    d = os.path.join(FLEET, c["name"])
    for sub in ("gcodes", "logs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    cname = "pt-%02d" % c["n"]
    dk.run(["rm", "-f", "-v", cname], check=False)
    dk.run(["volume", "rm", "-f", cname + "-config", cname + "-db"], check=False)
    box, gantry, prot = GEOMETRY[b[4]]
    m = BED_MARGIN
    env = {"SPEED": "20", "PUID": str(os.getuid()), "PGID": str(os.getgid()),
           "BED_X": str(b[1]), "BED_Y": str(b[2]), "BED_Z": str(b[3]),
           "SETUP": "1", "SETUP_BED": "%d,%d,%d,%d" % (m, m, b[1] - m, b[2] - m),
           "SETUP_TOOLHEAD": box, "SETUP_GANTRY": str(gantry), "SETUP_PROTRUSION": str(prot),
           "ACCEL": str(b[5]), "SHAKETUNE": str(b[5]), "ENCLOSED": str(b[6])}
    args = ["run", "-d", "--name", cname, "--memory", "1g", "-p", "127.0.0.1:%d:80" % c["port"],
            "-v", "%s:/home/pi/host:ro" % REPO,
            "-v", "%s:/home/pi/printer_data/gcodes" % os.path.join(d, "gcodes"),
            "-v", "%s:/home/pi/printer_data/logs" % os.path.join(d, "logs"),
            "-v", "%s-config:/home/pi/printer_data/config" % cname,
            "-v", "%s-db:/home/pi/printer_data/database" % cname]
    for k, v in env.items():
        args += ["-e", "%s=%s" % (k, v)]
    dk.run(args + ["virtual-kalico"])
    return cname


def stop(dk, cname):
    dk.run(["rm", "-f", "-v", cname], check=False)
    dk.run(["volume", "rm", "-f", cname + "-config", cname + "-db"], check=False)


def wait_ready(port, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/printer/info" % port, timeout=5) as r:
                st = json.loads(r.read())["result"]["state"]
                if st == "ready":
                    return "ready"
                if st in ("error", "shutdown"):
                    return st
        except Exception:
            pass
        time.sleep(3)
    return "timeout"


def wait_console(port, timeout=120):
    """Klippy can report ready before Moonraker has subscribed to the console; a prompt
    sent before that is lost and the driver waits forever. Echo until it shows up."""
    base = "http://127.0.0.1:%d" % port
    t0 = time.time()
    while time.time() - t0 < timeout:
        tag = "fleet-ready-%d" % int(time.time() * 1000)
        try:
            req = urllib.request.Request(base + "/printer/gcode/script", method="POST",
                                         data=json.dumps({"script": "RESPOND MSG=" + tag}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            time.sleep(1)
            with urllib.request.urlopen(base + "/server/gcode_store?count=20", timeout=10) as r:
                if any(tag in m["message"] for m in json.loads(r.read())["result"]["gcode_store"]):
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def check_gcode(c):
    """Run the offline checker on the session file the printer generated."""
    gdir = os.path.join(FLEET, c["name"], "gcodes")
    files = []
    for root, _, fs in os.walk(gdir):
        files += [os.path.join(root, f) for f in fs if f.endswith(".gcode")]
    if not files:
        return {"file": None}
    path = max(files, key=os.path.getmtime)
    # the geometry comes from the session's printune_geometry header
    p = subprocess.run([sys.executable, os.path.join(REPO, "tools", "check_gcode.py"), path],
                       capture_output=True, text=True, timeout=1800)
    out = p.stdout + p.stderr
    res = {"file": os.path.relpath(path, FLEET), "ok": p.returncode == 0}
    for key, pat in (("plates", r"plates: (\d+)"), ("outside_bed", r"outside bed: (\d+)"),
                     ("onto_other", r"extrusions onto another object: (\d+)"),
                     ("gantry", r"gantry over prints.*?: (\d+) moves"),
                     ("toolhead", r"toolhead box over finished prints.*?: (\d+) moves"),
                     ("crashes", r"nozzle crashes on travels: (\d+)")):
        m = re.search(pat, out)
        res[key] = int(m.group(1)) if m else None
    if not p.returncode == 0:
        res["error"] = out[-500:]
    return res


def drive(c):
    d = os.path.join(FLEET, c["name"])
    log = os.path.join(d, "run.jsonl")
    picks = ["^Printune Tune in this order=%s" % ("Tuning Wizard" if c["proc"] == "wizard" else "Advanced tuning"),
             "which filament=%s" % c["fil"]] + PICKS_COMMON
    args = [sys.executable, os.path.join(HERE, "drive_session.py"), "--script", "PRINTUNE",
            "--host", "127.0.0.1:%d" % c["port"], "--log", log, "--max-hours", "1.0"]
    for p in picks:
        args += ["--pick", p]
    p = subprocess.run(args, capture_output=True, text=True)
    with open(os.path.join(d, "driver.out"), "w") as f:
        f.write(p.stdout + p.stderr)
    res = {"driver_rc": p.returncode, "errors": None, "end_state": None, "prompts": 0, "plates": 0}
    try:
        for ln in open(log):
            r = json.loads(ln)
            if r.get("kind") == "summary":
                res["errors"] = r.get("errors")
            elif r.get("kind") == "end":
                res["end_state"] = r.get("print_state")
                res["end_message"] = r.get("message")
            elif "title" in r:
                res["prompts"] += 1
                if "clear the plate" in r["title"]:
                    res["plates"] += 1
    except FileNotFoundError:
        pass
    return res


def one(dk, c, lock, results):
    t0 = time.time()
    rec = {"name": c["name"], "bed": "%dx%dx%d" % c["bed"][1:4], "toolhead": c["bed"][4],
           "accel": c["bed"][5], "enclosed": c["bed"][6], "proc": c["proc"], "filament": c["fil"]}
    cname = None
    try:
        cname = start(dk, c)
        rec["ready"] = wait_ready(c["port"])
        if rec["ready"] == "ready" and not wait_console(c["port"]):
            rec["ready"] = "no console"
        if rec["ready"] == "ready":
            rec.update(drive(c))
            rec["check"] = check_gcode(c)
    except Exception as ex:
        rec["exception"] = str(ex)[-500:]
    finally:
        if cname:
            stop(dk, cname)
    rec["minutes"] = round((time.time() - t0) / 60, 1)
    with lock:
        results.append(rec)
        print("[%2d/%d] %-22s %s errors=%s end=%s plates=%s check=%s (%.1f min)" % (
            len(results), TOTAL, c["name"], rec.get("ready"), rec.get("errors"), rec.get("end_state"),
            rec.get("plates"), _short(rec.get("check")), rec["minutes"]), flush=True)
        _write(results)


def _short(ch):
    if not ch:
        return "-"
    if not ch.get("file"):
        return "no gcode"
    return "oob=%s crash=%s toolhead=%s gantry=%s onto=%s" % (
        ch.get("outside_bed"), ch.get("crashes"), ch.get("toolhead"), ch.get("gantry"), ch.get("onto_other"))


def _write(results):
    with open(os.path.join(FLEET, "report.json"), "w") as f:
        json.dump(sorted(results, key=lambda r: r["name"]), f, indent=1)


def verdict(r):
    ch = r.get("check") or {}
    if r.get("ready") != "ready" or r.get("exception"):
        return "FAIL (printer)"
    if r.get("errors") or r.get("end_state") != "complete":
        return "FAIL (session)"
    if any(ch.get(k) for k in ("outside_bed", "crashes", "toolhead", "gantry", "onto_other")):
        return "FAIL (G-code check)"
    if ch.get("file") and any(ch.get(k) is None for k in ("outside_bed", "crashes", "toolhead", "gantry")):
        return "FAIL (checker output not understood)"
    if not ch.get("file"):
        return "FAIL (no G-code)"
    return "pass"


def write_md(results):
    rows = sorted(results, key=lambda r: r["name"])
    lines = ["# Printune fleet run", "", "%d runs, %d passed." % (len(rows), sum(verdict(r) == "pass" for r in rows)),
             "", "| run | bed | toolhead | accel | proc | filament | result | errors | end | plates | outside bed | crashes | toolhead box | gantry | onto other | min |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ch = r.get("check") or {}
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["name"], r["bed"], r["toolhead"], "yes" if r["accel"] else "no", r["proc"], r["filament"], verdict(r),
            r.get("errors"), r.get("end_state"), ch.get("plates"), ch.get("outside_bed"), ch.get("crashes"),
            ch.get("toolhead"), ch.get("gantry"), ch.get("onto_other"), r["minutes"]))
    for r in rows:
        if verdict(r) != "pass":
            lines += ["", "## %s: %s" % (r["name"], verdict(r))]
            for k in ("exception", "end_message"):
                if r.get(k):
                    lines.append("- %s: %s" % (k, r[k]))
            if (r.get("check") or {}).get("error"):
                lines.append("- checker: %s" % r["check"]["error"])
            lines.append("- logs: fleet/%s/ (driver.out, run.jsonl, logs/klippy.log)" % r["name"])
    with open(os.path.join(FLEET, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    global TOTAL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--only", help="comma-separated run names")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--newgrp", action="store_true", help="call docker through 'newgrp docker'")
    a = ap.parse_args()
    cs = configs()
    if a.list:
        for c in cs:
            print("%2d %-22s bed %dx%dx%d %s accel=%d enclosed=%d port %d" % (
                (c["n"], c["name"]) + c["bed"][1:] + (c["port"],)))
        return 0
    if a.only:
        want = set(a.only.split(","))
        cs = [c for c in cs if c["name"] in want]
    TOTAL = len(cs)
    os.makedirs(FLEET, exist_ok=True)
    dk = Docker(a.newgrp)
    results, lock = [], threading.Lock()
    print("fleet: %d runs, %d at a time" % (len(cs), a.jobs), flush=True)
    t0 = time.time()
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for c in cs:
            ex.submit(one, dk, c, lock, results)
            time.sleep(2)            # stagger the starts
    write_md(results)
    print("done in %.0f min: %d/%d passed, report: %s" % (
        (time.time() - t0) / 60, sum(verdict(r) == "pass" for r in results), len(results),
        os.path.join(FLEET, "report.md")))
    return 0


TOTAL = 0
if __name__ == "__main__":
    sys.exit(main())
