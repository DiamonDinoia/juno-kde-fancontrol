"""The vision gate must tell an endpoint flake apart from a rendering defect.

A 504 retried as a failure would report a broken screenshot that is fine; a
401 retried three times just burns three minutes before saying the same thing.
"""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import vision_check  # noqa: E402


def _endpoint(*outcomes):
    """Endpoint whose _describe replays `outcomes`, counting the calls."""
    ep = vision_check.Endpoint("http://x/v1", "tok", "m")
    calls = []

    def fake(png, prompt):
        calls.append(prompt)
        out = outcomes[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    ep._describe = fake
    return ep, calls


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "boom", {}, None)  # type: ignore[arg-type]


def test_gateway_timeout_is_retried_and_can_succeed():
    ep, calls = _endpoint(_http(504), _http(502), {"window_visible": True})
    assert ep.describe(b"", "p") == {"window_visible": True}
    assert len(calls) == 3


def test_auth_failure_fails_on_the_first_attempt():
    ep, calls = _endpoint(_http(401), {"window_visible": True})
    with pytest.raises(urllib.error.HTTPError):
        ep.describe(b"", "p")
    assert len(calls) == 1, "a 4xx must not be retried"


def test_token_starved_empty_content_is_retried():
    ep, calls = _endpoint(RuntimeError("empty content"), {"window_visible": True})
    assert ep.describe(b"", "p") == {"window_visible": True}
    assert len(calls) == 2


def test_persistent_failure_still_raises():
    ep, calls = _endpoint(_http(504), _http(504), _http(504))
    with pytest.raises(urllib.error.HTTPError):
        ep.describe(b"", "p")
    assert len(calls) == 3


def test_tray_shots_get_the_tray_prompt():
    assert vision_check.prompt_for("tray-dark") is vision_check.TRAY_PROMPT
    assert vision_check.prompt_for("shot-quiet") is vision_check.APP_PROMPT
    assert vision_check.prompt_for("control") is vision_check.APP_PROMPT


def test_tray_rubric_flags_a_missing_chart():
    good = {"panel_visible": True, "utilization_charts": 2, "chart_has_line": True,
            "temp_gauges": 2,
            "rows": {"Compute GPU": "dGPU (NVIDIA)",
                     "CPU fan": "2560 RPM 31%", "GPU fan": "2480 RPM 31%",
                     "IGPU": "23% busy", "NET": "down 1.0 kB/s",
                     "POWER": "32 W", "BATTERY": "77%",
                     "CPU": "74 °C", "GPU": "61 °C"},
            "is_dark_theme": False, "defects": "none"}
    assert vision_check.tray_rubric("tray-dashboard", good) == []
    # Chart count slipped to one — or none: both are the defect the control
    # render and the unit tests exist to catch.
    assert vision_check.tray_rubric("tray-dashboard", {**good, "utilization_charts": 1})
    assert vision_check.tray_rubric("tray-dashboard", {**good, "utilization_charts": 0})
    assert vision_check.tray_rubric("tray-dashboard", {**good, "temp_gauges": 0})
    assert vision_check.tray_rubric("tray-dashboard", {**good, "temp_gauges": 1})
    assert vision_check.tray_rubric("tray-dashboard", {**good, "rows": {}})
    assert vision_check.tray_rubric("tray-dark", good), "dark shot must look dark"
    assert vision_check.tray_rubric("tray-off", good), "suspended shot must say so"
    suspended = {**good["rows"], "Compute GPU": "iGPU (Intel Arc)", "GPU": "suspended"}
    assert vision_check.tray_rubric("tray-off", {**good, "rows": suspended}) == []


def test_tray_rubric_needs_a_temperature_somewhere():
    """The gauges carry the °C readings; a render where none is readable is a
    dashboard without its headline numbers."""
    good = {"panel_visible": True, "utilization_charts": 2, "chart_has_line": True,
            "temp_gauges": 2,
            "rows": {"Compute GPU": "dGPU (NVIDIA)", "CPU fan": "2560 RPM 31%",
                     "GPU fan": "2480 RPM 31%", "IGPU": "23% busy",
                     "NET": "down 1.0 kB/s", "POWER": "32 W", "BATTERY": "77%",
                     "CPU": "74 °C"},
            "is_dark_theme": False, "defects": "none"}
    assert vision_check.tray_rubric("tray-dashboard", good) == []
    bare = {**good, "rows": {k: v for k, v in good["rows"].items() if k != "CPU"}}
    assert vision_check.tray_rubric("tray-dashboard", bare)


