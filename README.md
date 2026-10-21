# Wynn Build Helper

A build helper for Wynncraft, all five classes. Tell it about your character in plain words, and Claude works out the best build with you. Or fill in a short form. Every number comes from Wynnbuilder's own formulas, so nothing is guessed.

You get:

- **Best whole build**: every piece of gear picked together, not the strongest item per slot. Skill points items give and need from each other, set bonuses, elements, major IDs and the ability tree all count.
- **Minimum stats you choose**: for example "max DPS, but at least 20,000 health, 60,000 effective HP and no slower walk speed". The best build is picked only among builds that keep them, and every result shows health, effective HP and walk speed next to your current gear.
- **Ability tree**: the best legal tree for your level, in click order.
- **Skill points**: what to assign, and the order to equip items.
- **Powders** for your weapon and armor, and the special they unlock.
- **Gear upgrades** you can wear now, plus set combos. Items that need a quest, can't be traded, or don't drop from mobs are marked.
- **Crafted and custom items** from Wynnbuilder links or `CR-...` / `CI-...` codes.
- **Tomes and aspects** that help your goal most.
- **Damage and survival**: spells, main attack, poison, summons, ultimates, effective HP, regen, life steal and healing, and how many hits of a given enemy attack you can take.
- **Level plan**: for each upcoming level, the abilities to click, where your 2 new skill points go, and gear that unlocks then.
- **Wynnbuilder links** for your current build and the whole new build.

## Getting started

1. **Install Python** from https://www.python.org/downloads/. On Windows, tick **"Add Python to PATH"** on the first installer screen.
2. **Download** the app from https://github.com/Petereta7/wynn-build-helper (green **Code** button, then **Download ZIP**) and **unzip** it anywhere. You only do this once: after that the app updates itself (it asks first).
3. **Double-click** `Start Wynn Helper.bat` (Windows) or `Start Wynn Helper.command` (Mac). The first start sets up the app window, which takes about a minute. After that it opens straight away.
   - Mac: if it says the file is from an unidentified developer, right-click it, choose **Open**, then **Open** again.
4. **To chat with Claude**, open **api_key.txt** in the app folder, paste your Anthropic API key on the empty line and save. Get a key at https://console.anthropic.com/settings/keys. You can also paste it in the app under **Settings**, which saves it to the same file. The key stays on your computer. The API is billed to that account, separately from a Claude subscription. The chat uses Claude Opus 5.5 (the smartest model) by default; it costs more per message than Claude Sonnet 5.5 or Haiku 5.5, which you can switch to in Settings to keep costs down ("New chat" also helps).
   - No key? The **Fill in** tab gives the same results without chatting.

Then just talk to it, for example:

> I'm a level 47 mage with Haqherphix, Ridge, Phosphene, Adventurer's Pants and Morph-Gold. I want a melee build.

> I hit level 50. What changes?

> I keep dying to a boss that hits for about 3000 fire. Make me tankier without losing too much damage.

While it searches, **the Forge** shows the search live: a reel for each slot locks onto the best build found so far (and flicks through other items as the search tries swaps), the score for your goal climbs, a counter shows how many builds have been checked, and each weapon being built around lights up. Everything on it is real data from the search: sparks fly in the item's rarity colour when a slot improves, the score rolls up with a "+x%" on every new best, and embers rise faster the faster builds are being checked. Turn on **Sounds** in Settings for an anvil clang on each new best and a big one at the finish. (If your computer is set to reduce motion, the Forge stays calm.)

Before the first build, Claude asks whether you want any minimum stats kept (like at least 20,000 health or no slower walk speed). Say "no" to skip.

The build appears on the right. When the search finishes, every item flies into a glowing core that detonates and drops the pieces into place on your equipment screen (click or press Esc to skip). Then you get:

