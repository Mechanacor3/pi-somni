# Example setups

Somni's defaults suit everyday coding in interactive Pi. These setups are starting points for other kinds of work.
They are suggestions, not tested presets: start from the closest one, use it for a week, and adjust. Every key is in
[Settings](settings.md), and `/somni-doctor` checks a file for typos.

Each setup says what to put in the config file and what to run when. The examples go in a project's
`.pi/somni.conf`; settings that should follow you everywhere go in `~/.config/somni/somni.conf`.

| | Coding helper | Life coach | Game player |
|---|---|---|---|
| Dreams | Nightly, or at the end of a session | Nightly | Every few games, scripted |
| [Journal](journal.md) | Background catch-up (`auto`) | Every day; catch-up looks back a week | Written by the script (`on`) |
| [Daydreams](long-sessions.md#daydreams-experimental) | Only for long sessions | Off | On, forked |
| [Action words, deep sleep](action-words.md) | Off at first | Off | On, with the game's tools |
| [Forgetting](memory-over-time.md) | 30 days (default) | 90 days, slower fading | 30 days (default) |
| Personal memory | Yes (default) | Yes: it's all about you | Off (scripted runs) |

## Coding helper

One repository, worked on most days in interactive Pi. This is close to the defaults: Somni learns the project's
conventions and your corrections, and a session that runs long keeps its lessons after Pi compacts its context.

```yaml
# .pi/somni.conf
recording:
  tool_result_bytes: 8000      # build and test output is long; keep more of it
journal:
  auto: auto                   # a day Pi closed on gets its entry next session
daydreams:
  every_turns: 25              # only matters in long sessions
  fork: true                   # starts from the session's own context and prompt cache
wiki:
  link_style: markdown         # the vault reads as a GitHub or GitLab wiki
```

- **When to dream.** Run `/somni-dream` at the end of a working session, or on a schedule (below). Run `/somni-wiki`
  once a week, or after a big piece of work.
- **Standing preferences.** Say once how you want work done ("always run the linter before committing") and it heads
  every session's index ([preferences that always apply](everyday-use.md#preferences-that-always-apply)).
- **Outcomes.** Have CI report back with `somni outcome`, so dreams learn from the times the agent said "done" and
  the build said otherwise ([outcomes](outcomes.md)).
- **Action words later.** Once you see the same chore done the same way several times, turn on `actions.enabled` and
  limit `actions.tools` to safe ones.

## Life coach

A long-running conversation about you: goals, habits, how weeks went. There are few tool calls; what matters is what
you said and how it changed over months. Personal memory holds most of it.

```yaml
# ~/.config/somni/somni.conf (it should follow you, not one project)
personal:
  path: auto                   # ~/.somni: lessons about you, shared by every project
  never_share: Sam, Priya      # names /somni-share must never write (yours and your people's)
memory:
  forget_after_days: 90        # keep a season of raw conversation, not a month
  decay: 0.3                   # what you said about yourself fades more slowly
journal:
  auto: auto
  catch_up_days: 7             # if you talk once a week, the catch-up still finds the days
recording:
  image_bytes: 0               # no screenshots needed
wiki:
  page_guide: >
    One page per goal or theme. Say where it stands now, what has helped, what hasn't, and what was agreed
    for next time. Write it the way a good coach's notes read.
```

- **The journal is the heart of it.** Each day you talk gets a dated entry: what you discussed, decided and left open.
  Read the vault's `journal/` folder like a diary. Run `/somni-journal` at the end of a long conversation if you want
  today's entry now.
- **Changes, not mistakes.** When a goal changes, dreams keep the old one as history with an `update` ("ran three
  times a week until March"), so the agent can talk about how things changed.
- **Privacy.** Everything stays on your machine. List names in `never_share` before you ever use `/somni-share`.

## Game player

An agent that plays many games, run from a script, often with screenshots. Sessions are long, the same moves repeat,
and the script, not a person, decides when to dream.

```yaml
# .pi/somni.conf in the game's working directory
recording:
  image_max_edge: 768          # downscale screenshots (needs Pillow in the backend's Python)
dreams:
  max_images: 24               # let a dream look at more of the board
journal:
  auto: on                     # scripted runs too: one entry per day of play
daydreams:
  every_turns: 15              # long games outlast Pi's context; lessons arrive mid-game
  fork: true                   # needs a saved session (not --no-session); otherwise leave it out
  max_reads: 8
actions:
  enabled: true                # a recurring sequence of moves becomes one checked tool
  tools: [click, key, screenshot]
deepsleep:
  tools: [click, key, screenshot]   # /somni-deepsleep may rehearse a word with these for real
```

- **Dream every few games**, in the script: `pi -p "/somni-dream"`. A dream reviews up to 120 records per pass, and a
  game can make that many quickly.
- **Report the result.** After each game, record the score or verdict from outside the agent:
  `somni outcome --text "lost on turn 40: ran out of wood" --verdict fail`. Dreams learn most from a "done" that wasn't.
- **Deep sleep between sessions.** `/somni-deepsleep` builds or improves one action word, trying it with the real tools
  listed above. Use the names your game's tools actually have.
- **Simulated time.** If the script replays or pauses games, set `SOMNI_NOW` per turn so records and journal days get
  the game's time, or set `memory.decay: 0` so fading ignores the clock.

## Benchmarks and harnesses

A harness that scores Somni should start from nothing and change one thing at a time. Set `SOMNI_CONFIG=none` so no
config file applies, and set what the run needs in the environment. Leave `SOMNI_JOURNAL` unset (`auto` writes nothing
in scripted runs) unless the journal is what you're measuring. See [headless runs](headless.md).

## Dreaming and journaling on a schedule

Interactive sessions catch up on the journal themselves. For dreams, or for a machine with no interactive sessions, run
both from cron after midnight. Dream first: the journal pass then sees the claims the dream drew from that day and can
cite them.

```
30 0 * * *  cd /path/to/project && pi -p "/somni-dream" && pi -p "/somni-journal yesterday"
```

Run cron with the same `TZ` as your sessions, so "yesterday" ends at the same midnight. With a nightly job, set
`journal.auto: off` if you want the cron to be the only writer. Journaling a day twice is harmless: the second run
rewrites the entry as a new version.