def test_tray_rubric_pins_the_customized_layout():
    """tray-min must show exactly the enabled probes: a leak of a hidden row, or
    a missing visible one, is a defect the rubric has to name. The gauges stay
    on there; the charts are off."""
    good = {"panel_visible": True, "utilization_charts": 0, "temp_gauges": 2,
            "rows": {"CPU": "74 °C", "GPU": "61 °C",
                     "Compute GPU": "dGPU (NVIDIA)",
                     "CPU fan": "2560 RPM 31%", "GPU fan": "2480 RPM 31%",
                     "NET": "down 1.0 kB/s"},
            "is_dark_theme": False, "defects": "none"}
    assert vision_check.tray_rubric("tray-min", good) == []
    leaked = {**good, "rows": {**good["rows"], "BATTERY": "77%"}}
    assert vision_check.tray_rubric("tray-min", leaked)
    lost = {**good, "rows": {k: v for k, v in good["rows"].items() if k != "GPU fan"}}
    assert vision_check.tray_rubric("tray-min", lost)
    charted = {**good, "utilization_charts": 2}
    assert vision_check.tray_rubric("tray-min", charted)
    charted_one = {**good, "utilization_charts": 1}
    assert vision_check.tray_rubric("tray-min", charted_one)


def test_knob_rubric_needs_multiple_handles() -> None:
    """The knob shot must be distinguishable from a 2-point ramp, or the gate
    would pass on a screenshot where clicking never added anything."""
    good = dict(window_visible=True, curve_chart=True, chart_has_line=True,
                axis_labels=["CPU temperature (°C)"],
                buttons=["Quiet", "Turbo", "Apply", "Automatic (EC firmware)",
                         "Back to 2-point"],
                status_line="CPU 74 °C  fan1 2560 RPM", is_dark_theme=False,
                curve_handles=5, curve_shape="multi-segment", defects="none")
    assert vision_check.rubric("shot-knobs", good) == []
    for field, value, want in [("curve_handles", 2, "handles"),
                               ("curve_shape", "straight", "straight ramp"),
                               ("buttons", ["Quiet", "Turbo", "Apply", "Automatic"],
                                "Back to 2-point")]:
        errs = vision_check.rubric("shot-knobs", {**good, field: value})
        assert any(want in e for e in errs), (field, errs)


def test_two_point_rubric_rejects_a_knob_answer() -> None:
    """The other side of the same check: a 2-point shot reporting five handles
    is either a wrong render or a model guessing, and must not pass."""
    base = dict(window_visible=True, curve_chart=True, chart_has_line=True,
                axis_labels=["CPU temperature (°C)"],
                buttons=["Quiet", "Turbo", "Apply", "Automatic (EC firmware)"],
                status_line="CPU 74 °C  fan1 2560 RPM", is_dark_theme=False,
                curve_handles=2, curve_shape="straight", defects="none")
    assert vision_check.rubric("shot-quiet", base) == []
    errs = vision_check.rubric("shot-quiet", {**base, "curve_handles": 5})
    assert any("2-point shot should show 2 handles" in e for e in errs), errs


def test_gpu_knob_rubric_needs_the_gpu_evidence() -> None:
    """The GPU shot passes only if the selector and the GPU temperature made it
    into the picture — a render of the CPU tab by another name must not pass."""
    good = dict(window_visible=True, curve_chart=True, chart_has_line=True,
                axis_labels=["GPU temperature (°C)"],
                buttons=["CPU fan (pwm1)", "GPU fan (pwm2)", "Quiet", "Turbo",
                         "Apply", "Automatic (EC firmware)", "Back to 2-point"],
                status_line="CPU 74 °C  fan1 2560 RPM  GPU 67 °C", is_dark_theme=False,
                curve_handles=4, curve_shape="multi-segment", defects="none")
    assert vision_check.rubric("shot-gpu-knobs", good) == []
    assert vision_check.rubric("shot-gpu-knobs", {**good, "curve_handles": 2})
    assert vision_check.rubric("shot-gpu-knobs",
                               {**good, "buttons": [b for b in good["buttons"]
                                                    if "GPU" not in b]})
    assert vision_check.rubric("shot-gpu-knobs",
                               {**good, "status_line": "CPU 74 °C  fan1 2560 RPM"})
