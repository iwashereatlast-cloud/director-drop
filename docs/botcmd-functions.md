# botcmd function inventory & status audit (PR #34)

What every `botcmd` area does, and how we know it works. Status legend:

- **LIVE** — seen working on serverpc in real session logs this project
- **HARNESS** — covered by the shell/python harnesses + smoke audit, not (yet) exercised live
- **REMOVED** — taken out, with the reason

## Menus / shell

| function | what it does | status |
|---|---|---|
| main menu + `menu_lock` | one interactive menu at a time (flock `~/.botcmd.lock`, kill-safe); one-shots never locked | HARNESS (stderr-preservation + option-7 launch proven in test_shell15/smoke; scroll fix #31) |
| `botcmd launch` (menu 7) | ordered boot ritual (worldserver..timer) | HARNESS (FULL LAUNCH in smoke audit) |
| `botcmd status` | one-screen dashboard: units + census | HARNESS |

## Leo shell (`leo`)

| function | what it does | status |
|---|---|---|
| `botcmd leo on/off/status` | systemctl user unit control | LIVE (leo restarted via unit this session) |
| `botcmd leo chat [N\|-f]` | read/follow `/tmp/leo.log` | LIVE (used to read whisper replies: `botcmd leo chat 60`) |

## Director daemon (`director`)

| function | what it does | status |
|---|---|---|
| `botcmd director restart/status/log/watch` | systemctl + daemon.log | HARNESS (restarts done via `systemctl --user` directly) |
| `botcmd director raid [toon]` | Leo's group/raid from read-only DB | **LIVE** (serverpc logs) |
| `botcmd director deploy` | git pull + py_compile + restart | HARNESS |
| `botcmd director commands` | parse live WHITELIST/FORBIDDEN from director.py | HARNESS |

## Raid/group (`raid`)

| function | what it does | status |
|---|---|---|
| `botcmd raid show [toon]` | group from DB (same path as `director raid`) | LIVE |
| `botcmd raid clear [toon]` | `/w <m> leave` every non-G1 member | HARNESS (#28); largely superseded by the automatic post-ritual clear (#33) |

## Roster (`roster`, `go`)

| function | what it does | status |
|---|---|---|
| `botcmd roster census` | live who's-online | HARNESS |
| `botcmd roster ritual` | queue addaccount/bot add; since #33 also waits for logins and clears Leo's auto-formed group | **LIVE** (serverpc logs) |
| `botcmd roster add/remove` | one bot up/down | LIVE via ritual/down flow |
| `botcmd roster down <toon\|all>` | `bot remove`, SOAP `.kick` if still online | **LIVE** (serverpc logs show the kick flow) |
| `botcmd roster fix` | down all + ritual + verify | **LIVE** (run on serverpc) |
| `botcmd roster link ...` | trusted-account linking (I_WAS_HERE5TH) | HARNESS (#30); needed live for Golbez's account |
| `botcmd go <toon> <zone>` | `/w <toon> go travel <zone>` | HARNESS |

## States (`states`)

| function | what it does | status |
|---|---|---|
| `botcmd states show/edit/presets/zones` | conf table, editor, help, zone table | HARNESS (menus) |
| `botcmd states bots` | paginated picker -> set conf state | HARNESS |
| `botcmd states setall [preset]` | conf for every bot + apply + verify | HARNESS |
| `botcmd states apply [bot\|all]` | **THE set-state path**: since #34 read-modify-write — probe `nc ?`/`co ?`, whisper only the diff, full push on no reply, then verify | LIVE for the push (`queued: /w ...` in serverpc logs); probe+diff shape is #34 |
| `botcmd states tune [bot\|all]` | was a separate menu-10 option | **REMOVED as a separate path (#34)** — now an alias of `apply` |
| `botcmd states verify [bot\|all] [force]` | read-back: verdict = `nc ?` strategy membership vs conf | **LIVE** (VERDICT tables in serverpc logs) |
| `botcmd states probe [bot\|all] [force] [cmd]` | raw whisper passthrough, prints ALL reply lines | **LIVE** (probe output in serverpc logs) |
| `botcmd states quests [bot\|all]` | read-only quest log from `character_queststatus` | HARNESS (#30) |
| `rpg status do quest` inside the set-state presets | used to be appended to the `quest` preset | **REMOVED (#34)** — it only lands when a quest with a POI is already in the log, otherwise the bot just whispers back `No quest available; use 'do quest <id>'` (NewRpgAction.cpp:183-215): a dead round-trip per bot that stalls apply/verify reply collection. Still available as a raw conf line / manual probe; `rpg do quest <id>` forces by id |

## Watchdog (`watchdog`)

| function | what it does | status |
|---|---|---|
| `botcmd watchdog status/journal/conf/procs` | inspect the presence-policy timer | HARNESS |
| leo-watchdog.timer itself | starts/stops leo, zombie-kill, auto-ritual, since #33 post-ritual group clear (`AUTORITUAL_CLEAR`) | **LIVE** (it is what brings Leo + bots up) |

## System (`system`)

| function | what it does | status |
|---|---|---|
| `botcmd system worldserver` | ac-worldserver check | HARNESS |
| `botcmd system arm/soap/cmd` | arm SOAP gate, live test, run console cmd | LIVE for arm (SOAP `.kick` used by `roster down`) |
| `botcmd system grants` | RO identity SHOW GRANTS | HARNESS |

## Master (`master`)

| function | what it does | status |
|---|---|---|
| `botcmd master show/set/menu` | rotate which account is HUMAN | HARNESS |

## Removed / replaced over time (so nobody re-adds them)

- rpg-status-based verify verdict → replaced by `nc ?` strategy membership (#29): `rpg status do quest` needs a quest with a POI in the log, so status-based verify mismatched bots with empty logs
- `characters.online` gate on apply → removed (#25): worldserver boot resets all flags (Main.cpp:520); whispers to offline toons are harmless no-ops; verify read-back is the source of truth
- scrolling menus → fixed by `cls()` redraw (#31); `BOTCMD_NO_REFRESH=1` forces scroll
- `menu_lock` stderr swallow → fixed (#31)
- mod auto-join into Leo's raid on every bot login → no conf key disables it (PlayerbotMgr.cpp:591-619, PlayerbotOperations.h:37-90); undone by post-ritual clears (#33)
- separate "tune" menu option → folded into `states apply` (#34)
- `rpg status do quest` in the `quest` preset → removed (#34), see above
