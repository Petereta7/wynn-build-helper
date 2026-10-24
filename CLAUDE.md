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
- **"Max DPS" / "highest DPS" / "as much DPS as possible" means the most damage in ANY style** (goal mixed: main
  attacks only, spells, or both together, whichever is highest), after the player's minimums (owner, Oct 2026; it
  used to mean Wynnbuilder's Average DPS). Goal melee (Average DPS) is for M1-only builds / comparing with
  Wynnbuilder. Best builds are chosen as a whole, never best item per slot.
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
  history, home page with characters and library, save-to-library banner, compare two builds, share codes,
  settings, versions, updates.
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
   GitHub runs the checks (~20 min, the build check is the slow part) and, only if all pass, moves `stable`
   and `release/<version>`, which is what the update bar sees. Don't push `stable` by hand.

## Speed-ups

Speed-ups must leave every result identical: `tests/same_numbers.py --check` (every number of 2,000 random
builds) and the fingerprints from `tests/search_timing.py` (items, powders, tree, points, score) before and
after. Done so far (1.7.0): repeated crafted items scored once, faster crafted-item assembly and scoring maths,
fine-tuning / deeper search / every top build's craft stage run side by side on separate cores (each job is
independent, results gathered in the same order), shake-ups in speculative batches (a kick that improves
restarts the later ones from the new best; the random draws are replayed so it's exact). Not safe and not done: the simpler skill point solver for
crafted gear (Wynnbuilder's order rules differ). Possible later: splitting the weapon craft by attack speed
(exact only by replaying the shared random sequence, which needs each speed's candidate count first).
Measured deep search on the owner's PC (1.6 -> 1.7): Warrior L106 7.0 -> 4.1 min, Mage L106 17.5 -> ~9 min.

## Design decisions worth knowing

- Skill point solving: fast solvers verified against the reference on thousands of cases (speed_check); item
  signatures are per-process (`_SIG_EPOCH`) because a cross-process collision once over-assigned skill points.
  Every final build's skill points are re-checked from scratch. Gear is put on in Wynncraft's equip order (boots,
  leggings, chestplate, helmet, ring, ring, bracelet, necklace: Wynnbuilder's `wynn_equip`; `_wynn_sorted`), because
  the skip rules depend on input order: helmet-first once called a friend's L50 build wearable with 97 points when the
  game needs 99 (about 2% of random gear sets with negative skill bonuses differed). Build check scores didn't change.
- Toggles (ability buffs): only buffs with known timing count by default (Bullwhip on when the tree can keep it
  up, best Mystic Mask, Coursing Restraints 4s/12s, ultimate buffs at ULT_DPS_UPTIME 25%). Everything else off;
  users can turn any on, fully or "Name=70%". Part-time buffs are blended exactly (not scaled) when they don't
  scale linearly. The report says what to tick in Wynnbuilder.
- Floors/minimums: a build missing one always scores below every build that meets them. Checked on the build's
  final totals (owner: "no negative life steal" = the total, not each item). Keys: hp, ehp, spd, mr, hpr, ls, ms
  (ls/ms added Oct 2026 after the bot hand-patched a build with negative life steal), net, elemental defenses.
  "net" (owner: "no negative health" / never lose more HP than I gain) = hpr/4 + ls/3 per second (wiki: regen every
  4 s, life steal per 3 s while main-attacking); either can be negative if the other makes up for it.
- Spell rotations (spells and mixed goals): a lasting spell (display is a damage-per-second figure: summons, totems,
  storms) counts at most its DPS while up, however often it's recast (`rotation_dps`): recasting refreshes it, doesn't
  stack. Before 1.10.5 a Guardian Angels cast counted its whole duration at 1.5 casts/s (an archer L121 spell build
  showed 1.74M instead of 692k). Conservative: a few such spells might overlap in game.
  Tie-breakers use the capped number too (keeping the old uncapped one made them huge for summon builds and
  distorted spell searches). The tree beam also keeps the states furthest along each archetype (`far`), because the
  changed tie-breakers exposed a fragile path: mage L45 survival's Orphion's Pulse (needs 5 Light Bender) pays only
  after flat steps, and the beam dropped it (72,976 -> 33,538) until this was added.
  Baselines lowered in 1.10.5 (owner chose accuracy first, Oct 2026): assassin L50 balanced 21,190 -> 10,165, assassin
  L55 spells 107,581 -> 60,531, mage L40 spells 23,682 -> 12,800, shaman L70 mix 77,059 -> 75,093 (shaman L60 spells
  rose 55,350 -> 61,823). Evidence (tests/rescore.py, dev repo): the old best builds re-scored and re-searched under
  the fix: mage L40 9,825 and assassin L55 59,564 (both below the new search), but assassin L50 balanced 10,497 (no
  crafts; new quick 8,272, deep 10,295) and shaman L70 89,613 (craft stage; new deep 87,032) are above: the search does
  2-3% worse there in deep mode. Open: close that gap.
- Goal mix: main^(1-w) x second^w (like balanced at 50/50). Search filter and fast scoring keep what the second
  goal needs.
- Mana check: Wynnbuilder's model (3 casts/s at 9 clicks/s, income 5 + mr/5, bar 100 + maxMana + Int%, costs / manaMult
  for Paradox), plus the repeated-spell extra (see Repeated spells). Mana steal works only on main attack hits (wiki; Wynnbuilder has it as an optional tick box): never in
  a spells-only rotation; in the mixed goal ms/3 x the main-attack share (owner caught this, Oct 2026). Warrior's
  Generalist (every 4th cast costs 1) isn't counted yet (conservative).
- Armor powders add health (any element) and elemental defense; chosen for finished builds at the very end, so
  the search path is unchanged and builds only gain. (Putting them mid-search once made a build worse.)
- Crafted items: designed per slot for the finished best builds, all slots designed against the same build so
  results don't depend on core count; designer shake-ups (kicks) matter most for quality; checked against
  brute force (craft_check). Crafts use best rolls like every item. Off unless asked (see below).
- Search results are deterministic: same answer on 2 or 16 cores, Python 3.10-3.14, Windows (verified), which
  is what makes the never-worse check reliable.
- Share codes (`WBH1-` + zlib + base64 with "." for "_", which Discord turns into italics): item names / crafter
  codes + powders, tree, skill points, goal, the sender's numbers and settings notes. The receiver rebuilds every item
  from its own game data (unknown items show as missing, never guessed) and cleans everything else (link must be
  Wynnbuilder's, lengths capped, decompression capped). Numbers are labelled as the sender's. Saved builds keep
  `settings` (party buffs, enemy hits, tomes, aspects, stacks, non-max rolls) so comparisons say what they assumed.
  build_check checks every case's build opens identically from its code.
- The bot sees the player's characters every turn (`characters_context()`, sent as an app note inside each player
  message, NOT in the system prompt: Claude's thinking blocks are bound to the system prompt and everything before
  them, so the system prompt must never change during a chat). `clean_history()` keeps thinking blocks only for the
  turn in progress and only changes earlier turns (shortening old reports), and drops unanswered tool requests.
  `analyze_build` with `character` fills in class, level and current gear.
- Crafted items are off unless the player asks (form box, `crafted: true`, `--crafted`).
- Tree roles (`tree_roles`): each ability is goal (% it adds), path (needed to reach abilities that count; whole
  branch value), extra (adds 5%+ to something else, e.g. an ultimate's combo damage; kept), spare or start. Owner
  decided: spare points are left unspent (`prune_spare` removes an ability only when the goal number stays exactly the
  same), and the report lists ideas for them (one per branch, with the abilities on the way).
- Build from my items (`only_items`, `--only-items`, a character's `items` list = "My items"): `parse_owned_items`
  reads names / CR- codes with x2 / 2x / (2) counts (unknown names left out and said); worn gear counts as owned.
  While the search runs, `wynn_search._ONLY` limits `item_pool` (all levels up to theirs, drops or not) and
  `wh.ONLY_ONE` (rings owned once, also sent to workers in the spec) makes `_wearable` refuse the same ring twice;
  both reset afterwards. Remembered builds are keyed by the list. No usable weapon listed = any weapon (said in the
  report). The report swaps "Gear upgrades" for "Worth getting next": single items not owned, on top of the best
  owned build. Item keys: name, or the recipe for crafted pieces (they share names).
- Item special effects (major IDs): Wynnbuilder's abilities where it has them (`majid_abilities`); for ones it only
  describes, `HELPER_MAJIDS` adds exact ones in its ability format (AUBRIS_TEARS: Frog Dance no damage; ERGOSPHERE:
  Storm Dance's +30 air removed; attached to the base ability the node merges into, with the node as dependency) and
  SELFSUFFICIENT sets the weapon special cycle to 12.5 s. Others (movement, ally, conditional, unstated sizes) aren't
  counted; the report's "Special effects" lines and item cards say which (`majid_status`). Audit Oct 2026: 254
  Fabled items, 118 effects modeled by Wynnbuilder, 88 text-only (mostly movement/ally). Recheck HELPER_MAJIDS after
  game data updates (if Wynnbuilder starts modeling one, its abilities win automatically).
- Tomes (owner asked, Oct 2026): from AUTO_TOMES_LEVEL 55 (tomes need 60) `auto_tomes` picks weapon/armor tomes
  greedily for the finished build after the whole-build search (like armor powders), then re-spends skill points
  with them on (kept only if better); start numbers are recomputed with the same tomes so before/after compare gear.
  Picking them before the search was tried: shaman L70 mix fell 72,295 -> 69,864 (search landed elsewhere), so no.
  Old-name copies (`remapID`) and duplicates never picked; unused armor slots get the most health; no guild tomes
  unless listed; off when the player lists tomes, unticks it (`auto_tomes: false`, `--no-auto-tomes`) or builds from
  their items.
- Mythics (owner, Oct 2026): never in searches/suggestions/default weapon/tomes unless allowed (`mythics: true`,
  `--mythics`, form box): `wh.ALLOW_MYTHIC` set by run() each report, `mythic_ok()` in item_pool, _gear_pool,
  default_weapon, level plan, tomes; part of the remembered-builds key. Worn mythics and mythics in the player's own
  item list are still used.
- Mixed goal = "most damage, any style" (owner asked, Oct 2026): spell rotation at the rate clicks+mana allow, main
  attacks in the remaining 1 - r/3 of the time; linear in r so best is r=0 or r=max per rotation; poison once
  (larger), summons/special added as for spells; always >= spells goal and >= melee goal on the same build
  (goal_check). Helper's own estimate (Wynnbuilder has none). The search first runs spells-only and
  melee-only searches as deep as the main one (owner: "make everything fully deep") and passes their builds as
  `extra_starts`, because alone it once ended 0.24% below the spells
  goal's build (archer L60); with seeds every class at L60 is at or above both (tests/style_check.py in the dev repo).
- Search headroom (Oct 2026, tests/headroom.py in the dev repo): more weapons was the big win; the 'Maximum' search's
  other extras added nothing beyond it. A wider ingredient candidate list for crafts helped single slots but not
  whole builds (one case -0.03%), so it was dropped. Crafting for more top builds helps Shaman/Assassin most; with 30
  weapons the top 6 are mostly the new weapons' builds, so a craft-only run still beat the combined run there
  (154,867 vs 150,317 Shaman; 224,705 vs 221,436 Assassin): next idea is picking craft candidates for diversity.

- Search additions in 1.11.0 (owner: "release 10.5 and do that as well if it means finding better builds"; every one
  only adds candidates). Diagnosis (tests/collect_best.py, dev repo; tests/golden/best_known.json is the benchmark):
  the search picks gear for ONE ability tree (`search_start`, planned around the starting gear), so a main-attack tree
  never seriously tried spell gear: assassin L50 balanced 10,165 vs 15,692 for a spell build (cross-check: their gear
  with our tree 6,789; best_tree from our gear stays at 10,490).
  * Style trees (`style_open(goal, goal2)`: mixed, balanced, or a mix with both kinds): run() also searches from trees
    planned for spells and for main attacks (best_tree with goal spells / melee; skipped when equal to search_start
    or each other), crafted=False, and passes their builds as `extra_finals`; step 4d polishes them for the real goal
    and adds them. Mixed keeps its spell/melee seed searches exactly as before (their builds now also go in as
    extra_finals).
  * 4c: weapons re-ranked on the best 3 gears found (rb = no floors), the top `regear` not yet fully searched get the
    full search starting from those gears too; own polish slots (`regear_polish`).
  * 5b craft rounds (`craft_rounds`): around the best crafted build, local search again with its crafts in the pools,
    then design crafts again; plus a "crafts first" start (best build's weapon/tree, empty gear -> craft stage ->
    rounds), which reaches builds made mostly of crafted items (mage L45 survival 72,976 -> 84,568 before the mana fix).
  * Builds added by 4c/4d get crafts on top of the usual top `craft` builds, never in place of them (`regear_ids`).
  * 5c wider trees (`tree_wide` 120, Maximum 160; `_wide_one`): best_tree at that width for the best `polish` finished
    builds (fine-tuning uses 30, which missed assassin L55's better tree: 58,406 -> 65,520 on the same gear); when
    better, local search for that tree (crafts kept via `_pools_with`) and polish. About +7% time at L55.
  Results on the PC before the mana fix (all cases >= before): assassin L50 balanced 15,564, mage L45 84,568, mage L40
  13,626, shaman L60 mixed 59,126, shaman L70 mix 77,851, assassin L55 62,372, shaman L60 spells 62,572.
  build_check prints how close each case is to its best known build; run_all --record also raises best_known.json.
  High-level deep searches got slower (owner released anyway, Oct 2026; tests/search_timing.py on the PC, deep,
  crafted): warrior L106 9.7 -> 16.0 min (same build), archer L100 11.5 -> 20.2 (+3.8%), mage L106 22.1 -> 37.5,
  shaman L80 18.9 -> 30.9, assassin L106 balanced 16.1 -> 38.1 (10 min of it the style-tree searches). Biggest new cost:
  5b craft rounds (243-687 s, `timings["of which craft rounds"]`). 1.11.1 runs the two chains (rounds around the best
  crafted build; crafts first) side by side, their local searches in the crafting worker pool (`_local_best`), same
  builds. (An earlier "80+ min" reading was wrong: a cut-off PC step loses its output.) Laptop timings are noisy (heat).
- Mana steal / life steal accuracy (1.11.0, owner caught it): both work only on main attack hits (wiki). Mana: see
  "Mana check". Life steal: `ls_share` in score_build (melee 1, spells/burst/healing 0, mixed = main-attack share,
  ability = focus is the main attack, balanced/survival = whichever style deals the build's damage; a goal mix uses
  the main goal's) scales life steal in survival's self-heal and in the "net" minimum (`floor_values(..., ls_share)`;
  negative life steal too). Owner agreed to this reading. Same-numbers changes verified by reverting only these
  pieces (golden reproduced exactly): mana 1955 builds, life steal 251, same sets on Linux and Windows.
  Baselines in 1.11.0 (accuracy fix and search changes together; evidence tests/collect_best.py --old old_mana_code,
  dev repo, = best builds of the new search under the old rules, re-searched under the mana fix): mage L40 12,800 ->
  9,985 (old build re-searched 8,941), shaman L60 spells 61,823 -> 52,097 (45,981), shaman L70 mix 75,093 -> 72,389
  (71,667), shaman L60 mixed 55,549 -> 55,528 (53,421), mage L45 survival 72,976 -> 65,388 (mana fix stage: 72,496 vs
  new 74,984; life steal fix lowered it further: a spell mage's life steal no longer heals). Rose: assassin L50
  balanced 10,165 -> 15,430, assassin L55 60,531 -> 65,520. Open: assassin L50 balanced, a 16,564 build exists under
  the mana fix (before the life steal fix), the search gets 15,430. best_known.json reset to today's numbers for the
  cases the fixes changed.
- How to play / In Wynnbuilder (owner asked, Oct 2026): `how_to_play()` (report section "How to play this build",
  `LAST_RESULT["how_to_play"]`): attacks and order, casting pace from the mana check, buffs counted (TOGGLE_RULES
  reasons), mask, powder special, life/mana steal for the style; only facts the calculator used. `wynnbuilder_view()`
  ("In Wynnbuilder", `LAST_RESULT["wynnbuilder_view"]`; for a pasted link also "Your link in Wynnbuilder" with the
  link's own skill points): numbers Wynnbuilder shows with every toggle off (evaluated as goal melee so no powder
  special boost) plus each reason the app differs. The bot prompt: end new builds with a short How to play from those
  lines only; use the Wynnbuilder section for "why is it different there"; it can't see Wynnbuilder, so ask which box
  / a screenshot when a quoted number matches nothing. build_check fails a case without them.
- Contact spells (owner, Oct 2026: Arrow Shield "is a shield... I'd have to run at the enemy"): `CONTACT_SPELLS`
  (Arrow Shield: its arrows hit "enemies that get too close") count 0 in spell rotations / mixed / mana check
  (`rot_res` in score_build), still in burst; report and How to play say so. Same numbers: 102 archer builds changed,
  reverting CONTACT_SPELLS to empty reproduces the old reference exactly.
- Archetype choice (owner, Oct 2026): `Build(archetype=..., avoid_archetypes=...)`. Main archetype: more of its
  abilities than any other archetype's (`archetype_lead`; abilities from other paths allowed); avoided: none of their
  play-style abilities (`archetype_core`/`avoided_core`: anything but plain raw_stat/stat_scaling boosts with no archetype
  requirement, no toggle/slider; owner: "if a Battle Monk node improves Paladin that's fine, as long as the general play
  style isn't affected"), e.g. element masteries can be borrowed. Both enforced like minimums (shortfall -> floors_met False, graded penalty), so the
  tree planner, every search stage, prune_spare and workers (spec) respect them; part of the remembered-builds key and
  saved build settings. CLI --archetype / --avoid-archetype, app params archetype / avoid_archetypes, form selects (per
  class from /api/meta "archetypes"), bot prompt. Report line "Archetypes in this tree". goal_check: every class and
  archetype at L60; build_check case "archer L60 spells, Trapper, no Boltslinger" (35,776; the stricter first rule found
  35,927, a build still valid now: the looser rule led the search elsewhere. Open gap, see best_known.json).
- Pause (owner, Oct 2026: "I might have to leave ... my WiFi will get cut"): `ws.set_pause(on)` / `reset_pause()` (each
  report), `_pause_wait()` hooked into Build.evaluate every 256 builds scored (`wh.EVAL_STATS["pause"]`); each worker
  pool gets a pause Event through the spec (`_build_spec(build, ctx)`, `_PAUSE_EVENTS`). Only waits, so same build (time
  limits still count the pause). App: /api/pause {on}, Pause/Resume button by Stop; Stop also ends a pause. app_check
  "pause / resume" (same build, it waited). The search never needs internet; if Claude can't be reached after a chat
  search, chat_turn keeps the report (shown + in History) and records it in the chat as an assistant note.
- Crafts-first chain (5b) starts alongside the top builds' craft stages (needs only tops[0]); same builds.
- Archetype starts (1.12.0; owner chose "always on", Oct 2026): for goals where spells count (`archetype_runs_on`:
  spells, burst, ability, mixed, healing, balanced, or as goal2) and no main archetype chosen, run() also searches once
  per archetype (not avoided) with `floor_build.copy_with(archetype=a)` from best_tree planned with that archetype,
  crafted=False; builds go in as extra_finals (scored with no archetype rule, crafts on top of the usual tops). Why it
  helps: the plain search stops at a mix (archetype payoffs sit behind steps that add little). Experiment
  (tests/exp_arch.py, dev repo): shaman L60 spells 52,097 -> 63,766 (Acolyte), mage L40 spells 9,985 -> 11,259
  (Arcanist), assassin L55 none better alone, shaman L60 mixed none better. Release run: those +22.4% / +12.8%,
  assassin L55 +1.3% (66,401), every other case unchanged. Time: about 2x for spell goals (PC run_all 26 -> 42 min).
- Mana goal (owner, Oct 2026: "70 percent survival and 30 percent as much mana regen as possible"): goal "mana" =
  `income` (5 + mr/5, the mana check's figure; no mana steal, it needs main attacks), also a second goal; search
  filter rel {"mr"}. same_numbers unaffected (its goal lists are fixed).
- Repeated spells (1.13.0; owner's idea "1 damage spell, 1 damage spell, 1 reset spell", rule tested by the owner in
  game, Oct 2026): the 2nd cast of a spell in a row costs the same, each later one +5, and the spell right after a run
  carries the last increase (Escape 15, Escape 15, Arrow Shield 30 (25+5), Arrow Shield 25, Arrow Shield 30; Escape,
  Shield, Escape, Shield all normal). `repeated_cast_costs` / `repeat_extra_mana` (a run of m adds 5 x m(m-1)/2 per
  cycle, flat, refunds like Paradox apply). Rotations (spells, mixed, healing) try 2-4 different spells each cast once
  (exactly the old loop first, so ties keep the old rotation) and then each cast once or twice (`double_entries` /
  `double_list`: flat bit-mask entries, spells and mixed in one pass, lasting spells capped inline). Rotation tuples are
  now the cast order with repeats (e.g. (3, 3, 2)); `rotation_dps` / `rotation_breaks` take one entry per cast.
  `MAX_SPELL_RUN = 1` + `HEAL_ONE_SPELL = True` reproduce the 1.12.1 numbers exactly (same_numbers verified).
  ON since 1.13.1 (see Handed-on builds below). Before that it was HELD (MAX_SPELL_RUN = 1 in 1.13.0): with 2, PC build check: archer Trapper 35,776 -> 42,861, assassin L55 66,401 ->
  85,087, shaman L60 spells 63,766 -> 64,618, shaman L70 mix 72,465, others equal, but assassin L50 balanced 15,430 ->
  15,151. Diagnosis (tests/exp_craft.py, dev repo): the Trickster archetype search now ends at a higher-scoring
  uncrafted build whose crafts reach less; the old route's best (crafted rings/bracelet/necklace, 22-node tree) scores
  ~16,557 under the new rules. Tried, didn't help: trying craft designs together in the craft stage (assassin same,
  shaman L70 mix fell to 72,257), crafting the remaining accessories one after another (same). Search time with 2:
  PC run_all 42 -> 52 min.
  goal_check compares spells and any-style numbers with a brute force over every cast order. Scoring cost about +10-25%
  for spell/mixed goals after optimising (first version was +60%). Healing still also allows one heal spell on repeat
  (`HEAL_ONE_SPELL`, max 1 cast/s, normal cost) until we know whether waiting resets the count (owner to test).
  toggle_check: a damage-only buff at a share is scaled, so each spell's damage is exactly half way, but the score may
  pick another rotation, so the score only has to lie between off and on.
- Warrior tree effects Wynnbuilder's data leaves out (1.13.0; owner: "research the paladin tree ... incorporate them",
  "for survival builds prioritise your own survival, don't worry about teammates"): `HELPER_NODE_EFFECTS` adds them to
  the node in Wynnbuilder's effect format (`_with_helper_effects` in Tree): War Scream's own 20% resistance (+5% and
  +8% damage with Emboldening Cry; toggle "War Scream Resistance", TOGGLE_RULES "on": recast every 30s, no cooldown;
  TOGGLE_NEEDS: not with Bak'al's Grasp; party boosts warscream/emboldeningcry ignored when it's on), Second Chance
  (stat lastStandPct 30: survival + 30% health once), Rejuvenating Skin (rejuvPct 30: survival / (1 - 30% x 5/15)),
  Judgement's buff (ult share). Counter/Riposte ("Activate Counter") counted on by default: owner says it's a passive
  always-on resistance node (cooldown is the counter-attack's). Not counted (hit-rate / Holy Power dependent): Mantle,
  Manachism, Luster Purge, Heavenly Trumpet, Radiance (ID effectiveness, Wynnbuilder doesn't model either), ally-only
  effects. Party boosts fixed to Wynnbuilder: War Scream + Emboldening Cry = 25% (was max 20) and damMult.Strength 8.
  same_numbers: reverting HELPER_NODE_EFFECTS to {} and the boost fix reproduces the reference exactly (401 builds
  differ). Other classes' trees haven't been audited the same way yet (open idea).
- Handed-on builds and craft diversity (1.13.1, to turn repeated spells on without assassin L50 balanced falling):
  each search run() starts besides the main one (style trees, archetypes, mixed seeds) hands on its best and its
  next ALT_FINALS (2) best builds with other gear (`alt_finals`, `_distinct_finals`; flagged True in extra_finals).
  Diagnosis: the build that crafts best was the Trickster search's second-best (Solar Sailers/Reminiscence/Erhu,
  10,954 uncrafted -> 16,105 crafted); it never reached the craft stage. Crafts are also designed for up to
  `craft_diverse` builds whose ability tree none of the usual ones has. Displacement guard (`new_ids` in
  _search_best_build): the first try showed new builds can push out old work (shaman L60 spells 58,639, mage L45
  65,231): every limited choice (4d polish order, which builds get crafts, the craft-round seed, the crafts-first
  seed, 5c wider trees) is made exactly as before over the old builds only, and the new ones get extra work on top
  (crafts; a craft round only if their best crafted build beats the old best; a 5c run only if it would have made
  the cut). Result: every case at or above its baseline.
- CI build check runs in 4 parallel parts (`build_check.py --shard k/4`, matrix in checks.yml, 150 min each).

## History (newest first)

- 1.13.1: repeated-spell rotations on (MAX_SPELL_RUN = 2); other searches hand on their next-best builds (ALT_FINALS)
  and crafts are also designed for builds on other trees (craft_diverse), with every old choice kept exactly
  (new_ids). PC build check: archer Trapper 35,776 -> 42,861, assassin L50 balanced 15,430 -> 17,635, assassin L55
  66,401 -> 85,087, warrior L50 melee 7,523 -> 8,253, shaman L60 spells 63,766 -> 64,618, shaman L70 mix 72,389 ->
  72,466, others equal; 56.9 min (was 43.7).
- 1.13.0: Warrior survival: War Scream's own resistance, Emboldening Cry, Counter, Second Chance, Rejuvenating Skin,
  Judgement's buff now counted; War Scream + Emboldening Cry party boosts and the Potion/Strength damage boost max match
  Wynnbuilder. Repeated-spell rotations built and tested but held (MAX_SPELL_RUN = 1) because assassin L50 balanced
  fell 1.8% with them on. PC build check: warrior L50 melee 7,339 -> 7,523 and no-crafts 7,000 -> 7,174 (Emboldening
  Cry's +8%), everything else identical; 43.7 min.
- 1.12.1: "mana regen" goal (mix it in, e.g. 70% survival + 30% mana).
- 1.12.0: archetype starts (the search also starts from a tree built around each archetype for spell-type goals):
  shaman L60 spells +22%, mage L40 spells +13%, assassin L55 +1.3%; never worse; about 2x time for spell builds.
- 1.11.3: Pause / Resume for a running search (same build); a chat build is kept if the connection drops; crafted-item
  step overlaps more (same builds, faster).
- 1.11.2: main archetype / archetype to leave out (form, bot, CLI); Arrow Shield no longer counted as damage per second
  (it only hits enemies that come close).
- 1.11.1: "How to play this build" and "In Wynnbuilder" report sections (the bot uses both); crafted-item rounds run
  side by side (same builds, faster); report text fix for mana income.
- 1.11.0: search also from spell / main-attack trees (most damage, balanced), weapons re-ranked on the best gear,
  craft rounds and crafts-first, wider tree plan for the best builds; mana steal and life steal only from main attacks;
  Paradox refund; best-known benchmark; CI build check in 4 parallel parts. High-level deep searches much slower (fix
  next).
- 1.10.5: spell rotations count summons/totems/storms at most one copy (accuracy fix; spell and any-style numbers drop
  where they relied on stacking recasts).
- 1.10.4: life steal / mana steal / never-lose-health minimums (final totals); the bot may not estimate or hand-edit
  builds.
- 1.10.3: "max DPS" means the most damage in any style (mixed goal, first in the form); seeded by spell-only and
  main-attack-only searches as deep as the main one (about 3x the time).
- 1.10.2: "spells + main attacks" goal (mixed).
- 1.10.1: item special effects: report and item cards say what each does and whether it's counted; Pas De Deux,
  Grand Design and Self-Sufficient now counted (Wynnbuilder doesn't). Tomes in the build from level 55 (owner asked). Mythics left
  out unless the player allows them.
- 1.10.0: build from the items you have (My items per character, form box, bot; "worth getting next"); skill points
  in the game's equip order (fixes a build needing 2 more points than shown); spare ability points left unspent, with
  ideas.
- 1.9.0: deep search 30 weapons (was 16) and crafts for 6 builds x 150 shake-ups (was 3 x 40); crafted items off
  unless asked; crafting table + Forge crafting bench; tree roles (why each ability is there); chat 400 fix (thinking
  blocks are tied to the system prompt, so the characters list moved out of it); shorter bot questions.
- 1.8.1: a character's "Make a build for" button opens the chat (name typed in); the form is the secondary button.
- 1.8.0: compare two saved builds side by side; share codes (open from Home or by pasting in the chat, save to
  library, compare with yours); saved builds remember non-default settings; empty gear slot display fixed.
- 1.7.0: deep search about twice as fast with identical builds (crafted items scored once per distinct item,
  faster maths, fine-tuning / deeper search / shake-ups / every top build's crafts side by side); full check
  run ~5.4 min on the owner's PC (was ~14); same-numbers reference check (one per system).
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
- New item alerts: after a game data update, re-check saved builds.
- Report-a-problem button (pack build, settings and report into a file).
- Faster search / faster CI build check (currently ~20 min on GitHub).
- Search gaps left (best_known.json): archer Trapper case (35,776 vs 35,927 known), assassin L50 balanced (a 16,564
  build existed before the life steal fix).
- Speed up high-level deep searches again (see KNOWN PROBLEM above) without losing the better builds.
- Warrior's Generalist (every 4th spell costs 1 mana) isn't counted in mana checks yet (conservative).
- Audit the other classes' trees like the Warrior one (effects the game states exactly that Wynnbuilder's data leaves
  out, own survival first). Fallen: Corrupted stops healing (not modeled), Brink of Madness.
- Healing goal: a single heal spell on repeat (HEAL_ONE_SPELL, max 1 cast/s) still uses its normal cost, but in game the
  3rd cast in a row costs +5 and so on (owner confirmed) unless waiting resets it. Waiting for the owner's test (cast
  Heal ~4 times about 3 s apart) before turning HEAL_ONE_SPELL off. Mechanics notes: Project doc
  claude/wynncraft_mechanics.md (dev repo docs/).
- Tree planner robustness: beyond the per-archetype states (1.10.5), guided runs toward archetype payoff nodes (like
  the ultimate runs in best_tree) could find more; costs tree-planning time.
- Later, when the owner has them: aspects (from raids) and guild tomes (tomes from level 55 are done, guild ones not).
- Skipped by the owner (don't propose again unless asked): counting abilities that need you to be hit (Mantle,
  Manachism, hit-rate settings; owner: "leave that the way it is now"), Fallen's Corrupted no-healing (only the
  helper's survival score), ultimate charge maths (for now), a click-speed setting (keep 9 like Wynnbuilder), bank auto-organizer (against Wynncraft rules; a
  planner version was also declined), instant small edits without re-searching.

## Working environment notes

- **Owner's rule: run tests and experiments on the owner's PC whenever it's available** (it's much faster than the
  cloud session). Only fall back to the cloud session when the PC runner isn't picking up jobs.
- The owner's PC (i9-11900K, 8 cores / 16 threads, Windows, Python 3.14) is a GitHub self-hosted runner for the
  PRIVATE repository Petereta7/wynn-build-helper-dev (never attach it to the public one). Pushing a snapshot of
  the code there (without .github/workflows/checks.yml, so no hosted minutes are used) runs
  `.github/workflows/pc-tests.yml` on the PC: every check (tests/run_all.py), or, if the snapshot has
  `tests/pc_job.txt`, each command in it. Results land on the `results` branch as `runs/<run id>.txt`
  (fetch with `git fetch origin results:refs/remotes/origin/results`). The runner only works while the owner
  has `run.cmd` open in their `actions-runner` folder. The full check run takes ~5.4 min there (vs ~30 min in a
  2-core cloud session).
- When killing background processes, don't use `pkill -f`/`pgrep -f` with a pattern that also matches your own
  shell command; match the exact process (it has killed the shell before).
