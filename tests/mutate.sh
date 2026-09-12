#!/bin/bash
# Positive controls. Break one thing, prove a named check fails.
#
# A gate that has never failed cannot be told apart from one that cannot fail,
# so every claim in README's Validation section that a check "would catch" a
# regression is backed by a mutation here. Run it after changing any of the
# files in FILES:
#
#     bash tests/mutate.sh
#
# Output is one line per mutation: the failing pytest ids, then the failing
# shell tags. A line with "none | none" is a gate that did not fire -- that is
# the failure this script exists to find, not a pass.
#
# restore runs from the EXIT trap, so a timeout or a kill cannot leave a
# mutation in the working tree (it did once, and every later run was then
# measuring the mutant).
#
# Each suite runs under `timeout` and its exit status is read from a file, not
# from a pipe: an M3 mutation once blocked pytest in a modal QMessageBox for
# 65 min, and a pipeline's status would have reported the grep, not the hang.
set -u
export PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1

# One sweep at a time: two instances mutating the same tree produce failures
# indistinguishable from a real regression. A pgrep guard cannot do this job --
# the launcher's own argv contains this script's path, so it matches itself.
LOCK=/tmp/juno-mutate.lock
exec 9>"$LOCK" || exit 1
flock -n 9 || { echo "another sweep holds $LOCK -- refusing to run"; exit 1; }
BK=$(mktemp -d)
OUT=$(mktemp -d)
FILES=(backend/fancore.py backend/ktheme.py backend/sysmon.py juno-fancontrol-apply fan-profile fan-calibrate app.py fancurve.py tray.py
       systemd/30-juno-fancontrol.conf debian/install debian/postinst debian/prerm install.sh)
save()    { for f in "${FILES[@]}"; do mkdir -p "$BK/$(dirname "$f")"; cp "$f" "$BK/$f"; done; }
restore() { find . -name __pycache__ -type d -prune -exec rm -rf {} + ; for f in "${FILES[@]}"; do cp "$BK/$f" "$f"; done; }
trap 'restore; rm -rf "$BK" "$OUT"; echo "[trap] tree restored"' EXIT

LIMIT=300
FIRED=0
MISSED=0

syslane() {  # -> space-separated failed static systemd/packaging checks (host-side), or empty
    # test_deb.sh's matching assertions only run in the container gate, which
    # the sweep cannot touch; these greps keep the same properties killable
    # here. Every check below is fired by at least one mutation.
    local s=""
    grep -qx 'Restart=on-failure' systemd/30-juno-fancontrol.conf || s+="dropin-restart-policy "
    grep -q '^Restart=always' systemd/30-juno-fancontrol.conf && s+="dropin-restart-always "
    grep -qx 'StartLimitIntervalSec=180' systemd/30-juno-fancontrol.conf || s+="dropin-startlimit-interval "
    grep -qx 'StartLimitBurst=3' systemd/30-juno-fancontrol.conf || s+="dropin-startlimit-burst "
    grep -qx 'PrivateDevices=no' systemd/30-juno-fancontrol.conf || s+="dropin-devices-hidden "
    grep -q 'systemd/fancontrol-sleep-noop' debian/install || s+="noop-not-shipped "
    ! grep -q 'systemd/fancontrol-resume' debian/install || s+="resume-hook-still-installed "
    grep -q 'dpkg-divert --package juno-kde-fancontrol --add' debian/postinst || s+="divert-missing "
    grep -q 'dpkg-divert --package juno-kde-fancontrol --remove' debian/prerm || s+="undivert-missing "
    grep -q 'ovr=(/etc/systemd/system/fancontrol.service.d/\*\.conf)' debian/postinst || s+="etc-dropin-warning-missing "
    # systemd-sleep executes every non-hidden executable in system-sleep/; the
    # divert target must stay dot-prefixed or both hooks run on resume.
    grep -q 'system-sleep/\.fancontrol\.juno-diverted' debian/postinst || s+="divert-target-visible-postinst "
    grep -q 'system-sleep/\.fancontrol\.juno-diverted' debian/prerm || s+="divert-target-visible-prerm "
    grep -q 'install -m 755 "$SLP_NOOP" "$SLP"' debian/postinst || s+="noop-reinstall-missing "
    # The pre-deb resume hook must stay reaped by both lanes (install.sh for
    # the source lane, postinst for the deb lane's unowned leftovers).
    grep -q 'rm -f /usr/lib/systemd/system-sleep/fancontrol-resume' install.sh || s+="installsh-hook-rm-missing "
    grep -q 'rm -f /usr/lib/systemd/system-sleep/fancontrol-resume' debian/postinst || s+="postinst-hook-rm-missing "
    # Diversion half-state (entry gone, stale .juno-diverted on disk, upstream
    # hook live) must move the stale copy aside and retry, not just warn.
    grep -q 'mv "$SLP_DIV" "$SLP_DIV.stale"' debian/postinst || s+="half-state-fallback-missing "
    echo "${s% }"
}

