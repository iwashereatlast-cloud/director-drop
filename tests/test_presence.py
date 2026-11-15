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

# level patrol (owner 2026-11-13: 'check every few minutes if a toon is
# offline, bring it online and tell it to level'): the presence thread keeps
# the fleet online and grinding - two-phase, evidence-based.
print("== level patrol: fleet stays online and leveling ==")
sent = []
real_census, real_execute, real_lease = director.census, director.execute, director._lease_kind
WORLD = {"Ayla": True, "Golbez": False, "Marle": False, "Leo": True,
         "Cecil": False}          # Cecil = hand-play, Leo = the master

def fake_census(online_only=True):
    return {n: {"account": "i_was_cmd" if n == "Leo" else "bots",
                "online": on}
            for n, on in WORLD.items() if on or not online_only}

def fake_execute(orders, bots, pace, dry, master=None):
    for o in orders:
        sent.append((o["bot"], o["cmd"]))
        if o["cmd"] == "bot add":
            WORLD[o["bot"]] = True    # the unit logs in
    return orders

director.census = fake_census
director.execute = fake_execute
director._lease_kind = lambda bot: "farm" if bot == "Marle" else None
st = {"last_patrol": 0}
director._patrol(st)
check("patrol: an offline unit gets bot add (not the grind arm yet)",
      (("Golbez", "bot add") in sent
       and ("Golbez", "nc +new rpg,+grind,-follow") not in sent), True)
check("patrol: a leased unit is left to its mission",
      not any(b == "Marle" for b, c in sent), True)
check("patrol: hand-play toons are never touched",
      not any(b == "Cecil" for b, c in sent), True)
check("patrol: Leo himself is never touched",
      not any(b == "Leo" for b, c in sent), True)
check("patrol: the add is remembered for the arm phase",
      st["pending_arms"] == {"Golbez"}, True)
st["last_patrol"] = 0                # force the next round
director._patrol(st)
check("patrol: once census confirms online, the grind arm goes out",
      ("Golbez", "nc +new rpg,+grind,-follow") in sent, True)
check("patrol: the pending arm is cleared after sending",
      st["pending_arms"] == set(), True)
sent2 = list(sent)
st["last_patrol"] = 0
director._patrol(st)
check("patrol: a steady online fleet gets nothing",
      sent == sent2, True)
director.census, director.execute, director._lease_kind = real_census, real_execute, real_lease

print("presence heartbeat tests: %s" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
