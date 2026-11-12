#!/usr/bin/env python3
"""Presence-heartbeat tests (single-brain rule, LLM-decided design).

The presence thread is a HAND: it gathers facts and detects when the LLM
needs a turn. It never acts. These tests drive _hb_condition (when does the
LLM get asked) and _hb_exec_service (the only executor, strict allowlist)
with mocked facts - no worldserver, no DB, no LLM.
Run: python3 tests/test_presence.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import director  # noqa: E402  (module import runs no DB calls)

CFG = director._wd_conf()
FAILS = []


def check(name, got, want):
    if got == want:
        print("PASS  %s" % name)
    else:
        print("FAIL  %s: got %r want %r" % (name, got, want))
        FAILS.append(name)


def fresh():
    return {"prev_sig": None, "off_streak": 0}


def cond(state, leo, wanted, svc, errors=0):
    return director._hb_condition((leo, 0, wanted, svc, errors), state)


# 1. first tick: baseline only, never asks
s = fresh()
check("first tick: no heartbeat",
      cond(s, leo=0, wanted=1, svc="inactive"), ("", False))

# 2. the reboot case: wanted-online but service down -> standing alarm -> ask
check("reboot: wanted + inactive -> standing alarm asks the LLM",
      cond(s, leo=0, wanted=1, svc="inactive")[0] != "", True)

# 3. steady healthy state: never asks
s = fresh()
cond(s, leo=1, wanted=1, svc="active")
check("healthy steady: no heartbeat", cond(s, leo=1, wanted=1, svc="active"), ("", False))

# 4. state change (Leo logs in): asks IMMEDIATELY (changed=True)
check("login: state change asks immediately",
      cond(s, leo=1, wanted=1, svc="active"), ("", False))  # unchanged sig -> no
s2 = fresh()
cond(s2, leo=0, wanted=1, svc="active")
c, changed = cond(s2, leo=1, wanted=1, svc="active")
check("login: off->on is a state change", (c != "", changed), (True, True))

# 5. zombie candidate: service active + DB offline, UNCHANGED sig twice ->
# asks (the off->on transition itself already fired as a state change)
s3 = fresh()
cond(s3, leo=0, wanted=1, svc="active")   # this turn IS the state change
check("zombie: first unchanged offline read -> no ask yet",
      cond(s3, leo=0, wanted=1, svc="active"), ("", False))
c, changed = cond(s3, leo=0, wanted=1, svc="active")
check("zombie: second unchanged offline read -> asks", (c != "", changed), (True, False))

# 6. off-hours steady: no asks
s4 = fresh()
cond(s4, leo=0, wanted=0, svc="inactive")
check("off-hours steady: no heartbeat", cond(s4, leo=0, wanted=0, svc="inactive"), ("", False))

# 7. NEW bot errors fire an IMMEDIATE heartbeat (state change) - the LLM
# decides what to do; the thread only reports the fact
s5 = fresh()
cond(s5, leo=1, wanted=1, svc="active")   # baseline
check("no errors -> no heartbeat", cond(s5, leo=1, wanted=1, svc="active"), ("", False))
c, changed = cond(s5, leo=1, wanted=1, svc="active", errors=2)
check("new bot errors -> immediate heartbeat", (c != "", changed), (True, True))
check("same errors again (none new) -> no heartbeat",
      cond(s5, leo=1, wanted=1, svc="active"), ("", False))
c, changed = cond(s5, leo=1, wanted=1, svc="active", errors=1)
check("another new error -> immediate heartbeat again", (c != "", changed), (True, True))
director.BOTFB.append("Ayla: Unknown spell")
msg = director._hb_msg((1, 0, 1, "active", 2), "2 new bot error whisper(s) surfaced", CFG)
check("heartbeat message carries the error whispers",
      "Ayla: Unknown spell" in msg, True)

# 8. service executor: strict allowlist, nothing else
ran = []
real_run = subprocess.run


def fake_run(cmd, *a, **k):
    ran.append(cmd)
    return real_run(["true"], capture_output=True)


subprocess.run = fake_run
director.AUDIT = os.devnull
check("service order: leo start executes",
      "executed" in director._hb_exec_service({"service": "leo", "cmd": "start"}),
      True)
check("service order: ran systemctl --user start leo", ran[-1],
      ["systemctl", "--user", "start", "leo"])
check("service order: bad cmd refused",
      "SKIP" in director._hb_exec_service({"service": "leo", "cmd": "rm -rf"}),
      True)
check("service order: bad service refused",
      "SKIP" in director._hb_exec_service({"service": "worldserver", "cmd": "stop"}),
      True)

# loner policy (owner 2026-11-12: "Leo keeps forgetting to kick bots out of
# his party"): every presence tick enforces LEO SOLOS through the validated
# group-remove rail - never model discretion.
print("== loner policy: Leo solos ==")
kicks = []
real_validate, real_census = director.validate, director.census
real_members, real_send = director.group_members, director.send_order
director.AUDIT = os.devnull

def fake_validate(o, bots):
    if o["bot"] == "Leo":
        return "SKIP Leo: never removed from his own group"
    return None

director.validate = fake_validate
director.census = lambda online_only=True: {"Ayla": None}
director.send_order = lambda who, cmd: kicks.append((who, cmd)) or {"line": "/w %s leave" % who}
director.group_members = lambda name, _m=["Leo", "Ayla"]: list(_m)
out = director._enforce_loner()
check("loner: members get the validated group remove (rewritten to leave)",
      (out, kicks), (["/w Ayla leave"], [("Ayla", "group remove")]))
director.group_members = lambda name: []
check("loner: empty group -> no kicks", director._enforce_loner(), [])
director.group_members = lambda name: ["Leo"]
check("loner: Leo himself is never kicked", director._enforce_loner(), [])
director.validate, director.census = real_validate, real_census
director.group_members, director.send_order = real_members, real_send

print("presence heartbeat tests: %s" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