run() {  # -> "<failed pytest ids> | <failed shell tags> | <failed systemd checks>", or HUNG
    local py sh sys rc
    # The whole directory, never a list of files: a named list silently left
    # tests/test_ktheme.py out of this sweep for a whole pass.
    timeout "$LIMIT" python3 -m pytest tests -q -p no:cacheprovider > "$OUT/py" 2>&1
    rc=$?
    (( rc == 124 )) && { echo "PYTEST HUNG (${LIMIT}s)"; return; }
    py=$(grep '^FAILED' "$OUT/py" | sed 's/FAILED tests\///;s/ .*//' | tr '\n' ' ')

    timeout "$LIMIT" bash tests/test_apply_helper.sh > "$OUT/sh" 2>&1
    rc=$?
    (( rc == 124 )) && { echo "HELPER SUITE HUNG (${LIMIT}s)"; return; }
    # Strip from the first ": ", not the first ":" -- a T15 tag embeds the knob
    # spec (T15-reject[45:0]) and two distinct cases printed identically.
    sh=$(grep '^FAIL ' "$OUT/sh" | sed 's/^FAIL //;s/: .*//' | tr '\n' ' ')

    sys=$(syslane)

    echo "${py:-none} | ${sh:-none} | ${sys:-none}"
}

base=$(run)
echo "== baseline: $base"
if [[ "$base" != "none | none | none" ]]; then
    echo "BASELINE IS NOT GREEN -- refusing to attribute any failure to a mutation"
    exit 1
fi
save

mutate() { # mutate NAME FILE SED_EXPR
    restore
    sed -i "$3" "$2" || { echo "  $1: SED FAILED"; MISSED=$((MISSED+1)); return; }
    if cmp -s "$2" "$BK/$2"; then
        echo "  $1: MUTATION DID NOT APPLY"; MISSED=$((MISSED+1)); return
    fi
    local r; r=$(run)
    printf '  %-28s %s\n' "$1:" "$r"
    if [[ "$r" == "none | none | none" ]]; then MISSED=$((MISSED+1)); else FIRED=$((FIRED+1)); fi
}

pymutate() { # pymutate NAME FILE OLD NEW  -- for edits sed cannot express
    restore
    OLD="$3" NEW="$4" python3 - "$2" <<'EOF' || { echo "  $1: ANCHOR MISSING"; MISSED=$((MISSED+1)); return; }
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if old not in s:
    sys.exit(1)
p.write_text(s.replace(old, new, 1))
EOF
    local r; r=$(run)
    printf '  %-28s %s\n' "$1:" "$r"
    if [[ "$r" == "none | none | none" ]]; then MISSED=$((MISSED+1)); else FIRED=$((FIRED+1)); fi
}

echo "== mutations (each line: failing pytest ids | failing shell tags | failed systemd checks)"
mutate M1-knob-slope backend/fancore.py \
    's|(temp_c - t0) \* (p1 - p0) // (t1 - t0) + p0|(temp_c - t0) * (p1 - p0) // (t1 - t0 + 1) + p0|'
mutate M2-drop-bang-source backend/fancore.py \
    's|return f"!{fan_curve}" if c.knobs else f"{t}/{hw.temp_input}"|return f"{t}/{hw.temp_input}"|'
mutate M3-step-above-mintemp backend/fancore.py \
    's|k.append((self.mintemp - 1, self.minpwm))|k.append((self.mintemp + 1, self.minpwm))|'
mutate M4-drop-falling-check juno-fancontrol-apply \
    '/knob pwm must not fall/d'
