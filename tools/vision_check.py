#!/usr/bin/env python3
"""Vision-validate rendered screenshots against the Flatiron inference endpoint.

Two controls run FIRST, and both must be flagged by the rubric or the
validator is untrustworthy and the run fails with rc 2:
  1. a synthetic blank window (the model must not invent a UI), and
  2. every PNG under <shots>/control/, rendered by the real harness with a
     deliberate defect (the model must actually notice a missing widget).
Per-shot rubric failures on the real screenshots give rc 1.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

APP_PROMPT = """You are validating a screenshot of a Qt desktop app called "Juno Fan Control".
Look carefully and reply with ONLY a JSON object (no markdown fences):
{
 "window_visible": true/false,
 "curve_chart": true/false,        // a chart plotting fan PWM vs CPU temperature?
 "chart_has_line": true/false,     // a drawn curve line inside the chart?
 "curve_handles": <integer>,       // how many round dots sit ON the curve line?
 "curve_shape": "straight" | "multi-segment",  // does the curve bend more than once?
 "axis_labels": ["..."],           // axis titles / tick labels you can read
 "buttons": ["..."],               // visible button / radio labels
 "status_line": "...",             // verbatim transcription of the top status line
 "is_dark_theme": true/false,
 "defects": "none" | "description" // blank areas, overlapping/cut-off text, missing widgets
}"""

TRAY_PROMPT = """You are validating a screenshot of the system-tray popup panel of a Linux
laptop monitor called "Juno Fan Monitor".
Look carefully and reply with ONLY a JSON object (no markdown fences):
{
 "panel_visible": true/false,
 "utilization_chart": true/false,  // a chart of CPU/GPU utilization over time at the top?
 "chart_has_line": true/false,     // at least one drawn line inside that chart?
 "rows": {"LABEL": "verbatim text of that row"},  // the readout rows below the chart
 "is_dark_theme": true/false,
 "defects": "none" | "description" // blank areas, overlapping/cut-off text, missing widgets
}"""


def prompt_for(name: str) -> str:
    """Tray panels and app windows are different UIs and need different rubrics."""
    return TRAY_PROMPT if name.startswith("tray") else APP_PROMPT


def blank_png(w: int = 1020, h: int = 620, rgb: tuple[int, int, int] = (238, 238, 238)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class Endpoint:
    def __init__(self, base: str, token: str, model: str) -> None:
        self.base, self.token, self.model = base, token, model

    def describe(self, png: bytes, prompt: str, attempts: int = 3) -> dict:
        """Retry the endpoint's own failures: the reasoning trace occasionally
        eats the token budget and returns empty content, and the gateway
        answers 504 under load. Neither is a rendering defect. A 4xx is the
        caller's fault (bad token, unknown model) and must fail immediately."""
        for attempt in range(attempts):
            try:
                return self._describe(png, prompt)
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == attempts - 1:
                    raise
            except (RuntimeError, json.JSONDecodeError, urllib.error.URLError,
                    TimeoutError):
                if attempt == attempts - 1:
                    raise
        raise AssertionError("unreachable")

    def _describe(self, png: bytes, prompt: str) -> dict:
        body = json.dumps({
            "model": self.model, "max_tokens": 8000, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text", "text": prompt}]}],
        }).encode()
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            choice = json.load(r)["choices"][0]
        content = choice["message"].get("content")
        if not content:  # reasoning model token-starved: all budget went to the trace
            raise RuntimeError(f"empty content (finish_reason={choice.get('finish_reason')}); "
                               f"raise max_tokens")
        text = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.M).strip()
        return json.loads(text)


def rubric(name: str, d: dict) -> list[str]:
    """Return the list of violations (empty = pass)."""
    if name.startswith("tray"):
        return tray_rubric(name, d)
    errs = []
    if not d.get("window_visible"):
        errs.append("window not visible")
    if not d.get("curve_chart"):
        errs.append("curve chart missing")
    if not d.get("chart_has_line"):
        errs.append("curve line not drawn")
    axes = " ".join(map(str, d.get("axis_labels", []))).lower()
    if "temperature" not in axes and "°c" not in axes:
        errs.append(f"temperature axis label unreadable: {axes!r}")
    buttons = " ".join(map(str, d.get("buttons", []))).lower()
    for want in ("quiet", "turbo", "apply", "automatic"):
        if want not in buttons:
            errs.append(f"button/radio '{want}' missing (saw: {buttons!r})")
    status = str(d.get("status_line", ""))
    if not re.search(r"\d+\s*°?\s*c", status, re.I):
        errs.append(f"status line has no CPU temperature: {status!r}")
    if "rpm" not in status.lower():
        errs.append(f"status line has no fan RPM: {status!r}")
    if name == "shot-auto" and "ec auto" not in status.lower():
        errs.append(f"auto shot should report EC auto mode: {status!r}")
    if name == "shot-turbo-dark" and not d.get("is_dark_theme"):
        errs.append("dark shot does not look dark")
    # Handle count is checked on BOTH kinds of shot, so a model that always
    # answers "5" fails the 2-point shots and one that always answers "2" fails
    # the knob shot. Either way the field carries information.
    handles = d.get("curve_handles")
    if name in ("shot-knobs", "shot-gpu-knobs"):
        if not isinstance(handles, int) or handles < 4:
            errs.append(f"knob shot should show 5 handles on the curve, saw {handles!r}")
        if str(d.get("curve_shape", "")).lower().startswith("straight"):
            errs.append("knob shot draws a straight ramp, not a multi-segment curve")
        if "back to 2-point" not in buttons:
            errs.append(f"knob shot missing the 'Back to 2-point' button (saw: {buttons!r})")
    elif name.startswith("shot-"):
        if not isinstance(handles, int) or handles > 3:
            errs.append(f"2-point shot should show 2 handles, saw {handles!r}")
    if name == "shot-gpu-knobs":
        if "gpu fan" not in buttons:
            errs.append(f"gpu shot missing the fan selector (saw: {buttons!r})")
        if not re.search(r"gpu\s+\d+\s*°?\s*c", status, re.I):
            errs.append(f"gpu shot status has no GPU temperature: {status!r}")
    defects = str(d.get("defects", "")).strip().lower()
    if defects not in ("none", "", "no defects"):
        errs.append(f"defects reported: {defects}")
    return errs


