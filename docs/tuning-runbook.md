# Leo LLM tuning runbook (gemma4:12b thinking-spiral trial)

Goal: find the generation settings that keep Leo's turns fast and
spiral-free, using the instruments already shipped (DIRECTOR_* knobs,
spiral-tail logging, the 4096-token brake, turn timing in daemon.log).

## 0. Baseline (no changes)

```bash
cd ~/downloads/director-drop && git pull
sed -i '/DIRECTOR_DEBUG/d' ~/.director.env        # off unless diagnosing
systemctl --user restart leo-director
tail -3 daemon.log                                  # "Leo is listening"
```

Whisper these five, one at a time, waiting for each reply:

1. `send golbez to stv`                      (simple travel)
2. `have golbez farm peacebloom in stv`       (compound: skills + mats + farm)
3. `have golbez gather herbs in stv`          (the A38 case)
4. `where is golbez right now?`                (query-only)
5. `make gau level in thousand needles`        (level_in_zone + travel)

After each, note the turn time from the reply or:

```bash
grep "turn " daemon.log | tail -5
grep -c "spiral" daemon.log                     # capped spirals so far
grep "llm answered in" daemon.log | tail -5
```

Baseline expectation (A31): a healthy turn is <30s; prompt ~4.2k tokens,
thinking ~700, answer ~200. Anything near the 120s LLM_TIMEOUT or a
`llm hit the 4096-token brake` line is a spiral.

## 1. The trial: repeat penalty 1.15

```bash
echo 'DIRECTOR_REPEAT_PENALTY=1.15' >> ~/.director.env
systemctl --user restart leo-director
```

Run the same five whispers. Then compare:

```bash
grep "spiral tail" daemon.log | tail -6        # what it got stuck repeating
grep "llm answered in" daemon.log | tail -10
```

What you are looking for:
- fewer/shorter `spiral tail:` lines than baseline
- turn times closer to the 6-34s range than the 60s+ range
- NO correctness loss: the five replies should still resolve
  (travel -> Booty Bay, farm order, gather note, position answer)

## 2. If spirals persist: tighten further, one knob at a time

```bash
sed -i 's/DIRECTOR_REPEAT_PENALTY=.*/DIRECTOR_REPEAT_PENALTY=1.25/' ~/.director.env
# or add temperature control (default 0.2 via Ollama; director passes
# DIRECTOR_TEMPERATURE only when set):
echo 'DIRECTOR_TEMPERATURE=0.2' >> ~/.director.env
systemctl --user restart leo-director
```

Change ONE knob per sitting; five whispers per knob; write down the
turn times. If a setting makes replies dumber (wrong zone, malformed
orders, SKIPs that used to pass), back it off - the whole point of the
LLM is the LLM, we brake runaway repetition, not reasoning.

## 3. Reading the spiral tail

The `spiral tail:` line is the last 600 chars of thinking when the
brake fired. Patterns seen so far:
- repeating the same bullet/JSON line over and over -> repeat penalty
  is the lever
- re-deriving the zone table from scratch each pass -> the recipe sweep
  missed (check the recipes query ran; if not, the whisper phrasing
  needs keywords)
- circling on a fact it lacks (a name, a number) -> it is missing a
  query; that is a recipe gap, not a generation-settings problem

## 4. Locking it in

When a setting survives all five whispers twice with no brake lines,
write it into `leo/director.env.example` comments (values stay in
~/.director.env on serverpc, which is NOT committed) and note the
result in docs/assumptions.md as an A-row (generation profile).

## Housekeeping

- `DIRECTOR_DEBUG=1` stays OFF unless actively diagnosing (it dumps
  raw thinking to daemon.log).
- The knobs: DIRECTOR_MAX_TOKENS (brake, default 4096), DIRECTOR_TEMPERATURE,
  DIRECTOR_REPEAT_PENALTY, DIRECTOR_TOP_K, DIRECTOR_TOP_P, DIRECTOR_THINKING=0
  (opt-out last resort only).
