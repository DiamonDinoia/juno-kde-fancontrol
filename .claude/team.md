# Team state — tray dashboard, per-fan curves, autostart

### Goal
Ship three coherent changes in the fan stack: (1) the editor stops leaking one
fan's settings into the other — a preset applies only to the fan being edited
and native configs carry per-pwm bands; (2) the tray popup becomes a live
dashboard — CPU/dGPU temperature gauges, separate CPU and GPU usage charts,
network rate, power, and an explicit which-GPU-is-running indicator; (3) the
tray starts at login without intervention.

The trigger for (1): verified 2026-09-01 on the live 0.4.0 code — clicking
"Turbo" while editing the CPU fan wipes the GPU fan's knob curve in the editor
state and the apply carries one shared band for both pwms. fancontrol supports
per-pwm values natively (`MINTEMP=hwmon7/pwm1=40 hwmon7/pwm2=60`); the stack
just never emitted them.

### Deliverables
- [x] D1: editor presets touch only the selected fan; a native apply on a dGPU
  machine writes per-pwm bands (cpu band on pwm1, gpu band on pwm2); knob mode
  keeps its per-fan curves. An existing GPU knob curve survives a CPU preset
  click. check: `cd /home/marco/repos/juno/juno-kde-fancontrol && QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_app_init.py -q && bash tests/test_apply_helper.sh 2>&1 | grep -Eq 'helper tests: [0-9]+ passed, 0 failed' && grep -q test_preset_touches_only_the_selected_fan tests/test_app_init.py`
- [x] D2: tray popup shows CPU and dGPU temperatures as gauges (bar + value),
  following the KDE scheme like every other painting in the app. check:
  `cd /home/marco/repos/juno/juno-kde-fancontrol && QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_tray.py -k gauge -q && grep -q test_ tests/test_tray.py`
- [x] D3: tray popup shows the utilization over time as two charts — one for
  the CPU and one for the GPU (iGPU series, plus dGPU while awake). check:
  `cd /home/marco/repos/juno/juno-kde-fancontrol && QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_tray.py -q`
- [x] D4: tray popup has an explicit "which GPU is running" indicator (dGPU
  awake/active vs off). check: `cd /home/marco/repos/juno/juno-kde-fancontrol && QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_tray.py -k "mux or gpu_state" -q`
- [x] D5: the package autostarts the tray at login (XDG autostart entry);
  install.sh installs the same entry. check: `cd /home/marco/repos/juno/juno-kde-fancontrol && desktop-file-validate juno-fan-monitor-autostart.desktop && grep -q juno-fan-monitor-autostart.desktop debian/install`
- [x] D6: all probes/panels stay individually switchable (existing
  customization is not regressed). check: `cd /home/marco/repos/juno/juno-kde-fancontrol && QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_tray.py tests/test_vision_check.py -q`

Verification happens in the container; the laptop install is done by the user
with sudo, not by agents.

### Dispatch preferences
- Concurrency: parallel (user: "all at the same time")
- Experts: dispatched (team-expert worktrees; manager inline when infeasible)

### Pool
| Role | Specialty |
|------|-----------|
| fancontrol-domain | fancontrol config model, root helper, fan-profile, bash |
| qt-ui | PySide6 widgets, offscreen tests, ktheme colours |
| packaging | deb, XDG autostart, install.sh, container gate |