def tray_rubric(name: str, d: dict) -> list[str]:
    errs = []
    if not d.get("panel_visible"):
        errs.append("panel not visible")
    if not d.get("utilization_chart"):
        errs.append("utilization chart missing")
    if not d.get("chart_has_line"):
        errs.append("no line drawn in the chart")
    rows = {str(k).strip().lower(): str(v) for k, v in (d.get("rows") or {}).items()}
    for want in ("cpu", "fans", "igpu", "dgpu", "net", "battery"):
        if want not in rows:
            errs.append(f"row '{want}' missing (saw: {sorted(rows)})")
    if not re.search(r"\d+\s*°?\s*c", rows.get("cpu", ""), re.I):
        errs.append(f"CPU row has no temperature: {rows.get('cpu', '')!r}")
    if "rpm" not in rows.get("fans", "").lower():
        errs.append(f"FANS row has no RPM: {rows.get('fans', '')!r}")
    if "/s" not in rows.get("net", ""):
        errs.append(f"NET row has no transfer rate: {rows.get('net', '')!r}")
    if name.endswith("dark") and not d.get("is_dark_theme"):
        errs.append("dark shot does not look dark")
    if name.endswith("off") and not re.search(r"suspend|off|absent|d3",
                                              rows.get("dgpu", ""), re.I):
        errs.append(f"suspended-dGPU shot does not say so: {rows.get('dgpu', '')!r}")
    defects = str(d.get("defects", "")).strip().lower()
    if defects not in ("none", "", "no defects"):
        errs.append(f"defects reported: {defects}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=str(Path(__file__).resolve().parent.parent / "tests" / "out"))
    ap.add_argument("--model", default=os.environ.get("FI_VISION_MODEL", "moonshotai/Kimi-K3"))
    ap.add_argument("--base", default=os.environ.get("FI_BASE_URL",
                                                     "https://inference.flatironinstitute.org/v1"))
    ap.add_argument("--token-file", default=os.environ.get("FI_TOKEN_FILE",
                                                           os.path.expanduser("~/.config/fi-llm-token")))
    args = ap.parse_args()

    token = Path(args.token_file).read_text().strip()
    ep = Endpoint(args.base, token, args.model)

    # --- negative control: a blank window must NOT pass the rubric ---
    try:
        ctrl = ep.describe(blank_png(), APP_PROMPT)
    except Exception as e:  # endpoint down / auth broken / model changed
        print(f"CONTROL-ERROR: cannot reach vision endpoint: {e}")
        return 2
    ctrl_errs = rubric("control", ctrl)
    if not ctrl_errs:
        print(f"CONTROL-FAIL: model 'passed' a blank window: {ctrl}")
        return 2
    print(f"control ok (blank correctly flagged: {ctrl_errs[0]})")

    # --- negative control 2: real renders with a deliberate defect ---
    # A blank image only proves the model does not hallucinate a whole UI. These
    # prove it notices a widget that is actually missing.
    defects = sorted((Path(args.shots) / "control").glob("*.png"))
    if not defects:
        print(f"CONTROL-ERROR: no defect controls under {Path(args.shots) / 'control'}")
        return 2
    for shot in defects:
        try:
            d = ep.describe(shot.read_bytes(), prompt_for(shot.stem))
        except Exception as e:
            print(f"CONTROL-ERROR: {shot.name}: {e}")
            return 2
        errs = rubric(shot.stem, d)
        if not errs:
            print(f"CONTROL-FAIL: model 'passed' the broken render {shot.name}: {d}")
            return 2
        print(f"control ok ({shot.name} correctly flagged: {errs[0]})")

    shots = sorted(Path(args.shots).glob("*.png"))
    if not shots:
        print(f"no screenshots under {args.shots}")
        return 2
    failures = 0
    for shot in shots:
        name = shot.stem
        try:
            d = ep.describe(shot.read_bytes(), prompt_for(name))
        except Exception as e:
            print(f"{name}: ERROR {e}")
            failures += 1
            continue
        errs = rubric(name, d)
        if errs:
            failures += 1
            print(f"{name}: FAIL")
            for e in errs:
                print(f"  - {e}")
        else:
            summary = (str(d.get("rows", ""))[:80] if name.startswith("tray")
                       else str(d.get("status_line"))[:80])
            print(f"{name}: ok (dark={d.get('is_dark_theme')}, {summary!r})")
    print(f"{'ALL OK' if failures == 0 else f'{failures} shot(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
