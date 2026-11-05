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
RITUAL_ACCOUNTS="I_WAS_HERE2ND I_WAS_HERE3RD I_WAS_HERE4TH I_WAS_HERE5TH"
RITUAL_BOTS="Golbez"
# NEVER add the account you hand-play on to RITUAL_ACCOUNTS - hand-play toons
# are never bots. (Default master account is I_WAS_HERE1ST - see 'botcmd master'.)

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
- Bot-sender gate (2026-11-05 fix): senders on bot accounts (i_was_here% except
  the master account) or on Leo's shell account (i_was_cmd) are dropped by a
  read-only DB account check, and bot master-report lines ("Selling |c...",
  "=== ... ===") are dropped on sight. Only DIRECTOR_HANDPLAY toons on the
  master account plus guild members on non-bot accounts are authed. This does
  not need SOAP: '.playerbots inspect' does not exist in mod-playerbots
  @ e88cc40 and '.playerbots bot' needs an active session, so neither is
  usable from the SOAP console.

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

## Master account rotation (botcmd master)

Which i_was_here* account is the HUMAN account is a runtime setting. One-time
install of the env plumbing:

    cp leo/director.env.example ~/.director.env && chmod 600 ~/.director.env
    mkdir -p ~/.config/systemd/user/leo-director.service.d
    cp leo/leo-director.service.d/override.conf ~/.config/systemd/user/leo-director.service.d/
    systemctl --user daemon-reload && systemctl --user restart leo-director

Then rotate with:  botcmd -> 9. Master   (or one-shot: `botcmd master set I_WAS_HERE5TH`)

It shows a table like:

     current: I_WAS_HERE1ST   (env file: /home/iwashere/.director.env)
      1. [X] I_WAS_HERE1ST    toons: Aeris Cecil* Rydia Shadow* Vivi
      2. [ ] I_WAS_HERE2ND    toons: ...
      3. [ ] I_WAS_HERE3RD    toons: ...
      4. [ ] I_WAS_HERE4TH    toons: ...
      5. [ ] I_WAS_HERE5TH    toons: Golbez*

Selecting one writes DIRECTOR_MASTER_ACCOUNT + DIRECTOR_HUMANS +
DIRECTOR_HANDPLAY (auto-filled from that account's toons, minus
BOTCMD_BOT_TOONS in ~/.botcmd.conf) into ~/.director.env, mirrors them into
~/.botcmd.conf, and restarts leo-director. So the "never command / never
bot-add the group I'm hand-playing" rule follows the rotation automatically.

RULE: only select a HUMAN-ONLY account. Every other i_was_here% account is a
bot account - the daemon drops its toons as senders (DB account check, no
SOAP needed) and never auths them.

## Moving a toon to another account (e.g. Golbez -> I_WAS_HERE5TH)

One-time admin steps - YOU run these against the DB; the tools stay read-only:

    # 1. create the account (worldserver console, or first client login):
    .account create I_WAS_HERE5TH <password>
    # 2. make sure the toon is OFFLINE.
    # 3. point the character at the new account:
    mysql> SELECT id FROM acore_auth.account WHERE username='I_WAS_HERE5TH';
    mysql> UPDATE acore_characters.characters SET account=<id> WHERE name='Golbez';
    # 4. I_WAS_HERE5TH is already in RITUAL_ACCOUNTS defaults now; if you keep
    #    a ~/.leo-watchdog.conf, add it there too. Verify:
    python3 director.py census        # Golbez listed under I_WAS_HERE5TH
