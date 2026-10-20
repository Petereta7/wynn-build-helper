# Tests

Two kinds of checks ship with the helper.

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

## 5. Accuracy test (does the search find the true best build?)

```
python3 tests/accuracy_test.py 100 5
```

This makes small random cases (random class, level and goal, two or three items per slot), checks **every** gear combination by brute force, and checks whether the build search finds the same best build. The arguments are the first case number and how many cases to run. Each case takes 1 to 4 minutes because of the brute force; the search itself takes about a second. Every case should say OK.