# The KNOB_CPU guard this mutation used to target (apply_fancontrol's own
# clamp=0 for knob mode) is now unreachable dead code: regen_custom is the
# only caller that can ever see a non-empty KNOB_CPU, and it passes clamp=0
# explicitly below. Target the real guard instead: drop it and a plain custom
# curve's MAXPWM gets re-clamped to the cap on every restart (T2-ignore-cap,
# T-regen-preserve(b)).
mutate M5-regen-clamps-xfer fan-profile \
    's|apply_fancontrol "\$interval" "\$mintemp" "\$maxtemp" "\$minstart" "\$minstop" "\$minpwm" "\$maxpwm" 0|apply_fancontrol "$interval" "$mintemp" "$maxtemp" "$minstart" "$minstop" "$minpwm" "$maxpwm"|'
mutate M6-drop-insert-clamp app.py \
    's|k\[i\] = (t, max(lo, min(pwm, hi)))|k[i] = (t, pwm)|'
mutate M7-regen-loses-bang fan-profile \
    's|temps+=("$FANHW/$p=!$KNOB_HELPER")|temps+=("$FANHW/$p=$TEMPHW/temp1_input")|'
mutate M8-drop-knob-cap fan-profile \
    '/\[\[ -n "$KNOB_CPU" \]\] && cap_knobs/d'
mutate M9-drop-drag-clamp app.py \
    's|k\[i\] = (max(t_lo, min(t, t_hi)), max(p_lo, min(pwm, p_hi)))|k[i] = (t, pwm)|'
mutate M10-knob-validate-off backend/fancore.py \
    's|^            c._validate_knobs()|            pass  # MUTANT|'

mutate M11-drop-row-hiding app.py \
    's|self.form.setRowVisible(self.spin\[key\], not knobs)|pass|'
mutate M12-drop-hwmon-catch fancurve.py \
    's|    except HwmonNotFound as e:|    except ZeroDivisionError as e:|'
mutate M13-apply-hardcodes-devname juno-fancontrol-apply \
    's|echo "DEVNAME=$FANHW=$FAN_DEVNAME $TEMPHW=$TEMP_DEVNAME"|echo "DEVNAME=$FANHW=$FAN_DEVNAME $TEMPHW=coretemp"|'
mutate M14-regen-hardcodes-devname fan-profile \
    's|    echo "DEVNAME=$FANHW=$FAN_DEVNAME $TEMPHW=$TEMP_DEVNAME"|    echo "DEVNAME=$FANHW=V5xTNC_TND_TNE $TEMPHW=coretemp"|'
mutate M15-drop-occupied-nudge app.py \
    's|            t = free\[0\]|            return|'
mutate M16-argv-drops-average app.py \
    's|str(c.maxpwm), str(c.average),|str(c.maxpwm),|'

# --- theming: every colour has to come from the scheme, and follow it live ---
mutate M17-hardcoded-cap app.py \
    's|k\.negative|QColor("#c0392b")|g'
# Only the cap LINE reverted, its label left on the scheme. The label owns most
# of the negative pixels, so this is what a whole-image colour check misses.
mutate M17b-cap-line-only app.py \
    's|p.setPen(QPen(k.negative, 1.4, Qt.PenStyle.DashLine))|p.setPen(QPen(QColor("#c0392b"), 1.4, Qt.PenStyle.DashLine))|'
mutate M21-curve-not-scheme app.py \
    's|^        accent = k.focus$|        accent = pal.color(QPalette.ColorRole.Highlight)|'
mutate M22-hardcoded-marker app.py \
    's|^            live = k.positive$|            live = QColor("#27ae60")|'
mutate M19-wrong-colour-set backend/ktheme.py \
    's|^SET = "Colors:View"|SET = "Colors:Window"|'
mutate M20-ignore-scheme backend/ktheme.py \
    's|^    if not path:|    if True:|'

pymutate M18-no-stale-guard app.py \
    '        if act >= len(handles):
            act = -1        # its knob was removed before this repaint
' ''
pymutate M23-palette-all-groups app.py \
    '    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(161, 169, 177))
' ''
# The scheme read is memoized, so dropping the invalidation leaves a running
# window on the colours it started with for the whole session.
pymutate M24-retheme-keeps-cache app.py \
    '        ktheme.forget()
        self.canvas.update()' '        self.canvas.update()'
pymutate M25-error-colour-only app.py \
    '        self.result.setText(text if text.startswith(ERROR_PREFIX)
                            else f"{ERROR_PREFIX}{text}")' '        self.result.setText(text)'