### Split
| # | Subproblem | Role | Scope | Done condition | Depends on | Status |
|---|------------|------|-------|----------------|------------|--------|
| SP1 | XDG autostart: install the EXISTING juno-fan-monitor.desktop into /etc/xdg/autostart (deb + install.sh); tray.py main() gets wait-for-tray retry + QLockFile single-instance | packaging | debian/install, install.sh, tray.py (main()/Monitor.__init__ ONLY), tests/test_deb.sh, tests/container-entry.sh (installsh lane line only), tests/test_tray.py (one retry/guard unit test), README.md (## Install + file tables) | D5 green + container deb/install lanes green | — | open |
| SP2 | Per-fan independence: preset seeds only the selected fan; preset badge follows the selected fan; native configs emit per-pwm bands when fans differ; helper --gpu-band; fan-profile/fan-calibrate regen carry per-pwm values, never flatten; render_app editor renders cover it | fancontrol-domain | app.py, backend/fancore.py, juno-fancontrol-apply, fan-profile, fan-calibrate, tests/render_app.py, tests/test_app_init.py, tests/test_fancore.py, tests/test_apply_helper.sh, tests/mutate.sh (M36-M39 block), README.md (knob/preset + badge bullets) | D1 green; sweep model block all-fired | — | open |
| SP3 | Tray dashboard: gauges REPLACE cpu/gpu rows (scheme colours); two charts (CPU; GPU = iGPU + dGPU-while-awake); which-GPU-runs indicator; new widgets join probe toggles with legacy-key migration (read-side); vision control matrix grows to dual charts | qt-ui | tray.py (Panel/PROBES/widgets; main() EXCLUDED), tests/test_tray.py, tests/test_sysmon.py, tests/render_tray.py, tools/vision_check.py, tests/test_vision_check.py, tests/container-entry.sh, tests/mutate.sh (M40-M43 block), tray.png, README.md (## The tray monitor) | D2, D3, D4, D6 green; sweep tray block all-fired | — | open |

Overlap rule: no subproblem touches debian/changelog or README's Validation
section — manager writes both at integration (one 0.5.1 entry). README
sections disjoint as listed; tests/mutate.sh / tests/container-entry.sh get
appended, marked regions. fan-profile's KNOB_HELPER/cap_knobs/preset-table
names stay stable for test_deb.sh greps (interface contract SP1 relies on).
Merge order SP3, SP2, SP1: autostart rolls out last.

### Rounds
| Round | Event | Accepted findings | Rejected findings |
|-------|-------|-------------------|-------------------|
| 0 | 3 critics (domain, qt-ui, packaging) attacked the draft split | README per-section ownership (SP1: Install+tables; SP2: knob/preset+badges; SP3: tray; manager: Validation counts+changelog); mutate.sh pre-carved M36-M39 model / M40-M43 tray; render_app.py+editor renders to SP2; fan-calibrate to SP2 with a flatten-and-recap mutation (regen silently collapses per-pwm bands — empirical, no pre-fix fatal); test_sysmon.py to SP3, sysmon.py OUT (Snapshot already carries every reading); SP1 installs EXISTING desktop file into /etc/xdg/autostart (no twin); SP1 gets tray.py main() only: wait-for-tray retry (autostart races StatusNotifierHost) + QLockFile single-instance; probe-key migration policy (legacy cpu/gpu keys mapped read-side); gauges REPLACE cpu/gpu rows; dashboards gates: vision control matrix grows for dual charts; SP1 merges after SP3 | none |

| 1 | 3 experts in parallel; SP2 attempt 1 dropped out (no output, clean tree) -> re-dispatched as attempt 2; reviewers: SP3 ACCEPT, SP1 ACCEPT, SP2 PRUNE fan-profile:238-245 (show_status called regen_value before its defining statement executed - verified rc=127 live); prune applied by manager, gates re-run green (193 pytest, 125 helper, 0 failed) | sweep M27 never-wake held on all three; byte-stability EXPECTED_QUIET unmoved | SP2 status pretty-print 'pwm2 band' block (pruned; raw dual-band lines still visible) |

| 1-final | Fresh-context reviewer ran every deliverable check and the full verification command (run-container, 15 lanes) on team/integration: ALL GREEN | 210 pytest / 125 helper / 0 failed on integration | - |

### Verification
Build and test: `cd /home/marco/repos/juno/juno-kde-fancontrol && bash tests/run-container.sh` (runs the full pytest suite, helper suite, deb gate, offscreen renders in a clean debian:unstable container). Before final push also: sweep `bash tests/mutate.sh` from a scratch copy, expecting fired=ALL and missed=0.

### Notes
- Round 1 closed ALL GREEN; integration merged to main as fast-forward (team/integration is the delivery).
- Round 1 starting branch: main (a39427d). team/integration created from it; the main checkout now tracks team/integration for the duration of the run; return to main at end.
- User answered the Interview questions in prose and dismissed the modal;
  defaults recorded here: tray icon keeps the CPU temperature; autostart is
  system-wide (no opt-in toggle); dispatch prefs defaulted as above.
- repo publishes via CI: juno build.yml -> builds release (digest-verified);
  apt passthrough publishes to the `repo` release. Laptop bootstrap
  diamondinoia-apt is at 1.10, so a higher juno-kde-fancontrol version becomes
  installable after the apt publish; user installs with sudo.
- Live machine, 2026-09-01: 0.4.0 installed; 0.5.0 published but not yet
  installed; /etc/fancontrol still carries pwm2=coretemp until the user runs
  `sudo fan-profile quiet` (verified rootless that the 0.4.0 script rewrites
  it to the gpu source).
- Never probe a suspended dGPU (read_dgpu never-wake ordering is load-bearing,
  pinned by sweep M27). No host sudo for any agent; rootful steps run in
  podman (debian:unstable/testing).
