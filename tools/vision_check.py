#!/usr/bin/env python3
"""Vision-validate rendered screenshots against the Flatiron inference endpoint.

A blank-window control image is checked FIRST: if the model reports UI
content on it, the validator is untrustworthy and the run fails with rc 2.
Per-shot rubric failures give rc 1.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

PROMPT = """You are validating a screenshot of a Qt desktop app called "Juno Fan Control".
Look carefully and reply with ONLY a JSON object (no markdown fences):
{
 "window_visible": true/false,
 "curve_chart": true/false,        // a chart plotting fan PWM vs CPU temperature?
 "chart_has_line": true/false,     // a drawn curve line inside the chart?
 "axis_labels": ["..."],           // axis titles / tick labels you can read
 "buttons": ["..."],               // visible button / radio labels
 "status_line": "...",             // verbatim transcription of the top status line
 "is_dark_theme": true/false,
 "defects": "none" | "description" // blank areas, overlapping/cut-off text, missing widgets
}"""


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

    def describe(self, png: bytes) -> dict:
        body = json.dumps({
            "model": self.model, "max_tokens": 2500, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                {"type": "text", "text": PROMPT}]}],
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
        ctrl = ep.describe(blank_png())
    except Exception as e:  # endpoint down / auth broken / model changed
        print(f"CONTROL-ERROR: cannot reach vision endpoint: {e}")
        return 2
    ctrl_errs = rubric("control", ctrl)
    if not ctrl_errs:
        print(f"CONTROL-FAIL: model 'passed' a blank window: {ctrl}")
        return 2
    print(f"control ok (blank correctly flagged: {ctrl_errs[0]})")

    shots = sorted(Path(args.shots).glob("*.png"))
    if not shots:
        print(f"no screenshots under {args.shots}")
        return 2
    failures = 0
    for shot in shots:
        name = shot.stem
        try:
            d = ep.describe(shot.read_bytes())
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
            print(f"{name}: ok (curve {d.get('curve_chart')}, dark={d.get('is_dark_theme')}, "
                  f"status={str(d.get('status_line'))[:80]!r})")
    print(f"{'ALL OK' if failures == 0 else f'{failures} shot(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
