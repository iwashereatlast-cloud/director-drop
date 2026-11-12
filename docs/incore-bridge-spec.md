# Spec — director-bridge: the in-core module

**Status: DRAFT for owner review. Nothing here is built yet.** This document
fixes the design BEFORE any C++ exists, per the house rule that a guessed fix
is worse than no fix. Every engine claim below is source-pinned at
mod-playerbots @ `405f8119` / AC @ `e7e2e288e` or carries an A-row in
`docs/assumptions.md`.

## 0. Why this module exists

The whisper path works (travel, farm, walk, train, mail, level_in_zone are
live), but every function lives behind the rpg/nc engine's own gates, and the
engine decides what our orders mean. Three concrete walls, all pinned:

- `TaxiAction` returns early in the `HasGameClientMaster()` follower branch —
  `taxi ?`/`taxi <n>` are silent no-ops for Leo-mastered bots (A35).
- `'go travel'` is dead upstream (orphaned loader); the rpg engine's only
  chosen-destination-free flight is RANDOM via `rpg status travel flight`.
- `'trainer learn'` needs the MASTER's selected unit (A45, PR #132:
  `TrainerAction::GetTarget` returns the master's selected unit for
  mastered bots), which a headless Leo cannot reliably provide;
  `RpgTrainAction` has no master gate but auto-learns only affordable
  spells at whatever the bot happens to face (A45).

The module does NOT replace the whisper pipeline. It adds a second, typed,
evidence-returning path for the functions the engine gates off — with the
gates deliberate rather than accidental.

**Declined alternative (recorded so it is not re-litigated):**
`mod-llm-playerbots` (bigr00 fork) — a fork loses upstream currency, has 0
stars, and does not fix the engine gates above. OWN module dir, NOT a fork.
Both repos were reviewed as prior art on 2026-11-12 — see §8.

## 1. Shape (owner-settled, non-negotiable)

- Own module directory (`modules/mod-director-bridge/`), loaded by the stock
  AzerothCore module loader — ONE loader hook, no core file edits, no
  mod-playerbots edits. Version-skew risk is confined to the module's own
  `#include` surface (see §6.9).
- AF_UNIX socket (default `/tmp/director-bridge.sock`, mode 0660, owner the
  worldserver user) — NO TCP listener, ever. The 8888 lesson (A19: no-auth
  0.0.0.0 bind) is standing policy: no unauthenticated listeners on this
  stack.
- One JSON request per line → one JSON envelope per line, same discipline as
  db-search/chat-tunnel: `{interface: "director-bridge/1", ok, verb, bot,
  evidence, error}`.
- FIXED verb catalog — a request with an unknown verb answers
  `error: unknown_verb` and the catalog listing. Free-form command strings
  never reach the module (the LLM-boundary fuzz lesson, A39/A40, applies on
  this side of the socket too).
- World-thread-only execution: the socket reader thread NEVER touches a
  World/Map/Player object. It validates, enqueues a task, and the task runs
  on the world thread (AzerothCore's task queue). Replies are written back
  from the task's completion, not from the reader. (Working precedent:
  mod-llm-playerbots' `LlmToolbox.h` states this exact contract for its
  tool calls — provider thread returns calls, world thread executes them
  against a live `Player*`, everything in the toolbox file is world-thread
  only, and an unknown tool/bad argument answers a JSON error field
  instead of throwing.)
- Full `isUseful`/`isPossible` chains: every verb reuses the engine's own
  gate logic (or reimplements the same checks) — the module may ADD gates,
  never remove one. A verb that the engine would refuse answers with the
  engine's reason, not a silent no-op (the TaxiAction lesson).
- Idempotent verbs: same request twice = same result twice, no double effect.
  Each verb states its idempotency rule in its row below.
- Evidence returned: every response carries the post-state the verb claims
  (position, strategy set, spell learned, mail item id) — MISSION_EVIDENCE
  doctrine: the reply/position is the ONLY truth, never the order alone.