- **Before → after bars** for your goal, main attack and spell damage, effective HP, health, walk speed and mana regen, with your minimums marked on the bars. "Before" is the gear you entered; if you didn't enter any, your last build for the same class and goal; and for your very first build, zero.
- **Your equipment screen**: every piece in its slot as a Wynncraft-style card (hover or tap for its stats, requirements and powders), "NEW" on pieces that changed, and your skill points split into what you assign and what your gear gives.
- **The ability tree map**: the real tree with your abilities lit up and numbered in click order, coloured by archetype (hover for what each does).
- **Save to your library**: when a build is ready, a big banner at the top asks whether to save it to your library. Pick which character it's for (or make a new one) and it's kept there.
- **The bot knows your characters**: say "make my shaman tankier", or use a character's name, and it uses that character's class, level and current gear. Tell it a character is "level 60 now", or "save that to" a character, and it updates the library.
- **Home page and characters**: the app opens on your characters. Pick a class, then a character, to see its library of saved builds (the current one marked), view any of them in full, and **Make a build for** that character: the form is filled in with its class, level and current gear (crafted pieces included), or the chat is started for you. Characters and their libraries are kept on your computer only (`wynn_data/library.json`), and updates never touch them. The **Home** button at the top goes back there anytime.
- **History**: every build you make is saved on your computer only (the History tab) and kept through updates. Everyone starts with an empty history. When a build is ready, a small white box pops up in the top-left corner of your screen, on top of other windows, and disappears by itself, so you can play while it searches. (Games in exclusive fullscreen can cover it; turn the pop-up off in Settings if you don't want it.)

## Updates

The app checks for a new version when it opens (you can turn this off, or check by hand, in **Settings**). If there is one, a bar at the top shows what's new with an **Install update** button. Nothing is installed until you click it. The update comes only from this app's GitHub page, and only once it has passed every automatic check (the tests run on Windows, including a full build for every class; a version that fails is never offered). It never changes your `api_key.txt`, your settings or your saved builds. The files it replaces are kept in `_previous_version` until the next update. The app restarts with the new version.

From a terminal: `python updater.py` checks and asks before installing.

**Going back to an earlier version:** if an update causes problems, open **Settings → Versions**, pick an earlier version and click **Switch to this version**. The app restarts on that version. Your API key, settings, history and saved builds stay as they are. The update bar always offers the newest version whenever you're on an older one (including after switching back), so coming back is one click. Versions you've used are kept on your computer, so switching back to them works offline; others are downloaded from this app's GitHub page. From a terminal: `python updater.py 1.1.0`.

## Ability buffs (toggles)

Many abilities only give their bonus while a toggle is on (Wynnbuilder leaves them all off). To keep numbers honest, the helper only counts the ones whose timing is known:

- **Bullwhip**: on (summoners keep enemies Whipped with Uproot or Haunting Memory).
- **Mystic Masks**: you always wear one, so the helper uses the mask that helps your goal most (and Lunatic Transmute with the Lunatic mask).
- **Coursing Restraints**: counted for the share of time its duration and cooldown allow (4s every 12s, so 33%).
- **Ultimate buffs** (Angelic Ascension, Gravitational Collapse, Awakened): counted for the same share of time as ultimate damage (25%).

Everything that depends on a resource whose timing the game data doesn't give (Holy Power, Tricks, Unstable, Blood Pool, Corrupted, "next hit" effects) stays off. The report lists every toggle in your tree, shows what each off one would add if you keep it up, and tells you which to tick in Wynnbuilder to see the same numbers there. If you keep a buff up, say so (for example "I keep Contamination up about 70% of the time") and it counts, for that share of the time, when the tree and gear are chosen.

## How the best build is found

There are trillions of possible gear combinations (about 643 trillion for a level 60 Warrior), far too many to try one by one. The helper:

1. **Throws out items that provably can't win**: items you can't wear even in the best case, and items another item beats in every way your goal uses. Set pieces and major-ID items are always kept.
2. **Builds around each top weapon** (every attack speed and element gets a chance), starting from several gear sets: empty, yours, a greedy fill, and the best builds remembered from your earlier scans with the same settings.
3. **Improves each one** by swapping one, two (and in Deep search three) items at a time, scoring the whole build every time: skill points items give each other, set bonuses, elements, major IDs.
4. **Fine-tunes the best few**: ability tree, exact skill point split and powders.
5. **Searches some more on top**: a quick look at 12 more weapons (32 in Deep search) that step 2 skipped, the full search around the most promising of them, and deeper three-item swaps plus a few shake-ups around the best builds. These only add builds to choose from; everything above is kept exactly as it was, and the answer is the best of all of them, so this step can only find better builds, never worse ones.

6. **Designs crafted items** for the best builds: for each slot (weapon included) it designs the crafted item that makes the whole build best with the rest of the build on, and keeps it where it beats the normal item; then fine-tunes again around the crafts. See "Crafted items" below. Like step 5, this only adds builds to choose from.

If you set minimum stats, a build that misses one always scores below every build that meets them all, so the search moves toward builds that qualify and then maximizes your goal among them. If nothing in range can meet them all, the report says which minimum was missed and shows the closest build.

It uses every CPU core, and remembers builds it has already scored, so repeats cost nothing. The weapons, the fine-tuning of the best builds, the deeper search, the shake-ups and the crafted items for every top build all run side by side on separate cores, and each gives exactly the answer it would one at a time. **Deep search** is the default (untick it in the app, or use `--quick`, for a faster search). It tries more weapons, more starting points and three-item swaps, then shakes up the best build and searches again, and designs crafted items; on an 8-core / 16-thread PC it takes about 4 to 9 minutes at level 100+ (about twice as fast as version 1.6), less at lower levels. Progress and time left are shown, and you can stop anytime and keep the best build so far.

Every speed-up is checked to change nothing: `tests/same_numbers.py` compares every number of 2,000 random builds with a recorded reference, and `tests/search_timing.py` shows that the builds found (items, powders, tree, skill points, score) are exactly the same as before.

Searches remember their best builds in `wynn_data/best_builds.json` (only for the exact same settings), so the next scan can only match or beat them.

Every result says **how sure** it is: how many independent search attempts (each from a different starting point) ended at the same best build. If they disagree, try Deep search.

**Accuracy:** on small cases where every possible combination can be checked by brute force, the search found the true best build every time (see `tests/accuracy_test.py` to run it yourself). On full-size cases nobody can check every combination, so there it's very likely, not guaranteed, to be the best.

## Crafted items

The search designs crafted items for your build and uses them wherever they beat normal items (often accessories and mid-level gear). For each slot it picks the highest recipe you can wear (most health or damage), tier 3 materials, for weapons the best attack speed, and the six ingredients and where each goes in the 3 x 2 grid (some ingredients make their neighbours stronger or weaker). Every craft follows the game's crafting rules, the same ones Wynnbuilder's crafter checks: each ingredient usable by that profession, none above the recipe's level range, and durability left over.

The report and the "How to craft" panel show each crafted piece's recipe, materials, the ingredient grid, where each ingredient drops, and a link that opens it in Wynnbuilder's crafter. Someone has to craft it: you (you can craft an item up to your own level in that profession) or a crafter. Crafted stats use the best rolls, like every other item (Wynnbuilder does the same).

Don't want crafted items? Untick "Design crafted items" in the form, tell the bot, or use `--no-crafted`.

How well the designer does is checked in `tests/craft_check.py`: on small cases every possible ingredient grid is tried and the designer must find one exactly as good, and every craft it designs is checked against the crafting rules.

## Command line (optional)

Everything also works from a terminal, without the app:

```
python wynn_helper.py
```

It asks you a few questions, or see the examples and options below.

## Examples

```
python3 wynn_helper.py --from-link "https://wynnbuilder.github.io/builder/#..."

python3 wynn_helper.py --class mage --level 42 --goal spells --gear "Haqherphix, Ridge, Phosphene, Adventurer's Pants, Morph-Gold" --keep "Arcane Transfer"

python3 wynn_helper.py --class warrior --level 106 --goal balanced --powders "weapon=t6 t6 t6" --sliders "Corrupted=60" --buffs fortitude

python3 wynn_helper.py --class shaman --level 100 --goal survival --gear "CR-...your crafted item code..., Gaping Cavity" --enemy "fire 4000"

python3 wynn_helper.py --class warrior --level 60 --goal melee --min "hp=6000, walk speed=0"
```

## Goals (`--goal`)

| Goal | Optimizes for |
|---|---|
| `spells` | Spell damage you can keep up, limited by your mana (summons and sigils included) |
| `burst` | Biggest one-time combo |
| `melee` | Highest DPS, exactly as Wynnbuilder shows it (Average DPS: damage per hit × attack speed). Use this for "highest DPS". |
| `survival` | Effective HP, plus healing and regen |
| `balanced` | Damage and effective HP together |
| `ability` | Highest damage of one ability you choose with `--focus`, e.g. `--focus "puppets"`, `"Meteor"`, `"Bash"`. The tree and gear are chosen for that ability alone. In the chat, just say "maximum puppet damage". |
| `healing` | Healing per second (support) |

**Mixing two goals:** "mostly spell damage, but I want to survive too" is a main goal plus a second goal with a weight, for example 70% spells + 30% survival (`--goal spells --goal2 survival --goal2-weight 30`, the "Also care about" box in the form, or just say it to the bot). The score is a weighted mix of the two, like `balanced` (damage and effective HP at 50/50), so the best build is the one that's good at both in that proportion. It's softer than a minimum: use a minimum when you have a number in mind ("at least 20,000 health"), a mix when it's a preference. The report shows both parts before and after.

**Mana check:** for spell builds the report and results screen say whether your rotation can be cast at full speed (3 casts a second) forever, or how many seconds it lasts from a full mana bar and how many casts a second you can keep up after that (which is what the spell damage counts). It uses Wynnbuilder's mana model: 5 + mana regen/5 + mana steal/3 mana a second, a bar of 100 + max mana bonus + your Intelligence %.

**Armor powders:** the whole-build search also picks powders for armor pieces with empty slots, when they make the build better: every armor powder adds health (5 to 75 by tier, any element), so tanky goals and health minimums gain from them; the element matters against an enemy hit you enter or an elemental defense minimum. Pieces you already powdered are left as they are.

## Options

| Option | What it does |
|---|---|
| `--from-link` | Load your build from a Wynnbuilder link: class, level, gear, powders, tomes, aspects and tree. The report compares your tree with the best one. |
| `--class`, `--level` | Your class and combat level |
| `--gear` | Your items, comma-separated. Spelling can be a bit off; it picks the closest match and tells you. Crafted and custom items go in as their Wynnbuilder codes (`CR-...`, `CI-...`). |
| `--keep` | Abilities you want no matter what, e.g. `"Arcane Transfer"` for the mana bank |
| `--powders` | Powders you already have, e.g. `"weapon=t6 t6; chestplate=w4 w4"`. Leave the weapon out to get a recommendation. |
| `--max-powder-tier` | Highest powder tier you can get (default 7; item level also limits this) |
| `--tomes` | Tomes you own, by name |
| `--aspects` | Aspects you own with tier, e.g. `"Comet 2, Ray of Frost 1"` |
| `--aspect-tier` | Tier to assume for recommended aspects (default 1) |
| `--sliders` | Your typical stacks, e.g. `"Distortion=40"`. Sliders you don't set use Wynnbuilder's default. |
| `--toggles` | Ability buffs you keep up, with an optional share of the time: `"Contamination=70%, Activate Luster Purge"` |
| `--toggles-off`, `--no-auto-toggles` | Turn off buffs counted by default, or all of them (exactly like Wynnbuilder's defaults). |
| `--toggles` | Ability toggles to turn on, e.g. `"Activate Gravitational Collapse"` |
| `--buffs` | Party buffs: totem, fortitude, chantlunatic, warscream, emboldeningcry, hauntingfanatic, hauntinglunatic, eldritchcall, judgement, chantfanatic, weathering, acidrain |
| `--raid-buffs` | Raid buffs, e.g. `"Lightbearer-II, Seraphim-I"` |
| `--rolls` | Item stat rolls: `max` (perfect, like Wynnbuilder; default), `avg`, `min` or `base` |
| `--enemy` | An enemy hit to measure survival against, e.g. `"fire 3000"` or `"fire=2000, earth=1000"`. Elemental defenses then count, and armor powders are picked for it. |
| `--min` | Minimum stats every suggested build must keep, e.g. `"hp=20000, walk speed=0"`. Stats: health (`hp`), effective HP (`ehp`, against `--enemy` if given), `walk speed` (%), `mana regen`, `health regen`, and `earth`/`thunder`/`water`/`fire`/`air def` |
| `--min-hp`, `--min-ehp`, `--min-walk-speed`, `--min-mana-regen` | The same minimums one at a time |
| `--drops-only` | Only suggest items that drop from mobs, loot chests or dungeons |
| `--no-crafted` | Don't design crafted items for the build (they're designed by default) |
| `--no-whole-build` | Skip choosing all gear together (faster) |
| `--quick` | Faster normal search (about a minute) instead of the default deep search |
| `--time-limit` | Stop the whole-build search after this many seconds and keep the best so far |
| `--item-levels` | Only use items within this many levels below yours (default: every item up to your level) |
| `--poison` | `wynnbuilder` (default, its in-game-tested figures) or `low` (listed value ÷ 3 per second, no stacking) |
| `--cps` | Clicks per second you manage (default 9) |
| `--rank-ap` | Extra ability points from rank: 2 for VIP+, 4 for HERO and above |
| `--plan-levels`, `--per-slot`, `--beam` | Plan length, suggestions per slot, search width |
| `--offline`, `--no-official`, `--no-gear`, `--no-plan` | Skip downloads, the official check, or sections |

## Where the data comes from

Each run checks for new data and saves it in `wynn_data/`, so it also works offline.

- **Wynnbuilder** (https://github.com/wynnbuilder/wynnbuilder.github.io): ability trees with every rule, spell damage multipliers, items, tomes, aspects, major IDs and link formats.
- **Wynncraft's official API**: used to cross-check the ability tree and your items. If the two disagree, for example right after a patch, the report tells you what differs.

## How accurate is it?

The app and the command line use the same calculator. Each part was run side by side with Wynnbuilder's own code on the same inputs:

| Part | Cases checked | Result |
|---|---|---|
| Damage calculation (including part- and element-specific multipliers) | 2,000 | Identical |
| Item roll ranges | 22,427 stats on 5,420 items | Identical |
| Skill point solver | 1,900 real gear sets | Identical except one tie (same total points) |
| Weapon powder damage | 500 | Identical |
| Effective HP, regen and defenses | 1,000 | Identical |
| Wynnbuilder links | 300 builds | Identical links; all decode back correctly |
| Crafted items (stats, requirements, powdered damage, codes) | 3,000 random crafts | Identical |
| Custom items | 800 | Identical |
| Links with crafted and custom items | 200 builds | Identical |

Tree rules match Wynnbuilder's, plus the level 90 requirement for ultimates. Powder special rules follow the official wiki. The `tests` folder has these checks so you can re-run them after a game patch (see `tests/README.md`).

Things it doesn't do, or estimates:
- **Armor powder specials** (Rage, Kill Streak and the like) are listed but not counted, since they depend on the fight.
- **Poison**: uses Wynnbuilder's figures, which assume poison from repeated hits stacks. The wiki only says the listed value is "total damage over 3 seconds", so `--poison low` gives a cautious estimate to compare.
- **Enemy hits** (`--enemy`): elemental defense is a flat reduction of that element's part of each hit, applied after the percentage reductions (official wiki). Negative raw defense stops at 0 even with a big % bonus (Wynnbuilder's display lets it turn positive).
- **Where items come from**: the game data only says an item "never drops" from mobs, not whether it's a raid, quest, merchant or event reward or was removed. Those items are marked; `--drops-only` hides them.
- **Aspects**: about half have effects in Wynnbuilder's data; the rest can't be scored, and the report says so.
- **Timing assumptions**, printed with every report: summons and sigils active half the time, triggered hits every 3 seconds, ultimates active a quarter of the time, weapon specials about every 6 seconds on melee builds (15 otherwise), and boosts from specials assume single-target fights.

## License

GPL-3.0, because it ports logic from Wynnbuilder (GPL-3.0). See LICENSE. Not affiliated with Wynncraft or Wynnbuilder.
