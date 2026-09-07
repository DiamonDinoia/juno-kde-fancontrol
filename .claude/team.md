# Team state — random 100% fan bursts: prove the origin, kill it

### Goal
The user sees fan bursts at 100% repeatedly with no load justification, latest
spotted just before this run. Prior adversarial review already falsified the
initial classification (D1/D5 of the retired "investigate system fan control"
run) and produced the competing mechanism: fancontrol's pwmenable() writes 255
to every pwm at every service start, and the service restarts a lot (resume
hook, Restart=always, fan-profile apply, plus one observed EOPNOTSUPP crash on
Sep 04 when pwm_enable flipped to EC-auto under the daemon). This run proves
the mechanism cleanly, ships the fix in juno-kde-fancontrol, and answers the
open mystery (who flips pwm_enable under the daemon).

### Deliverables
- [x] D1: causal proof that every fancontrol start write 255 to both fans
  AND the resume path fires it per suspend/resume. Evidence: journal count of
  `Enabling PWM on fans` lines vs suspend/resume counts; fancontrol source
  line refs; a sensor-level note of duration (how long 255 stays, i.e.
  between start and first control-loop write). Live-measured on the laptop
  using read-only probes and an artificial restart the user runs with sudo.
  Deliverable: table in the logbook.
- [x] D2: fix in juno-kde-fancontrol — stop blasting at start. Ideas to
  evaluate then implement: (a) resume path must not restart the service —
  reassert pwm*_enable in place when needed instead (clevofan's enable bits
  survive suspend; verified from the module source); (b) shaft the module
  write ordering so the daemon takes manual mode FIRST then reads curve
  (check pwmenable does that already; if not fixable in the package, change
  the surface that causes restarts: ensure no routine path restarts
  fancontrol); (c) regen itself may restart fancontrol — collapse to
  check-consistency only unless the config changed. check: tests +
  implementation; the container gate's helper suite gets restart-avoidance
  cases; full gates run as usual.
- [x] D3: answer "who flips pwm_enable": read /usr/src/clevofan-2.4
  (fan_auto semantics around its EOPNOTSUPP returns and the estimated_pwm=255
  path at tach=0), check whw never writes enable=2 in this system beyond
  fan-profile auto, classify the Sep 04 13:26:52 crash as resident silencing
  path or evidence of a foreign writer.
  evidence file in logbook with source line refs.
- [x] D4: regression gate extension — a sweep mutation must catch a resume
  reintroducing a service restart (container gate must assert `systemctl
  try-restart` is not in the resume hook and fan-profile does not blindly
  restart after regen; test deb lane asserts the same). Sweep + helper stays
  green with the new cases.

### Dispatch preferences
- Concurrency: parallel
- Experts: dispatched (team-expert)

### Pool
| Role | Specialty |
|------|-----------|
| evidence-journal | journal history, systemd timings, correlation |
| fan-stack-packaging | juno-kde-fancontrol packaging/bash/unit work + its gates |
| kernel-module | clevofan source, enable semantics, EOPNOTSUPP path |