- Roster-scoped: a verb naming a bot outside the roster allowlist (guild
  'Special FXc' + bot accounts — same gate as chat-tunnel's watch list) is
  refused with `error: not_in_roster`. RNDBOTs and strangers are unreachable
  by construction. The G1 hand-play five are refused the same way
  (BOTCMD_HANDPLAY parity).

## 2. Verb catalog (phase-tagged)

| verb | phase | effect | idempotency rule | evidence returned |
|---|---|---|---|---|
| `catalog` | 1 | list verbs + their gates | pure | the catalog |
| `ping` | 1 | liveness + world-thread round-trip latency | pure | latency_ms |
| `bot_state` | 1 | one online bot: position, zone, strategies, rpg status | pure | the state snapshot |
| `taxi_graph` | 1 | reachable flight nodes for a bot (the `taxi ?` the engine silently swallows, A35) | pure | node list with costs |
| `trainer_catalog` | 1 | spells a trainer class can teach THIS bot, with prices | pure | spell list + affordability |
| `travel_precheck` | 1 | would a teleport/flight to X be legal now (distance, map, faction gate A34)? | pure | verdict + reason |
| `taxi_fly` | 2 | fly a bot to a KNOWN, REACHED node (explicit gate: node must be in `taxi_graph` output) | re-request mid-flight → same state, no second route | route started + arrival evidence (position at destination node) |
| `trainer_learn` | 2 | learn one named spell from the trainer the bot faces (fixes the master-selected-unit wall) | spell already known → `ok, already_known`, no charge | spell id + remaining money |
| `mail_send` | 2 | ONE item bot→Leo, mailbox-in-range gate kept | item gone from bags → `ok, already_sent` | item id + mail id |
| `lease_flag` | 3 | set/clear the module-side lease marker for a bot (mirrors director.py `_LEASES`, one mission per bot) | set twice → same flag | flag state |

Phase 2 verbs are idempotent ACTIONS; phase 3 adds the lease flag so
director.py's registry and the module agree on who owns a bot. No verb ever
bypasses economy rules (trainer/mail prices still apply — §6.7).

## 3. Phases and proof gates

Each phase ships ONLY after the previous phase's live proof is pasted into
the thread and the A-rows are filled. Same protocol as the services
(`services-roadmap.md` order & gates).

- **Phase 1 — read-only.** `catalog`, `ping`, `bot_state`, `taxi_graph`,
  `trainer_catalog`, `travel_precheck`. Proof: harness with a fixture world
  (or a live worldserver with a throwaway bot) answering all six without a
  worldserver crash/tick stall over a 100-request battery; the roster gate
  refuses a stranger and an RNDBOT; a `\n`/pipe/garbage corpus never crashes
  the reader.
- **Phase 2 — idempotent actions.** `taxi_fly`, `trainer_learn`, `mail_send`.
  Proof: each verb double-fired back-to-back shows identical evidence and no
  double charge/double item; each engine-gate refusal returns the engine's
  reason (e.g. `trainer_learn` with no trainer in range, `taxi_fly` with an
  unreached node).
- **Phase 3 — lease flag.** `lease_flag`. Proof: director.py's `_LEASES` and
  the module's flag agree across a stand-down → travel → stand-down cycle;
  two competing missions on one bot answer the second with a lease conflict.

## 4. director.py integration (post-phase-2)

A new service shim (same pattern as db-search's client) — the LLM gains
recipes that call the module's verbs; the whisper pipeline stays for
everything already working. No prompt surgery: new functions land as JSON
recipe entries (house rule). `parse_orders` remains THE shape boundary for
LLM output; module verbs are called by director.py, never by the model
directly.

## 5. Non-goals

- No replacement for the whisper missions that work (travel/tele, farm
  keeper, walk, gather, mail reads).
- No free-form command passthrough (that is what `exec` via chat-tunnel
  already does, gated).
- No writes to `characters`/`world` tables, no DB travel-table patches
  (house rule).
- No TCP socket, no remote reach, no new systemd unit beyond the module
  itself (the socket lives inside worldserver).

## 6. Risk review (each risk → mitigation → phase gate that proves it)

| # | risk | mitigation | proved by |
|---|---|---|---|
| 6.1 | world-thread violations (reader thread touching Player/Map) | reader thread only parses JSON + enqueues; all object access inside world-thread tasks; code-review gate: zero `Player*`/`Map*` dereferences outside task bodies | phase-1 battery + review |
| 6.2 | tick stall (a task blocking the world tick: DB, disk, long loops) | tasks are bounded (no DB, no disk, single-bot scope, hard cap on loop iterations); socket I/O never inside a task; a per-task watchdog logs slow verbs | phase-1 100-request battery: server tick variance logged before/after |
| 6.3 | blast radius (module crash kills worldserver) | phase 1 is read-only; verbs fail closed (`error:...`, world keeps running); no exceptions escape the task wrapper | fault-injection battery (garbage, unknown verb, offline bot, closed socket mid-read) |
| 6.4 | security (socket reachable by another local user/process) | 0660 + worldserver-user ownership; AF_UNIX only; roster gate on EVERY verb including reads; no verb executes strings | roster-gate proof + perms check on serverpc |
| 6.5 | gate-bypass (the module becoming a way around engine checks — the whole point of the taxi/trainer walls) | verbs may only ADD gates; each phase-2 verb's row names the engine gate it keeps (node-reached for taxi_fly, trainer-in-range + affordability for trainer_learn, mailbox-range for mail_send); a verb cannot toggle a strategy or force a state the engine would refuse | phase-2 refusal battery: every gate refusal returns the engine's reason |
| 6.6 | idempotency drift (double-fire doubling an effect) | per-verb idempotency rule (§2) + double-fire proof | phase-2 double-fire proof |
| 6.7 | economy bypass (free spells, item duplication) | trainer/mail verbs go through the engine's own charge paths; `trainer_learn` refuses unaffordable spells with the price; `mail_send` moves the item via the mail system, never a bag edit | phase-2 money/item deltas logged in evidence |
| 6.8 | RNDBOT leakage (verbs reaching random bots the director doesn't own) | roster allowlist is the ONLY path to a bot object; registry lookups (RandomPlayerbotMgr) are never used | phase-1 roster-gate proof on a guild-invited RNDBOT and a stranger |
| 6.9 | version skew (module built against mod-playerbots @ 405f8119 drifting on next AC rebuild) | module pins its include surface to stable AC interfaces (Player, WorldObject, task queue); every mod-playerbots symbol it touches is listed in this doc's A-row and re-verified at the next rebuild (the Nov 5 rebuild audit is the template) | rebuild audit checklist in the A-row |

## 7. Open questions for the owner (block phase 1 start)

1. Module dir name: `mod-director-bridge` (assumed above) — confirm.
2. Socket path/perms: `/tmp/director-bridge.sock` 0660 worldserver-user —
   confirm, or a run-directory path under the server's own tree.
3. Phase-1 `bot_state` scope: strategies + rpg status only, or also
   bag/talent digest (db-search already answers the DB-side of those)?
   Prior art (§8) argues for finer verbs over one fat snapshot — e.g.
   `bot_state` (strategies/rpg) kept thin, with `bot_bags`/`bot_talents`
   added only when a recipe needs them.
4. Does phase 2 need a worldserver.conf kill-switch
   (`DirectorBridge.Enable`, default off per phase) so a bad verb can be
   disabled without a rebuild? (Recommended: yes — and mod-dungeon-clear's
   `DcModuleEnable` is the proven pattern: read once, latched on the first
   world tick via an atomic compare-exchange, first writer wins so a
   `.reload config` can never move the answer under a bot that already
   read it.)

## 8. Prior art reviewed (2026-11-12)

Both repos were cloned and read at the owner's suggestion. Neither is used
as code; they are evidence that the spec's shape is proven in the wild.

### mod-dungeon-clear (jrad7) — the structural template

A drop-in AzerothCore module that drives mod-playerbots tank bots through
dungeons. Its README states the claim our §1 depends on: it "runs against a
stock, unmodified mod-playerbots checkout with no playerbots source edits" —
it SUBCLASSES playerbots' strategy/action/trigger/value classes and links
against them. That is the exact mechanism our verbs will use to keep the
engine's `isUseful`/`isPossible` chains real rather than reimplemented.

- **Loader hook:** `Addmod_dungeon_clearScripts()` in
  `src/mod_dungeon_clear_loader.cpp` — the name AC's generated loader
  derives from the directory name. One function, six `AddSC_*` calls.
  Confirms our "one loader hook".
- **Enable gate:** `DcModuleEnable` — conf bool read once, latched on the
  first world tick (`std::atomic`, compare-exchange, first writer wins);
  a `.reload config` cannot move the answer under a bot. Adopted for §7 Q4.
- **Test discipline:** ~100 gtest files in `t/` over PURE decision kernels
  ("Decide(Observation) -> Verdict, gtested"), engine-free planner halves,
  route probes, plus a live `.dc test` harness writing JSONL run records.
  This is the same philosophy as our harnesses, one level deeper: our phase
  proofs should also split the headless-testable kernel (verb validation,
  roster gate, idempotency rules) from the live battery (tick-stall,
  evidence).
- **Build landmine documented in its `mod-dungeon-clear.cmake`:** MSVC's
  `M_PI`/`_USE_MATH_DEFINES` ordering breaks CORE headers when a module TU
  includes `<cmath>` before the first core header — evidence for our §6.9
  version-skew row: pin the include surface and expect build-system edges,
  not just API drift.

### mod-llm-playerbots (bigr00 fork) — threading + catalog precedent, wrong shape for us

Declined as a fork (upstream currency, 0 stars, no engine-gate fixes), but
its LLM layer is the best written-down statement of our §1 threading rule
(`LlmToolbox.h`): the model provider runs on a worker thread and never
touches game state; the provider RETURNS tool calls; the world thread
executes them against a live `Player*`; unknown tool or bad argument answers
a JSON error field — never throws. Its fixed read-only tool catalog
(get_character_sheet, get_equipped_items, get_bag_items, get_quest_log,
get_nearby_players, get_group_members, get_chat_history, get_server_status)
is phase-1 verb-shape precedent, and suggests splitting our `bot_state`
into finer verbs rather than one fat snapshot (folded into §7 Q3).

Design contrast, recorded: the fork embeds the LLM client INSIDE the
worldserver process (provider on an in-process worker thread). Our design
keeps the model OUT of the process — director.py owns Ollama, the module is
a thin verb endpoint — so a model/provider hang can never touch the world
tick, and the module stays buildable without any HTTP client code.
