# Leo presence control (systemd user units)

Leo's headless world session becomes a managed service with a watchdog:

- log in:  systemctl --user start leo
- log out: systemctl --user stop leo
- watchdog poll (every 2 min): starts Leo when wanted, stops him when not,
  zombie-kills a hung session (2 consecutive offline readings), and - default
  on - queues the bot-start ritual right after Leo logs in.

Wanted-online = server-local hour in [ON_H, OFF_H) (default 08-22), OR any
toon in GROUP online (default: the G1 manual-leveling five). Outside both,
Leo is logged out. Manual start/stop works, but the next tick re-applies
policy - being online in-game as Cecil/Rydia/Shadow/Vivi/Aeris overrides
off-hours.

## Install (once)

0. pkill -f 'shell.py'                       # retire the manual shell first
1. mkdir -p $HOME/.config/systemd/user
2. ln -sf $HOME/downloads/director-drop/leo/leo.service $HOME/.config/systemd/user/leo.service
   ln -sf $HOME/downloads/director-drop/leo/leo-watchdog.service $HOME/.config/systemd/user/leo-watchdog.service
   ln -sf $HOME/downloads/director-drop/leo/leo-watchdog.timer $HOME/.config/systemd/user/leo-watchdog.timer
3. systemctl --user daemon-reload
4. sudo loginctl enable-linger iwashere      # user units survive boot without a login
5. systemctl --user enable --now leo-watchdog.timer

Do NOT enable leo.service itself - the watchdog owns when Leo runs.

## Knobs (optional): create $HOME/.leo-watchdog.conf

ON_H=8                                          # server-local hour to come online
OFF_H=22                                        # server-local hour to go offline
GROUP="Cecil Rydia Shadow Vivi Aeris Aerith"  # space-separated toon names (both Aeri* spellings until verified in the DB: SELECT name FROM acore_characters.characters WHERE name LIKE 'Aeri%')
AUTORITUAL=1                                    # 1 = ritual after Leo logs in
RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH"
RITUAL_BOTS="Golbez"
# NEVER add I_WAS_HERE1ST to RITUAL_ACCOUNTS - the G1 five are hand-play only.

## Receipts

- systemctl --user status leo
- journalctl --user -n 20 -u leo-watchdog.service     # watchdog decisions
- python3 director.py census                          # roster online states
- tail -n 20 daemon.log                               # director banner

## Notes

- Hours use the SERVER clock. Run date once: if it prints UTC, either
  sudo timedatectl set-timezone America/Vancouver or shift ON_H/OFF_H.
- The watchdog reads characters.online via the read-only MySQL identity -
  same grants director.py uses, no new credentials.
- Re-added bots likely need nc strategy re-issue during sprees (known
  mod-playerbots behavior) - the ritual brings them ONLINE, not back to work.
- director.py daemon (Leo's brain) runs as leo-director.service (see below).

## The director daemon (leo-director.service)

The daemon under systemd too - pinned working directory, log always at
downloads/director-drop/daemon.log, restarts itself, starts at boot.

Defaults since the 2026-11 review fix: validated orders are HELD until the
master whispers 'go' (set DIRECTOR_CONFIRM=0 in the unit's Environment= to
auto-execute again); the purity rails (autogear/maintenance/equip
upgrade/reset botAI/drop/sell/buy/repair/bank/...) are FORBIDDEN in
validate() regardless of CONFIRM; audit.log/daemon.log/daemon.lock are
gitignored - never `git add -A` them into the public repo.

Install:

1. pkill -f 'director.py daemon'          # stop the manual daemon
2. ln -sf $HOME/downloads/director-drop/leo/leo-director.service $HOME/.config/systemd/user/leo-director.service
3. systemctl --user daemon-reload && systemctl --user enable --now leo-director

Receipts:

- systemctl --user status leo-director
- journalctl --user -n 30 -u leo-director        # everything stdout says
- tail -n 30 $HOME/downloads/director-drop/daemon.log

The watchdog only manages leo.service (the shell). leo-director always runs;
it is harmless when Leo is offline (its tail -F just waits for the log).
