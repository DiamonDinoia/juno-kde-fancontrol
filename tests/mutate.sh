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
FILES=(backend/fancore.py backend/ktheme.py backend/sysmon.py juno-fancontrol-apply fan-profile app.py fancurve.py tray.py)
save()    { for f in "${FILES[@]}"; do mkdir -p "$BK/$(dirname "$f")"; cp "$f" "$BK/$f"; done; }
restore() { find . -name __pycache__ -type d -prune -exec rm -rf {} + ; for f in "${FILES[@]}"; do cp "$BK/$f" "$f"; done; }
trap 'restore; rm -rf "$BK" "$OUT"; echo "[trap] tree restored"' EXIT

LIMIT=300
FIRED=0
MISSED=0

run() {  # -> "<failed pytest ids> | <failed shell tags>", or HUNG
    local py sh rc
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

    echo "${py:-none} | ${sh:-none}"
}

base=$(run)
echo "== baseline: $base"
if [[ "$base" != "none | none" ]]; then
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
    if [[ "$r" == "none | none" ]]; then MISSED=$((MISSED+1)); else FIRED=$((FIRED+1)); fi
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
    if [[ "$r" == "none | none" ]]; then MISSED=$((MISSED+1)); else FIRED=$((FIRED+1)); fi
}

echo "== mutations (each line: failing pytest ids | failing shell tags)"
mutate M1-knob-slope backend/fancore.py \
    's|(temp_c - t0) \* (p1 - p0) // (t1 - t0) + p0|(temp_c - t0) * (p1 - p0) // (t1 - t0 + 1) + p0|'
mutate M2-drop-bang-source backend/fancore.py \
    's|return f"!{fan_curve}" if c.knobs else f"{t}/{hw.temp_input}"|return f"{t}/{hw.temp_input}"|'
mutate M3-step-above-mintemp backend/fancore.py \
    's|k.append((self.mintemp - 1, self.minpwm))|k.append((self.mintemp + 1, self.minpwm))|'
mutate M4-drop-falling-check juno-fancontrol-apply \
    '/knob pwm must not fall/d'
mutate M5-regen-clamps-xfer fan-profile \
    '/\[\[ -n "$KNOB_CPU" \]\] && clamp=0/d'
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
    's|return self.settings.value(f"probes/{key}", True, type=bool)|return True|'

restore
echo "== after restore: $(run)"
echo "== fired=$FIRED missed=$MISSED"
[[ $MISSED -eq 0 ]]