### Split
| # | Subproblem | Role | Scope | Done condition | Depends on | Status |
|---|------------|------|-------|----------------|------------|--------|
| R1 | Prove blast->start causality: per-day counts of fancontrol 'Enabling PWM on fans' vs suspend/resume events vs user-visible blasts; static duration of the 255 window (start protocol). logbook only | evidence-journal | read-only journalctl + /usr/sbin/fancontrol + this repo's READMEs | D1: table in logbook, every number citable with its command | none | done — logbook 967e99d (reviewer fixes folded) |
| R2c... | | | | | | |
| R2 | Kill routine restarts and bound the exception paths. juno-resume hook: no try-restart (clevofan's PM notifier already re-asserts manual duty at PM_POST_SUSPEND — verified :420-437). Upstream hook: neutralize via dpkg-divert to a dot-prefixed no-op in postinst/prerm. Apply/regen/calibrate paths restart only when the effective curve content changed (compare excluding the minute-grain timestamp header; FP_NOW/JFC_NOW seams). Unit policy: Restart=always -> Restart=on-failure + StartLimitBurst 3 per 180 s. postinst warns on stale /etc drop-ins and on an un-diverted upstream hook. Docs tell the truth about boot blast = firmware + first start ritual. Gates: sweep's FILES + run() gain systemd-checks lane; helper T23 block (two applies same minute & differing FP_NOW both prove no restart when unchanged; changed config still restarts); deb lane asserts hook content and diversion; README/postinst prose updates | fan-stack-packaging | juno-kde-fancontrol repo, all systemd/*, fan-profile, fan-calibrate, juno-fancontrol-apply, debian/postinst, debian/control (note clevofan runtime dep), tests/* suite plumbing, README.md | D2+D4: gates green incl. sweep fired=all missed=0; every new check covered by a sweeping mutation | none | done — merged into main 6b6785f; reviewer round: BLOCKER divert-target visible to systemd-sleep fixed (dot-prefix), sweep 61 fired/all missed=0 |
| R3 | clevofan source verdicts + write-side attribution with line refs on src/clevofan.c (dkms builds src/; four sibling copies exist). Correct round-0 premise error: tach-0 -> pwm READS 0 under manual; reads the EC register's 255 under auto; enable=2 makes a 100 ms stop, not a blast. Attribute Sep 04 13:26 enable flip: enumerate every in-repo writer (fan-profile auto path, calibrate restore path, pwmdisable self-restore) and inspect whether a leftover juno clevofan-auto*.service exists and ever ran via pkexec/journal; name the residual uncertainty if unprovable | kernel-module | /usr/src/clevon-2.4/src/clevofan.c read, journalctl read, logbook write ONLY | D3 with per-claim line refs | none | done — logbook d2e68e6 |

### Rounds
| Round | Event | Accepted findings | Rejected findings |
|-------|-------|-------------------|-------------------|
| 0-v2 | two critics, kernel-module + packaging lenses | upstream sleep hook is a second restart per resume (neutralize via diversion); Restart=always crash loop amplifies; skip-restart needs timestamp seam; sweep FILES/run() has no systemd lane (plumbing is part of R2); /etc drop-ins entirely shadow the deb's unit units — postinst must warn; regen timestamps defeat byte-compare; PM notifier already re-asserts manual duty — resume needs no restart; module readback inverts the round-0 premise (tach0 -> reads 0 in manual) | residual sub-second 255 on intentional starts+apply — accepted, documented instead of diverting /usr/sbin/fancontrol |

| 1 | R1 evidence + R2 packaging executed; R2 reviewed twice (6 findings, 1 blocker: divert target visible to systemd-sleep — fixed dot-prefixed; 2 mediums: uncovered sweep checks — mutations M57-M60 added) then ACCEPT; gates: container 253->257 PASS, sweep 57->61 fired/all missed=0, helper 125->135, deb 81->85; merged 9484139 -> integration -> main 6b6785f | none |

| 0-pre | prior run REJECTED my classification; counterevidence accepted: pwmenable 255-per-start; resume+Restart=always+apply restarts clustered; EOPNOTSUPP crash at Sep 04 13:26:52 on enable-flip (clevofan returns EOPNOTSUPP at ...370/373 when fan_auto state blocks); TFN unbound + 103C trips irrelevant; thermald clean | cdev/thermald/clevo-WMI clean claims (2,4 of that run) | my claims that fancontrol can't emit 255, EC panic preference |

### Verification
The container gate `bash tests/run-container.sh` on integration, sweep
`bash tests/mutate.sh` 100% fired, publish chain CI + e2e. Full fan watches
run without sudo.

### Notes
- Sudo never used by agents; all live writes happen via pkexec or user-typed.
- Prior team session (dash/curves/autostart) closed at start of this run.
- Also recorded: the EOPNOTSUPP at Sep 04 13:26:52 mentions write to
  pwm_fan_ctrl — check order of places module disallows writes (clevofan
  enable semantics: fan_auto[channel]==1 means EC free-run; we then can't pwm).
- A legit path for 100% on degrees we didn't yet weigh: the KNOB_XFER config
  writes MAXPWM=255 for knob curves; loop-law `tval>=maxt => pwmval=maxpwm`
  puts 255 whenever... no: knob mode's fan_curve helper returns pwm*1000 and
  the xfer writes that VALUE directly (fancontrol writes pwmval which IS the
  maxpwm-scale number, follow upstream's interpolation). Duty-clamp happens
  through --knobs+150 cap at apply, so knob config cannot reach 255 unless
  the curve itself demands it. R2's tests pin this claim.

### Closeout (2026-09-07)
All deliverables green. R1 (journal) + R3 (module) deliverables are logbook
reports (memory 967e99d + d2e68e6); R2 is the only code branch, merged as
9484139 -> integration -> main 6b6785f. Reviewer round 1 found 6 findings
(1 blocker: the dpkg-divert target was a VISIBLE name systemd-sleep still
executes — both hooks would run; fixed dot-prefixed, mirroring mutations
M58/M59; 2 mediums: uncovered sweep checks -> M57/M60; prerm made
warn-and-continue; `wrote` print moved after validation). Round 2 ACCEPT.
Gates at merge: container 257 PASS / 0 FAIL, sweep 61 fired all missed=0,
helper 135, pytest 210, deb lane 85.
Publish: juno CI -> builds asset 0.6.3+diamon1. Drivers repo main merged
(b51a186) + CI sibling-checkout fix e6d9086 -> diamon6 debs. apt repo:
packages.toml gained clevo-keyboard-dkms passthrough (branch
publish/juno-0.6.3 + manual nightly dispatch on it, because apt local main
carries another session's 12 unpushed commits).
Evidence numbers (24 starts in window): boot 20 % / resume 4 % / crash 8 % /
GUI apply 32 % / dev-script 36 %; 255 window min 12.3 ms max 40.1 ms per
start; residual after this fix: boot-time-only. Sep-04 13:26 crash = user's
own sudo modprobe -r (attribution in logbook).
Dead tach on fan2 (GPU fan) remains unexplained — reads 0 while fan spins;
no evidence it causes bursts (manual-mode readback returns the cached duty).
