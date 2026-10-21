"""
Same numbers, bit for bit: scores thousands of random builds (every class, every goal, with buffs, minimums,
enemy hits, goal mixes, crafted and powdered items) and writes every number the scoring produces to a file.
Run it with the old code and the new code; the two files must be identical. Speed-ups must pass this.

    python tests/same_numbers.py --check      score them and compare with tests/golden/same_numbers.json
                                              (the automatic checks run this: every number must be identical)
    python tests/same_numbers.py --record     save the current numbers as the reference (only after a change
                                              that's meant to change numbers, e.g. an accuracy fix)
    python tests/same_numbers.py out.json [builds per class, default 400]
    python tests/same_numbers.py --compare old.json new.json
"""
import hashlib
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("CODE_DIR") or os.path.dirname(HERE))


def fingerprint(r):
    keep = ("value", "main", "spell_dps", "melee_dps", "melee_hit", "burst", "extra_dps", "mana_income", "heal_ps",
            "poison_spell", "poison_melee", "life_steal_ps", "triggered_dps", "spell_goal", "melee_goal", "focus_value",
            "rotation", "heal_rotation", "goal_parts", "mana", "floors_met", "floor_values", "totals", "assigned", "mask")
    out = {k: r.get(k) for k in keep if k in r}
    out["defense"] = {k: v for k, v in (r.get("defense") or {}).items()}
    out["spells"] = {str(b): [x.get("damage"), x.get("cast"), x.get("cost"), x.get("heal"), x.get("mana_gained")]
                     for b, x in (r.get("results") or {}).items()}
    out["stats"] = sorted((str(k), v) for k, v in (r.get("stats") or {}).items() if v)
    return repr(out)


# One reference per system: Windows and Linux can differ in the last digit of a few powers (their maths
# libraries round differently), so each is compared with numbers made on the same kind of system.
GOLDEN = os.path.join(HERE, "golden", "same_numbers_%s.json" % ("windows" if sys.platform == "win32" else "linux"))


def main():
    if sys.argv[1] in ("--check", "--record"):
        if sys.version_info < (3, 12):
            # Python 3.12 made sum() of decimal numbers more precise, so older Pythons differ in the last digits.
            print("SKIP  same numbers as the reference   (needs Python 3.12 or newer; the reference was made with it)")
            return
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"same_numbers_{os.getpid()}.json")
        sys.argv = [sys.argv[0], tmp, "400", sys.argv[1]]
        mode = sys.argv[3]
        score_all(tmp, 400)
        if mode == "--record":
            os.replace(tmp, GOLDEN)
            print(f"Reference saved to {os.path.relpath(GOLDEN, os.path.dirname(HERE))}.")
            return
        if not os.path.exists(GOLDEN):
            print(f"SKIP  same numbers as the reference   (no reference for this system yet: {os.path.basename(GOLDEN)})")
            os.remove(tmp)
            return
        a, b = json.load(open(GOLDEN)), json.load(open(tmp))
        os.remove(tmp)
        diff = [k for k in a if a[k] != b.get(k)]
        ok = not diff and len(a) == len(b)
        print(f"{'PASS' if ok else 'FAIL'}  same numbers as the reference   ({len(b)} random builds, every class and goal"
              + (f"; {len(diff)} different, e.g. {diff[:3]})" if diff else ")"))
        sys.exit(0 if ok else 1)
    if sys.argv[1] == "--print":                 # the numbers as one JSON line (to copy a reference off a computer)
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"same_numbers_{os.getpid()}.json")
        score_all(tmp, 400)
        print("SAME_NUMBERS_JSON " + open(tmp).read())
        os.remove(tmp)
        return
    if sys.argv[1] == "--compare":
        a, b = (json.load(open(p)) for p in sys.argv[2:4])
        diff = [k for k in a if a[k] != b.get(k)]
        print(f"{len(a)} builds compared, {len(diff)} different" + (f": {diff[:5]}" if diff else " (identical)"))
        sys.exit(1 if diff else 0)
    score_all(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 400)


def score_all(out_path, per_class):
    import wynn_helper as wh
    d = wh.load_game_data(offline=True)
    rng = random.Random(12345)
    goals = ["spells", "melee", "burst", "survival", "balanced", "healing"]
    results = {}
    for cls in wh.CLASSES:
        tree = wh.Tree(d, cls)
        nodes = tree.nodes
        for n in range(per_class):
            lvl = rng.choice([20, 45, 70, 90, 106])
            goal = rng.choice(goals)
            kw = {}
            if rng.random() < 0.2:
                kw["floors"] = {"hp": rng.choice([2000, 8000, 20000]), "spd": 0}
            if rng.random() < 0.2:
                kw["enemy"] = {rng.randrange(6): rng.choice([1000, 3000, 6000])}
            if rng.random() < 0.2:
                kw["goal2"], kw["goal2_weight"] = rng.choice(["survival", "spells", "melee"]), rng.choice([0.2, 0.5])
            if rng.random() < 0.15:
                kw["buffs"] = rng.sample(list(wh.BUFFS), 2)
            toggles = sorted({e["toggle"] for nd in nodes for e in nd.raw.get("effects", []) if e.get("toggle")})
            if toggles and rng.random() < 0.3:
                kw["toggle_share"] = {rng.choice(toggles): rng.choice([0.25, 0.5])}
            if rng.random() < 0.3:
                kw["auto_masks"] = True
            build = wh.Build(d, tree, lvl, goal, **kw)
            owned = set(tree.roots)
            for _ in range(rng.randrange(5, 60)):                  # a random tree: grow from what's owned
                cand = [nd for nd in nodes if nd.id not in owned and (not nd.parents or any(p in owned for p in nd.parents))]
                if not cand:
                    break
                owned.add(rng.choice(cand).id)
            armor = [None] * 8
            for sl in range(8):
                if rng.random() < 0.8:
                    pool = [it for it in d.items.values() if it.type == wh.SLOT_TYPES[sl] and it.level <= lvl]
                    if pool:
                        it = rng.choice(pool)
                        if sl < 4 and it.slots and rng.random() < 0.4:
                            it = wh.armor_with_powders(it, [wh.Powder(rng.randrange(5), rng.randint(1, 6))] * it.slots)
                        armor[sl] = it
            wpool = [it for it in d.items.values() if it.type == wh.CLASS_WEAPON[cls] and it.level <= lvl]
            wpn = rng.choice(wpool)
            if wpn.slots and rng.random() < 0.6:
                wpn = wh.weapon_with_powders(wpn, [wh.Powder(rng.randrange(5), rng.randint(1, 6))] * wpn.slots)
            extra = [rng.randrange(0, 30) for _ in range(5)]
            fast = rng.random() < 0.3
            key = f"{cls}-{n}"
            try:
                r = build.evaluate(frozenset(owned), armor, wpn, extra, fast=fast)
                results[key] = hashlib.sha1(fingerprint(r).encode()).hexdigest()
            except Exception as exc:
                results[key] = f"error {type(exc).__name__}: {exc}"
    with open(out_path, "w") as fh:
        json.dump(results, fh)
    print(f"{len(results)} builds scored -> {out_path}")


if __name__ == "__main__":
    main()
