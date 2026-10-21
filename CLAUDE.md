# Wynn Build Helper: notes for Claude

This file is the project's memory: who it's for, the owner's rules, how everything fits together, what's been
done and decided, and what's next. Read it before changing anything. Keep it up to date when something
important is decided or built (update the "History" and "Open ideas" sections).

## What it is

A Wynncraft build helper used by the owner (GitHub: Petereta7) and friends, on **Windows only**. It ports
Wynnbuilder's maths exactly and searches for the best whole build (ability tree, gear, skill points, powders,
crafted items) for a goal. A desktop app with a Claude chat ("the bot") sits on top. Friends install it from
this repository and get updates through the app's update bar. Each friend uses their own Claude API key.

## The owner's rules (always follow these)

- **Accuracy above all; no inflated numbers.** Count only what's known. When unsure, be conservative and say so.
  Wynnbuilder is the reference: match it exactly, and say where the helper deliberately differs.
- **Changes must never make builds worse.** New search stages only add candidates; the final pick is the best
  of everything. tests/build_check.py enforces it against recorded scores (tests/golden/build_baseline.json).
  If a change lowers any recorded score, find out why and fix it (don't just re-record).
- **Everything must work for every class** (Archer, Warrior, Mage, Assassin, Shaman). Test all five.
- **"Highest DPS" means Wynnbuilder's Average DPS** (goal melee). Best builds are chosen as a whole, never best
  item per slot.
- **Always prioritise updates:** the update bar always offers the newest version when a newer one exists,
  including after switching back to an older version. No "skip this version".
- **Windows only.** Don't add Mac/Linux work.
- Plain words with the owner and in the app; no jargon they didn't use. Explain ideas before big visual changes
  if asked ("tell me the idea before you do it").
- Follow Wynncraft's rules: no automation that acts in the game (e.g. auto bank sorting is a disallowed mod).

## Files

- `wynn_helper.py`: the calculator (Wynnbuilder port: items, crafted/custom items, powders, skill points, ability
  tree, spells, defenses, toggles, goals, floors/minimums) and the text report (`run()`), CLI.
- `wynn_search.py`: whole-build search (safe filter, local search per weapon on all cores, polish, extra search,
  crafted-item stage, armor powders last), live view for the app, `EFFORT` levels, `cpu_cores()`
  (`WYNN_HELPER_CORES` env override).
- `wynn_craft.py`: crafted item designer (recipe, tier 3 mats, attack speed, 6-ingredient grid with position
  effects; Wynnbuilder crafter rules: profession, ingredient level <= recipe max, durability >= 1).
- `wynn_app.py` + `app/index.html`: desktop app (local HTTP server + pywebview window). Chat with tools
  (`analyze_build`, `manage_characters`, `lookup_item`, `list_abilities`), results visuals, The Forge animation,
  history, home page with characters and library, save-to-library banner, settings, versions, updates.
- `updater.py`: updates from GitHub branch **`stable`** (only versions that passed every check), snapshots in
  `_versions/`, switching versions via `release/<version>` branches. Never touches `PROTECTED` files.
- `notify.py`: the little "build ready" pop-up.
- `tests/`: see tests/README.md. `run_all.py` / `Run all checks.bat` runs everything at once on all cores.
- `.github/workflows/checks.yml`: CI on Windows; publishes to `stable` + `release/<version>` only if all pass.

Personal files, never committed or overwritten by updates: `api_key.txt` (repo has only the blank template),
`wynn_app_config.json`, `wynn_data/build_history.json`, `wynn_data/library.json`, `wynn_data/best_builds.json`,
`_versions/`, `_previous_version/`.

## How to release a change

1. Make the change; keep it accurate and never-worse; cover every class.
2. Run the checks: `python tests/run_all.py` (all at once), or individually: `app_check`, `self_test`,
   `toggle_check`, `speed_check`, `craft_check`, `goal_check`, `build_check` (slow). After a change that makes
   builds better, `python tests/run_all.py --record` saves the higher scores (it never lowers one).
   If Wynnbuilder or game data changed: `python tests/crossval/run_all.py` (needs Node.js + git).
3. Bump `version.json` and add an entry at the top of `versions.json` (same version, date, notes in plain words
   for friends). `tests/app_check.py` checks they match.
4. Scan for secrets: no real `sk-ant-...` key anywhere (app_check checks too). Never commit personal files.
5. Commit as `Petereta7 <Petereta7@users.noreply.github.com>` (never the owner's real email), push to `main`.
   GitHub runs the checks (~35-40 min, the build check is the slow part) and, only if all pass, moves `stable`
   and `release/<version>`, which is what the update bar sees. Don't push `stable` by hand.

## Design decisions worth knowing

- Skill point solving: fast solvers verified against the reference on thousands of cases (speed_check); item
  signatures are per-process (`_SIG_EPOCH`) because a cross-process collision once over-assigned skill points.
  Every final build's skill points are re-checked from scratch.
- Toggles (ability buffs): only buffs with known timing count by default (Bullwhip on when the tree can keep it
  up, best Mystic Mask, Coursing Restraints 4s/12s, ultimate buffs at ULT_DPS_UPTIME 25%). Everything else off;
  users can turn any on, fully or "Name=70%". Part-time buffs are blended exactly (not scaled) when they don't
  scale linearly. The report says what to tick in Wynnbuilder.
- Floors/minimums: a build missing one always scores below every build that meets them.
- Goal mix: main^(1-w) x second^w (like balanced at 50/50). Search filter and fast scoring keep what the second
  goal needs.
- Mana check: Wynnbuilder's model (3 casts/s at 9 clicks/s, income 5 + mr/5 + ms/3, bar 100 + maxMana + Int%).
- Armor powders add health (any element) and elemental defense; chosen for finished builds at the very end, so
  the search path is unchanged and builds only gain. (Putting them mid-search once made a build worse.)
- Crafted items: designed per slot for the finished best builds, all slots designed against the same build so
  results don't depend on core count; designer shake-ups (kicks) matter most for quality; checked against
  brute force (craft_check). Crafts use best rolls like every item. On by default; "crafted": false turns off.
- Search results are deterministic: same answer on 2 or 16 cores, Python 3.10-3.14, Windows (verified), which
  is what makes the never-worse check reliable.
- The bot sees the player's characters every turn (`characters_context()`); `analyze_build` with `character`
  fills in class, level and current gear.

## History (newest first)

- 1.6.0: bot knows your characters (and can update them / save builds); goal mix; mana check; armor powders.
- 1.5.0: home page (class -> characters -> library), big "save this build to your library?" banner.
- 1.4.0: crafted items designed for the build; tests/run_all.py for using all cores.
- 1.3.x: ability buffs counted for their share of time, every class tested; automatic GitHub checks gating
  updates via `stable`; Windows-only CI.
- 1.2.x: version snapshots and switching in Settings.
- 1.1.x: results visuals (equipment screen, tree map, before/after bars), final assembly animation, history;
  fix for over-assigned skill points.
- 1.0.x: first release with updater; The Forge live view (with sound); effective HP minimum.

## Open ideas (not done yet)

- Armor powder specials (Rage, Kill Streak, Concentration, Endurance, Dodge): situational, off by default.
- Owned-items mode: build only from items the player lists as owned.
- New item alerts: after a game data update, re-check saved builds.
- Compare saved builds side by side / share a build with a friend.
- Report-a-problem button (pack build, settings and report into a file).
- Faster search / faster CI build check (currently ~35 min on GitHub).
- Later, when the owner has them: aspects (from raids) and guild tomes.
- Skipped by the owner (don't propose again unless asked): bank auto-organizer (against Wynncraft rules; a
  planner version was also declined), instant small edits without re-searching.

## Working environment notes

- The owner has a 16-core Windows PC. Claude sessions so far ran in a small cloud machine (2 cores); the full
  check run takes ~30 min there, estimated ~4-6 min on the owner's PC. To use the PC, a chat needs folder and
  command access to it (chats started from the Claude desktop app). Otherwise the owner can double-click
  `tests\Run all checks.bat` and paste back `tests\last_run.txt`.
- When killing background processes, don't use `pkill -f`/`pgrep -f` with a pattern that also matches your own
  shell command; match the exact process (it has killed the shell before).
