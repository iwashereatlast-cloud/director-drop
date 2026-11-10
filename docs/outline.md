# Outline & handoff — goals, functions, proof protocol

Pinned by PR #43. Purpose: **this repo is the durable context.** Chat threads
are ephemeral and the sandbox `/tmp` was wiped twice (2026-11-07, 2026-11-08) —
only `/workspace` and git survive, so anything that matters lives here. A new
session ("Director Thread" of the LLM controller) should read this file +
`docs/assumptions.md` + `leo/README.md` first, then `git log --oneline -15`.

## Goal

On the private era server (AzerothCore + mod-playerbots @ `405f8119`, AC @
`e7e2e288e`), command a roster of guild bots through one GM toon (**Leo**, shell
account `shellacct`) — by in-game chat (LLM director, `director.py`) or by shell
(`botcmd`) — and nothing unsafe can happen by accident.

### Invariants (rails)

- Bot commands are **whitelist-only** (`WHITELIST` in director.py);
  `FORBIDDEN` (autogear, maintenance, equip upgrade, reset botai, destroy, drop,
  sell, buy, repair, bank, hire, teleport, taxi, debug, cdebug, cheat) is blocked
  **structurally** in `validate()`, before whitelist/confirm.
- **G1 hand-play five** (Cecil, Aeris/Aerith, Rydia, Vivi, Shadow) never summoned,
  never commanded (`BOTCMD_HANDPLAY` / `HANDPLAY`).
- DB access = read-only identity (`~/.my.cnf-director`, SELECT grants only).
- Game writes go through FIFO `/tmp/leo.in` with a 3s timeout; daemon defaults
  to dry-run + `DIRECTOR_CONFIRM=1` (plans held until the master whispers `go`).
