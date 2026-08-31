# juno-kde-fancontrol

KDE/Qt6 fan-curve editor for Juno (Clevo) laptops, on top of the existing
`fan-profile` → `/etc/fancontrol` → `fancontrol.service` stack. Standalone
app (System 6 KCMs are C++ plugins; this stays pure Python/PySide6).

![quiet](screenshot.png)

## What it does

- Live strip: CPU package temp, per-fan RPM, current PWM %, control mode
  (fancontrol manual vs EC auto), refreshed every second.
- Draggable curve chart: `pwm = f(temp)`, same control law `fancontrol`
  applies (`MINPWM` below MINTEMP, integer ramp `MINSTOP→MAXPWM`, `MAXPWM`
  above MAXTEMP). Two handles: `(MINTEMP, MINPWM)` and `(MAXTEMP, MAXPWM)`.
  The dashed green marker is the live `(temp, pwm)` point; the red dashed
  line is the calibrated noise cap from `/etc/fan-profile.maxpwm`.
- Presets are scraped from `/usr/local/bin/fan-profile` at runtime — the
  GUI can never drift from the CLI table (quiet/balanced/cool/turbo). Any
  edit turns the profile into `custom`.
- `Automatic (EC firmware)` hands the fans back to the EC
  (`fan-profile auto`).
- Apply runs `pkexec /usr/sbin/juno-fancontrol-apply` (or the `/usr/local`
  variant from a manual install), which writes `/etc/fancontrol` in
  fan-profile's exact byte format (hwmon indices re-resolved from `/sys` at
  apply time — they drift between boots), honors the calibrated cap unless
  the preset is `turbo`, validates with `fancontrol --check` (syntax), then
  restarts `fancontrol.service` and probes `is-active` (the real device
  gate — `regen` in the service's ExecStartPre re-validates indices). On any
  failure it restores the previous config AND restarts the previous daemon.

## Requirements

- This laptop family: `clevofan` + `coretemp` hwmon, `fancontrol`,
  `fan-profile` (`~/system-fixes/11-fan-control-fancontrol.sh`).
- `python3-pyside6` (Qt6 ≥ 6.4), `pkexec` + a polkit agent (Plasma ships one).
- **Custom (non-preset) curves need the regen fix in `fan-profile`**
  (`~/system-fixes/fan-profile`, tests T9/T10 pin the contract): the
  service's `ExecStartPre=/usr/local/bin/fan-profile regen` replays the
  preset table at every (re)start, so an unpatched fan-profile rejects the
  `custom` label and the apply rolls back. One-time install:
  `sudo install -m755 ~/system-fixes/fan-profile /usr/local/bin/fan-profile`

## Install

Debian package (preferred — apt-tracked files under `/usr`):

```sh
bash tests/run-container.sh        # validates everything; deb lands in tests/out/deb/
sudo apt install ./tests/out/deb/juno-kde-fancontrol_*_all.deb
```

From source (files under `/usr/local`):

```sh
sudo bash install.sh
```

Run: `juno-kde-fancontrol`, or the "Juno Fan Control" launcher.

## Build the package

Target distro is Debian unstable (`debian/control` Depends resolve there).
Two ways:

```sh
bash tests/run-container.sh                     # clean debian:unstable container
# or natively:
dpkg-buildpackage -us -uc -b --root-command=fakeroot   # needs dpkg-dev debhelper
```

(debhelper-compat 13; the package is `Architecture: all`, `3.0 (native)`.)

## Validation

```sh
bash tests/run-container.sh        # clean debian:unstable container: 22 unit
                                   # tests, 24 helper integration checks
                                   # (regen label contract vs the real
                                   # fan-profile), deb build+install+verify,
                                   # 3 offscreen GUI renders
python3 tools/vision_check.py      # sends tests/out/*.png to the Flatiron
                                   # vision endpoint (Kimi-K3) with a
                                   # blank-window negative control
```

`tests/out/` is git-ignored scratch. `tools/vision_check.py` needs
`~/.config/fi-llm-token`.

## Files

| path | role |
|---|---|
| `app.py` | PySide6 GUI (chart, editor, live sensors, pkexec apply) |
| `backend/fancore.py` | pure-python core: config parse/emit, hwmon discovery, sensors, control law |
| `juno-fancontrol-apply` | root helper; fan-profile-compatible writer + service restart |
| `org.juno.kdefancontrol.policy.in` | polkit policy template (`@HELPER@` path substituted) |
| `scripts/juno-kde-fancontrol` | installed entry point (`/usr/bin` or `/usr/local/bin`) |
| `debian/` | package build (debhelper-compat 13, architecture: all) |
| `tests/` | pytest suite, helper shell tests, deb gate, offscreen render harness |
| `tools/vision_check.py` | screenshot vision validation via the inference endpoint |