# The hint carries an inline stylesheet, which the palette cannot override.
pymutate M26-tray-hint-not-retheme tray.py \
    '        sheet = f"color: {ktheme.colors(self.palette()).inactive.name()}"
        if sheet != self.hint.styleSheet():
            self.hint.setStyleSheet(sheet)' '        pass'

# --- the GPU fan ---
# The never-wake ordering lives in read_dgpu: query the power state first and
# only talk to the card when it answers "active".
pymutate M27-gpu-wakes-when-suspended backend/sysmon.py \
    '    if runtime != "active":
        return Dgpu(present=True, powered=False, state=f"{runtime} ({power_state})".strip())
' ''
# A broken nvidia-smi dropping to coretemp is what keeps the daemon out of
# restorefans forever; removing the fallback must break the fallback test.
pymutate M28-gpu-smi-failure-aborts backend/fancore.py \
    '    try:
        temp = read_sensors(discover(platform_dir), platform_dir).cpu_temp_c
    except HwmonNotFound:
        temp = None
    if temp is None:
        raise HwmonNotFound("nvidia-smi gave no temperature, coretemp also unreadable")
    return int(round(temp * 1000))' \
    '    raise HwmonNotFound("nvidia-smi gave no temperature")'
# The pwm2 knob read must come from the pwm2 line; fan1's line is a different
# curve on a hotter sensor.
mutate M29-gpu-curve-reads-pwm1 fancurve.py \
    's|pwm = CPU_PWM if args.fan == "cpu" else GPU_PWM|pwm = CPU_PWM|'
# The split source emit: pwm2 in knob mode belongs to the gpu helper.
mutate M30-apply-gpu-on-cpu-helper juno-fancontrol-apply \
    's|FCTEMPS+=("$FANHW/$pwm=!$GPU_CURVE")|FCTEMPS+=("$FANHW/$pwm=!$FAN_CURVE")|'
# regen caps both knob lines, or the GPU fan outshouts the calibrated cap.
mutate M31-regen-skips-gpu-cap fan-profile \
    '/\[\[ -z "$KNOB_GPU" \]\] || KNOB_GPU=$(cap_line "$KNOB_GPU" "$cap")/d'
# On a dGPU machine the CPU curve alone is a wrong curve for pwm2; the helper
# must say so, not write it.
mutate M32-apply-allows-half-split juno-fancontrol-apply \
    '/|| die "this machine has a dGPU: knob mode needs --gpu-knobs too, or pwm2 would follow the CPU temperature"/d'
# The renderer refusing to carry the GPU line would downgrade a dual config to
# the CPU curve on the next write.
pymutate M33-render-drops-gpu-knobs backend/fancore.py \
    '        if gpu_curve is not None:
            head.append(knobs_line(gpu_curve.knobs, GPU_PWM))
' ''

# --- tray probes ---
# A setting that is read but never written flips the panel only until the next
# start; the persistence test measures exactly that.
mutate M34-probe-toggle-not-saved tray.py \
    '/self.settings.setValue(f"probes\/{key}", on)/d'
# A set_probe that ignores the store reads as all-on everywhere.
mutate M35-probe-on-ignores-store tray.py \
    's|if self.settings.contains(skey):|if False:|'

# --- tray dashboard ---
# The scheme-painting test paints with colours nothing else uses; a hardcoded
# series colour leaves the scheme's neutral absent from the GPU chart.
pymutate M40-dgpu-colour-hardcoded tray.py \
    'self.panel.chart_gpu.add("dGPU", getattr(k, SERIES["dGPU"]), s.dgpu.util_pct)' \
    'self.panel.chart_gpu.add("dGPU", QColor("#3daee9"), s.dgpu.util_pct)'
# A degenerate gauge range must paint empty, not divide by zero.
pymutate M41-gauge-divides-a-zero-span tray.py \
    '        span = self.hi - self.lo
        if span <= 0:
            return 0.0
' '        span = self.hi - self.lo
'
# The indicator must never advertise the dGPU while the card sleeps; the
# assertion pairs the text with the never-called nvidia-smi log.
pymutate M42-indicator-claims-awake-dgpu tray.py \
    '        if d.powered and d.util_pct is not None:
            self.panel.set_indicator("dGPU (NVIDIA)", "neutral")' \
    '        if d.present:
            self.panel.set_indicator("dGPU (NVIDIA)", "neutral")'