- One interactive `botcmd` menu at a time (flock `~/.botcmd.lock`).
- Bot chatter never reaches the LLM (no LLM↔bot loop); only bot **error**
  whispers surface as context (#41: `Recent bot error whispers`).

## Layers

| # | layer | where | role |
|---|---|---|---|
| 1 | worldserver + mod-playerbots | external | bots, strategies, master — not ours |
| 2 | Leo headless shell | `leo/leo.service`: `tail -f /tmp/leo.in \| shell.py >> /tmp/leo.log` | FIFO→game bridge |
| 3 | LLM director | `director.py` (`leo-director.service`) | leo.log → LLM → JSON orders → validate → FIFO; DB facts; `repl()`/`run_file()` offline |
| 4 | botcmd CLI | `bin/botcmd` + `lib/botcmd/*.sh` | remembered hand commands (systemctl / RO mysql / FIFO printf / git) |
| 5 | watchdog | `leo/leo-watchdog.{sh,timer}` | presence policy |

## Functions (prove ONE at a time, in this order)

Green = the observable named per row, produced by the commands on serverpc.

| # | function | commands | green looks like |
|---|---|---|---|
| A | Leo shell alive | `botcmd leo status`, `leo chat` | unit active, FIFO exists, a FIFO line appears in-game and as `[Name] [whisper]: ...` in leo.log |
| B | one bot up under Leo | `roster census`, `roster add <toon>`, `states probe <toon> "nc ?"`, `states verify <toon>` | `characters.online` flips (async), probe prints a `Strategies:` reply, verify row not NO REPLY |
| C | read a bot's state | `states probe`, `states verify`, `states quests` | reply lines printed / verdict row / quest rows |
| D | set a bot's state | `states show/edit/presets`, `states apply`, `states setall` | verify flips MISMATCH → `OK (nc: …)` |
| E | repair a silent bot | `states verify <toon> remaster` / `verify all force remaster` | previously-NO REPLY bot answers after recall |
| F | move a bot | `botcmd go <toon> <zone words>` | DB map/zone changes |
| G | group/raid control | `director raid`, `raid show/clear` | group_member rows change; `leave` keeps AI (bot still answers) |
| H | LLM director | whisper Leo in-game | ack whisper → order lines in leo.log/audit.log → bot acts → reply visible |
| I | ops | `launch`, `director restart/log/deploy/commands`, `watchdog`, `system`, `master`, `status`, `doctor` | units active; deploy pulls + restarts; doctor bundle read-only |

## Proof protocol (house rule since 2026-11-07)

1. For the chosen function write down: **command → expected observable → where
   the evidence lands** (leo.log / audit.log / DB / whisper reply).
2. Run it on serverpc, paste the output into the thread.
3. Green → next function. Red → smallest possible fix → re-proof the SAME
   function. Nothing else ships meanwhile.
4. Every behavioral assumption has a row in `docs/assumptions.md` with the exact
   confirming command; fill its result column from the run. A FAIL + pasted
   evidence is the only ticket for changing bot-behavior code.
5. No new layers/tools. `botcmd doctor` (PR #42) is read-only and optional —
   use it as the evidence bundle if wanted, build nothing on it.

## First proof (agreed starting point): function B with Ayla

```
botcmd leo status                       # A: unit active, FIFO present
botcmd roster census                    # DB works, toons listed
botcmd roster add Ayla                  # she already answered a whisper in a screenshot, so start with her
sleep 35                                # login is async (assumptions A3/A4)
botcmd states probe Ayla "nc ?"        # GREEN = "Strategies: ..." reply printed
botcmd states verify Ayla               # GREEN = one-row table, verdict not NO REPLY
# if silent instead: botcmd states verify Ayla remaster   then re-probe
```

## Handoff notes / open items

- serverpc bring-up to current code:
  `cd ~/downloads/director-drop && git fetch origin && git reset --hard origin/main`
  `systemctl --user restart leo-director leo-watchdog.timer`
- env: `~/.director.env` — recommend `DIRECTOR_KEEP_ALIVE=-1` (keep model warm),
  and `DIRECTOR_DEBUG=1` while proving; then
  `systemctl --user daemon-reload && systemctl --user restart leo-director`.
  (`leo/leo-director.service.d/override.conf` loads `EnvironmentFile=%h/.director.env`.)
- Last known live state (pre-#43): after the pre-#38 remove/add race, 12 bots were
  DB-offline and 3 online-silent; only Golbez had master=Leo. Repair sequence on
  current code: `botcmd states verify all force remaster` → `botcmd states apply all`
  → `botcmd states verify` (expect all OK) → `botcmd raid clear` if a group formed.
- Open: Aeri*/Aerith spelling unverified —
  `SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%';`
- Open: link ACCT_BOT5 — on that account `.playerbots account setKey <KEY>`,
  then `botcmd roster link ACCT_BOT5 <KEY>`; check `botcmd roster link check ACCT_BOT5`.
- Confirmed: `shellacct` gmlevel=3, RealmID=-1 (= all realms, NOT a bug) — assumptions A13.
- Known gap (evidence-gated candidate fix): presets toggle only what they name;
  default strategies (pvp, duel, pull, force, tank assist — seen in a screenshot of
  Ayla's `nc ?` reply) stay ON. Strip them in presets/conf only after
  `states verify` MISMATCH "still on […]" shows it live.
- Nov 5 rebuild audit (no code changes needed): mod-playerbots `e88cc40..405f8119`,
  AC `3f2969d..e7e2e288e` — no interface we depend on changed; new
  `ApplyInstanceStrategies` on login only strips instance/raid strategies in
  instances. Details in PR audit notes / assumptions A16.
- PRs #36–#43 merged to `main` by push (no CI monitoring was ever enabled).
- Tests: `tests/` holds the pinned harnesses (`tests/README.md`). The older
  /tmp-only harnesses (test_shell..23, test_states.py, test_raid.py,
  audit_smoke.sh) were lost in the 2026-11-07 sandbox wipe — regenerate from
  PR descriptions if needed. Run pinned tests: `for t in tests/test_shell*.sh; do bash "$t"; done`
