# Tests

## Automatic checks on GitHub

Every push to `main` runs these checks on Windows, the only system the app is used on (newest Python, plus Python 3.10, the oldest the app runs on): the app check, self-test, toggle check, speed check and craft check, and the build check for every class. **Only when every check passes** is that version copied to the `stable` branch, which is where the app's update bar looks, and kept as `release/<version>` for switching back. A version that fails never reaches anyone.

Once a week (and on demand: Actions tab → Checks → Run workflow) the helper is also compared side by side with Wynnbuilder's current code, plus three brute-force accuracy cases, to catch game or Wynnbuilder updates that change the numbers.

The setup is in `.github/workflows/checks.yml`. The checks below can all be run by hand too.

## Everything at once, on all your cores

```
python tests/run_all.py
```

or double-click **Run all checks.bat** in this folder (Windows). It runs every check the GitHub checks run, side by side, splitting the computer's CPU cores between them (each build case gets its share; the answers don't depend on how many cores a case gets, only how fast it finishes). On a 24-core PC it takes a few minutes instead of the half hour or so it takes one after another. The summary is printed and saved to `tests/last_run.txt`.

- `--full` also runs the side-by-side comparison with Wynnbuilder's code (needs Node.js and git) and brute-force accuracy cases, one per two cores (up to 24).
- `--record` saves higher build scores to `tests/golden/build_baseline.json` (never lowers one).
- `--cores 16` uses 16 cores instead of all of them.

## 1. Quick self-test (Python only)

```
python3 tests/self_test.py
```

This needs no internet and no other software. It checks the helper against results that were confirmed identical to Wynnbuilder's own code. Those results are stored in `tests/golden/`:

- crafted item stats, requirements, powdered damage and codes
- custom item decoding
- Wynnbuilder links (with normal, crafted and custom items)
- spell damage, skill points, weapon powders and defenses

It also checks a few known builds, and that the tree search gives a legal tree for every class.

It takes about 15 seconds. Every line should say PASS. If you change the code, run it to make sure nothing broke.

## 2. Side-by-side check against Wynnbuilder (after a game patch)

When Wynncraft or Wynnbuilder updates, the golden results can go stale. This check runs Wynnbuilder's current JavaScript next to the helper on thousands of random inputs and reports any difference.

You need:

- **Node.js** 18 or newer (https://nodejs.org)
- **git**

Then run:

```
python3 tests/crossval/run_all.py
```

It clones Wynnbuilder into `tests/crossval/wynnbuilder` the first time, or updates it. It then generates random cases with the helper, runs the same cases through Wynnbuilder's code, and prints one line per area:

```
damage            2000/2000 identical
skill points      1499/1500 identical (1 tie: same total points)
rolls             ...
```

Use `--refresh-golden` to replace the files in `tests/golden/` with the new results, but only after every area reports identical.

## 3. Speed check (fast skill points = reference)

```
python3 tests/speed_check.py
```

The search uses a faster skill point solver. This compares it with the reference version on 3,000 random gear sets (many with negative skill bonuses, where the equip order matters): points per skill, totals and equip order must all be identical. Both lines should say 0 differences.

## 4. Toggle check

```
python3 tests/toggle_check.py
```

Checks that a buff that's fully on gives the same numbers as Wynnbuilder's on switch, that a buff up part of the time adds exactly that share of its effect, and that "the best Mystic Mask" really is the best of the three.

## 5. App check

```
python3 tests/app_check.py
```

The things that would break the app for everyone: every Python file compiles, the page's JavaScript has no syntax errors (needs Node.js, skipped without), no real API key is in any file, `version.json` matches `versions.json`, an update and a switch back (run on a copy of the app) never touch the API key, settings, history or saved builds, and the app's server answers the page and its data for every class. Takes a few seconds.

## 6. Build check (every class, never worse)

```
python3 tests/build_check.py
```

Runs the whole app, the same way as asking the bot, for one build per class (each with a different goal), plus one with a minimum health and one with an ability buff part of the time. Each build must be wearable (items in level range, the class's weapon, skill points worked out from scratch, a legal ability tree, minimums met), and its score must be at least the one recorded in `tests/golden/build_baseline.json`, so a change can never make builds worse without this failing. After a change that makes builds better, run `python3 tests/build_check.py --record` to save the higher scores (it never lowers one). Takes about 10 minutes.

## 7. Craft check

```
python3 tests/craft_check.py
```

The crafted item designer: on small cases (one per class, different slots and goals, and a weapon) every possible 6-ingredient grid is tried and the designer must find one exactly as good; every craft it designs (82, all classes and levels) is checked against the crafting rules from the raw game data (profession, level range, durability); and each craft's code for Wynnbuilder's crafter reads back as the same item. About a minute.

## 8. Accuracy test (does the search find the true best build?)

```
python3 tests/accuracy_test.py 100 5
```

This makes small random cases (random class, level and goal, two or three items per slot), checks **every** gear combination by brute force, and checks whether the build search finds the same best build. The arguments are the first case number and how many cases to run. Each case takes 1 to 4 minutes because of the brute force; the search itself takes about a second. Every case should say OK.