# Two charts are the dashboard; one is half of it. The series/visibility tests
# name it.
pymutate M43-gpu-chart-dropped tray.py \
    'CHARTS: tuple[tuple[str, str], ...] = (
    ("chart-cpu", "CPU utilization chart"),
    ("chart-gpu", "GPU utilization chart"),
)' 'CHARTS: tuple[tuple[str, str], ...] = (
    ("chart-cpu", "CPU utilization chart"),
)'

# --- per-fan independence (SP2) ---
# regen collapsing pwm2's band onto pwm1's is the silent flattening T22 guards.
mutate M36-regen-collapses-pwm2 fan-profile \
    's|BAND_GPU="$gmintemp $gmaxtemp $gminstart $gminstop $gminpwm $gmaxpwm"|BAND_GPU=""|'
# A preset click that re-seeds the other fan wipes its knob curve again.
pymutate M37-preset-reseeds-other-fan app.py \
    '        # Only the selected fan gets the preset: seeding the other fan would
        # silently wipe a knob curve built on it.
        self.editor_from_curve(p, name)' \
    '        self.curves["gpu"] = replace(p, knobs=())
        self.editor_from_curve(p, name)'
# Emitting the shared fanout even when the bands differ silently writes a
# single-band config for a dual-band request.
pymutate M38-render-collapses-gpu-band backend/fancore.py \
    '        if gpu_band is not None and key not in ("AVERAGE",):
            lines.append(per_pwm(key, value, getattr(gpu_band, key.lower())))
        else:
            lines.append(per_pwm(key, value))' \
    '        lines.append(per_pwm(key, value))'
# Routing a custom label through the preset table is fan-calibrate's pre-fix
# behaviour; it exits 1 and leaves the cap written but the curve in place.
mutate M39-calibrate-custom-dies fan-calibrate \
    's|priv env NO_RESTART=1 fan-profile regen|priv fan-profile "$cur"|'

# --- restart hygiene (every fancontrol start blips 255 onto each pwm) ---
# Unit policy: the crash loop cap and on-failure restart live in the drop-in,
# guarded by the sweep's systemd lane (the container gate checks the same).
mutate M44-restart-always-back systemd/30-juno-fancontrol.conf \
    's|^Restart=on-failure$|Restart=always|'
mutate M45-startlimit-dropped systemd/30-juno-fancontrol.conf \
    '/^StartLimitBurst=3$/d'
mutate M57-startlimit-interval-dropped systemd/30-juno-fancontrol.conf \
    '/^StartLimitIntervalSec=180$/d'
mutate M63-private-devices-back systemd/30-juno-fancontrol.conf \
    '/^PrivateDevices=no$/d'
# An explicit apply or profile switch must reset the crash-loop start counter
# first, or StartLimitBurst=3 refuses it and leaves fancontrol down.
mutate M64-apply-no-reset-failed juno-fancontrol-apply \
    '/reset-failed fancontrol.service/d'
mutate M65-profile-no-reset-failed fan-profile \
    '/reset-failed fancontrol.service/d'
# Packaging: the no-op hook must ship, the old resume hook must not come back,
# and the diversion must be addable, removable and warned about.
mutate M46-noop-not-shipped debian/install \
    '/fancontrol-sleep-noop/d'
pymutate M47-resume-hook-back debian/install \
    'systemd/fancontrol-sleep-noop usr/share/juno-kde-fancontrol/' \
    'systemd/fancontrol-sleep-noop usr/share/juno-kde-fancontrol/
systemd/fancontrol-resume usr/lib/systemd/system-sleep/'
mutate M48-divert-dropped debian/postinst \
    's|dpkg-divert --package juno-kde-fancontrol --add --rename|true|'
mutate M49-undivert-dropped debian/prerm \
    '/dpkg-divert --package juno-kde-fancontrol --remove/d'
mutate M50-etc-dropin-warning-dropped debian/postinst \
    '/ovr=(\/etc/d'
# Restart-only-on-change: a broken timestamp exclusion restarts on every
# minute crossing; a dropped skip restarts on every re-apply; a dropped
# is-active leaves a down daemon down.
pymutate M51-stamp-not-excluded fan-profile \
    '    cmp -s <(sed '"'"'1s/ — .*//'"'"' "$1") <(sed '"'"'1s/ — .*//'"'"' "$2")' \
    '    cmp -s "$1" <(sed '"'"'1s/ — .*//'"'"' "$2")'
