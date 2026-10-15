#!/usr/bin/env python3
"""Run a G-code file on the virtual printer and answer its Mainsail prompts.

Stdlib only. Talks to Moonraker over HTTP (through Mainsail's port, 8080, by default):
  - uploads the file and starts the print
  - reads the console (gcode_store), rebuilds each action:prompt_* dialog
  - answers it by "pressing" a button: sends the button's G-code
  - logs every prompt, the button pressed, and the printer state after it

Usage:
  drive_session.py session.gcode [--host 127.0.0.1:8080]
      [--pick 'TITLE_REGEX=LABEL_SUBSTRING' ...] [--log run.jsonl]
  drive_session.py --script PRINTUNE --pick 'Printune$=Tuning Wizard' ...
      (start with a command instead of a file, e.g. the Printune menu)

Without a matching --pick rule the middle regular button is pressed.
LABEL_SUBSTRING "-" presses nothing (wait, e.g. for a heat soak to end by itself).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

STATE_OBJS = {
    "print_stats": ["state", "message", "filename", "print_duration"],
    "pause_resume": ["is_paused"],
    "toolhead": ["position", "homed_axes"],
    "extruder": ["temperature", "target", "pressure_advance", "smooth_time"],
    "heater_bed": ["temperature", "target"],
    "temperature_sensor chamber": ["temperature"],
    "gcode_move": ["extrude_factor", "speed_factor", "homing_origin"],
    "firmware_retraction": [
        "retract_length",
        "retract_speed",
        "unretract_extra_length",
        "unretract_speed",
    ],
    "idle_timeout": ["state"],
    "exclude_object": ["excluded_objects", "current_object"],
}


class Moonraker:
    def __init__(self, host):
        self.base = "http://%s" % host

    def _req(self, method, path, body=None, headers=None, timeout=60):
        req = urllib.request.Request(
            self.base + path, data=body, method=method, headers=headers or {}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
            return d.get("result", d)

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._req("GET", path + q)

    def post(self, path, payload=None, timeout=60):
        return self._req(
            "POST",
            path,
            json.dumps(payload or {}).encode(),
            {"Content-Type": "application/json"},
            timeout,
        )

    def gcode(self, script, timeout=3600):
        return self.post("/printer/gcode/script", {"script": script}, timeout)

    def upload(self, path):
        bnd = uuid.uuid4().hex
        name = os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        body = (
            (
                "--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            )
            % (bnd, name)
        ).encode() + data + ("\r\n--%s--\r\n" % bnd).encode()
        return self._req(
            "POST",
            "/server/files/upload",
            body,
            {"Content-Type": "multipart/form-data; boundary=%s" % bnd},
            timeout=600,
        )

    def state(self):
        q = "&".join(
            "%s=%s" % (urllib.parse.quote(k), ",".join(v))
            for k, v in STATE_OBJS.items()
        )
        return self._req("GET", "/printer/objects/query?" + q)["status"]


def parse_button(rest):
    parts = rest.split("|")
    label = parts[0].strip()
    gcode = parts[1].strip() if len(parts) > 1 and parts[1].strip() else label
    return {"label": label, "gcode": gcode}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("gcode", nargs="?")
    ap.add_argument("--script", help="start by sending this G-code instead of a file")
    ap.add_argument("--host", default="127.0.0.1:8080")
    ap.add_argument("--pick", action="append", default=[])
    ap.add_argument("--log", default=None)
    ap.add_argument("--max-hours", type=float, default=6.0)
    a = ap.parse_args()
    rules = []
    for p in a.pick:
        k, _, v = p.rpartition("=")
        rules.append((re.compile(k, re.I), v))
    mr = Moonraker(a.host)
    if not a.gcode and not a.script:
        ap.error("give a G-code file or --script")
    log = open(a.log or (os.path.splitext(a.gcode)[0] if a.gcode else "script") + ".run.jsonl", "w")

    def emit(kind, **kw):
        kw.update(kind=kind, t=round(time.time() - t0, 1))
        log.write(json.dumps(kw) + "\n")
        log.flush()
        short = {k: v for k, v in kw.items() if k not in ("state", "lines")}
        print(json.dumps(short)[:300], flush=True)

    t0 = time.time()
    store = mr.get("/server/gcode_store", count=1000)["gcode_store"]
    last_t = store[-1]["time"] if store else 0.0
    if a.script:
        mr.gcode(a.script)
        emit("start", script=a.script)
    else:
        mr.upload(a.gcode)
        fname = os.path.basename(a.gcode)
        mr.post("/printer/print/start?" + urllib.parse.urlencode({"filename": fname}))
        emit("start", file=fname)
    prompt = None
    errors = []
    done_at = None
    not_ready = None
    while time.time() - t0 < a.max_hours * 3600:
        time.sleep(1.0)
        store = mr.get("/server/gcode_store", count=1000)["gcode_store"]
        new = [e for e in store if e["time"] > last_t]
        if new:
            last_t = new[-1]["time"]
        for e in new:
            msg = e["message"]
            if e.get("type") == "response" and (
                msg.startswith("!!") or "Error" in msg[:12]
            ):
                errors.append(msg)
                emit("error", msg=msg)
            m = re.match(r"^// action:(prompt_\w+)\s?(.*)$", msg)
            if not m:
                continue
            cmd, rest = m.group(1), m.group(2)
            if cmd == "prompt_begin":
                prompt = {"title": rest, "text": [], "buttons": [], "footer": []}
            elif prompt is None:
                continue
            elif cmd == "prompt_text":
                prompt["text"].append(rest)
            elif cmd == "prompt_button":
                prompt["buttons"].append(parse_button(rest))
            elif cmd == "prompt_footer_button":
                prompt["footer"].append(parse_button(rest))
            elif cmd == "prompt_end":
                prompt = None
            elif cmd == "prompt_show":
                p = prompt
                prompt = None
                allb = p["buttons"] + p["footer"]
                choice = None
                wait = False
                for rx, want in rules:
                    if rx.search(p["title"] + " " + " ".join(p["text"])):
                        if want == "-":
                            wait = True
                            break
                        for b in allb:
                            if want.lower() in b["label"].lower():
                                choice = b
                                break
                        if choice:
                            break
                if choice is None and p["buttons"] and not wait:
                    choice = p["buttons"][len(p["buttons"]) // 2]
                if choice is None and allb and not wait:
                    choice = allb[0]
                st = mr.state()
                emit(
                    "prompt",
                    title=p["title"],
                    text=p["text"],
                    buttons=[b["label"] for b in p["buttons"]],
                    footer=[b["label"] for b in p["footer"]],
                    pressed=choice and choice["label"],
                    gcode=choice and choice["gcode"],
                    state=st,
                )
                if done_at:
                    done_at = time.time()
                if choice:
                    time.sleep(0.5)
                    try:
                        mr.gcode(choice["gcode"])
                    except Exception as ex:
                        errors.append(str(ex))
                        emit("error", msg="button gcode failed: %s" % ex)
                    time.sleep(0.3)
                    emit("after", state=mr.state())
        info = mr.get("/printer/info")
        if info["state"] != "ready":
            # a RESTART/FIRMWARE_RESTART passes through "startup"; give it time
            not_ready = not_ready or time.time()
            if info["state"] in ("shutdown", "error") or time.time() - not_ready > 30:
                emit("end", print_state="klippy " + info["state"], message=info["state_message"])
                errors.append(info["state_message"])
                break
            continue
        not_ready = None
        st = mr.get(
            "/printer/objects/query?print_stats=state,message"
        )["status"]["print_stats"]
        if st["state"] in ("complete", "cancelled", "error"):
            # prompts can follow the end of the print (retraction picks,
            # summary): keep answering until it has been quiet for 10 s
            if done_at is None:
                done_at = time.time()
            elif time.time() - done_at > 10.0:
                emit("end", print_state=st["state"], message=st["message"], state=mr.state())
                break
    else:
        emit("timeout")
    emit("summary", errors=len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