pymutate M52-helper-always-restarts juno-fancontrol-apply \
    '    if "$SYSTEMCTL" is-active --quiet fancontrol.service && cfg_eq "$TMP" "$FANCONFIG"; then' \
    '    if false && "$SYSTEMCTL" is-active --quiet fancontrol.service && cfg_eq "$TMP" "$FANCONFIG"; then'
pymutate M53-cli-always-restarts fan-profile \
    '    if systemctl is-active --quiet fancontrol.service && cfg_eq "$tmp" "$FANCONFIG"; then' \
    '    if false && systemctl is-active --quiet fancontrol.service && cfg_eq "$tmp" "$FANCONFIG"; then'
pymutate M54-calibrate-always-restarts fan-calibrate \
    "    pre=\$(sed '1s/ — .*//' \"\$FANCONFIG\" 2>/dev/null || true)" \
    '    pre=mutant-never-matches'
mutate M55-calibrate-never-restarts fan-calibrate \
    's|priv "$SYSTEMCTL" restart fancontrol.service|priv "$SYSTEMCTL" try-restart fancontrol.service|'
pymutate M56-helper-inactive-left-down juno-fancontrol-apply \
    '    if "$SYSTEMCTL" is-active --quiet fancontrol.service && cfg_eq "$TMP" "$FANCONFIG"; then' \
    '    if cfg_eq "$TMP" "$FANCONFIG"; then'
# Dropping the dot from the divert target puts the original hook back in
# systemd-sleep's scan: both hooks would run on every resume.
mutate M58-divert-target-visible-postinst debian/postinst \
    's|system-sleep/\.fancontrol\.juno-diverted|system-sleep/fancontrol.juno-diverted|'
mutate M59-divert-target-visible-prerm debian/prerm \
    's|system-sleep/\.fancontrol\.juno-diverted|system-sleep/fancontrol.juno-diverted|'
# Without the reinstall line a rewritten hook stays rewritten until the next
# upgrade: the no-op is only ever placed once.
mutate M60-noop-reinstall-dropped debian/postinst \
    '/install -m 755 "$SLP_NOOP" "$SLP"/d'
# The pre-deb fancontrol-resume hook must be reaped by both lanes; a dropped
# rm leaves the try-restart-on-resume hook (one 255 blip per resume) alive.
mutate M61-installsh-hook-rm-dropped install.sh \
    '/rm -f \/usr\/lib\/systemd\/system-sleep\/fancontrol-resume/d'
mutate M62-postinst-hook-rm-dropped debian/postinst \
    '/rm -f \/usr\/lib\/systemd\/system-sleep\/fancontrol-resume/d'
# Without the stale-copy fallback a half-state (entry gone, .juno-diverted
# still on disk, upstream hook live again) fails the re-divert permanently;
# the container gate's deb lane kills the same arm behaviourally.
pymutate M63-half-state-fallback-dropped debian/postinst \
    '            # Half-state: the entry is gone but a stale diverted copy still
            # sits at $SLP_DIV while the live path carries the upstream hook
            # again, so the --rename above failed (rc 2, "rename involves
            # overwriting") and rolled the entry back. Move the stale copy
            # aside under a fixed name and try the divert once more.
            if [ -e "$SLP_DIV" ]; then
                echo "juno-kde-fancontrol: WARNING: moving stale $SLP_DIV to $SLP_DIV.stale to recover the diversion" >&2
                mv "$SLP_DIV" "$SLP_DIV.stale"
                dpkg-divert --package juno-kde-fancontrol --add --rename --divert "$SLP_DIV" "$SLP" \
                    || echo "juno-kde-fancontrol: WARNING: could not divert $SLP; the upstream resume hook stays live" >&2
            else
                echo "juno-kde-fancontrol: WARNING: could not divert $SLP; the upstream resume hook stays live" >&2
            fi
' \
    '            echo "juno-kde-fancontrol: WARNING: could not divert $SLP; the upstream resume hook stays live" >&2
'

restore
echo "== after restore: $(run)"
echo "== fired=$FIRED missed=$MISSED"
[[ $MISSED -eq 0 ]]
