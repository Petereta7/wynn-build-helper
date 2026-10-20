"""
Crafted item checks:

  1. The craft designer finds the best grid: on small cases (one per class, different slots, goals and a weapon),
     every possible 6-ingredient grid is tried, and the designer must find a grid exactly as good.
  2. Every craft it designs can be made in game, checked here from the raw game data (not with the code that
     makes crafts): ingredients usable by the recipe's profession, none above the recipe's level range, and
     durability left over (1 or more).
  3. Every designed craft's code (what Wynnbuilder's crafter opens) reads back as the same item.

    python3 tests/craft_check.py

Every line should say PASS.
"""
import itertools
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wynn_helper as wh      # noqa: E402
import wynn_craft as wc       # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<60} {detail}", flush=True)


def wearable_weapon(d, build, cls, lvl):
    w = wh.default_weapon(d, cls, lvl)
    if wh._wearable([None] * 8, w, lvl):
        return w
    ok = [it for it in d.items.values() if it.type == wh.CLASS_WEAPON[cls] and it.level <= lvl
          and wh._wearable([None] * 8, it, lvl)]
    return max(ok, key=lambda it: (it.level, sum(x[1] for x in it.damage)))


def setup(d, cls, lvl, goal, slot):
    tree = wh.Tree(d, cls)
    build = wh.Build(d, tree, lvl, goal)
    own = frozenset(tree.roots)
    wpn = wearable_weapon(d, build, cls, lvl)
    a = [None] * 8
    if slot == "weapon":
        def score(it):
            if not wh._wearable(a, it, lvl):
                return None
            return build.evaluate(own, a, it, wh._fit_extra([0] * 5, a, it, lvl), fast=True)["value"]
        kind = wh.CLASS_WEAPON[cls]
    else:
        def score(it):
            a2 = list(a)
            a2[slot] = it
            if not wh._wearable(a2, wpn, lvl):
                return None
            return build.evaluate(own, a2, wpn, wh._fit_extra([0] * 5, a2, wpn, lvl), fast=True)["value"]
        kind = wh.SLOT_TYPES[slot]
    return score, wc.recipe_for(d, kind, lvl)


def made_in_game(d, it):
    """Problems with a craft, worked out from the raw data only."""
    _k, rid, mats, ids, _atk = it.link_code
    rec = d.recipes[rid]
    out = []
    for i in ids:
        ing = d.ingredients[i]
        if i == 4000:
            continue
        skills = ing.get("skills") or (wh.CRAFT_SKILLS_GEAR if ing.get("isPowder") else ())
        if rec["skill"] not in skills:
            out.append(f"{ing['name']}: wrong profession")
        if (ing.get("lvl") or 0) > rec["lvl"]["maximum"]:
            out.append(f"{ing['name']}: level {ing.get('lvl')} > {rec['lvl']['maximum']}")
    amounts = [m["amount"] for m in rec["materials"]]
    mult = (wh.CRAFT_MAT_MULT[mats[0]] * amounts[0] + wh.CRAFT_MAT_MULT[mats[1]] * amounts[1]) / sum(amounts)
    low = math.floor(rec["durability"]["minimum"] * mult + 0.5) + sum(
        (d.ingredients[i].get("itemIDs") or {}).get("dura", 0) for i in ids)
    if low < 1:
        out.append(f"durability {low}")
    return out


def main():
    d = wh.load_game_data(offline=True)
    wc.remember_names(d)
    t0 = time.time()

    # 1. designer = best of every grid (small pools: 3 strongest ingredients, 2 that boost others, empty slot)
    cases = [("warrior", 80, "melee", 0), ("mage", 70, "spells", 4), ("archer", 90, "spells", 2),
             ("assassin", 60, "balanced", 7), ("shaman", 100, "survival", 1), ("mage", 106, "spells", "weapon"),
             ("warrior", 45, "survival", 3)]
    designed = []
    for cls, lvl, goal, slot in cases:
        score, rec = setup(d, cls, lvl, goal, slot)
        full = wc.usable_ingredients(d, rec)
        d0 = wc.Designer(d, rec, score)
        solo = sorted(((d0.value([g["id"]] + [4000] * 5, "SLOW"), g) for g in full if g["id"] != 4000),
                      key=lambda x: -x[0])
        top3 = [g for _v, g in solo[:3]]
        boosters = [g for g in sorted(full, key=lambda g: -wc._boost(g)) if wc._boost(g) > 0 and g not in top3][:2]
        pool = top3 + boosters + [d.ingredients[4000]]
        des = wc.Designer(d, rec, score, pool=pool)
        v, it = des.design()
        ids = [g["id"] for g in pool]
        best = max(itertools.product(ids, repeat=6), key=lambda g: des.value(list(g), "SLOW"))
        bv = des.value(list(best), "SLOW")
        same = v is not None and abs(v - bv) <= 1e-9 * max(1, abs(bv))
        label = "weapon" if slot == "weapon" else wh.SLOT_TYPES[slot]
        check(f"{wh.CLASS_TITLE[cls]} L{lvl} {goal} {label}: designer = best grid", same,
              f"({v:.2f} vs {bv:.2f})" if v is not None else "(nothing found)")
        # the full designer, every ingredient, for the rules checks below
        v2, it2 = wc.Designer(d, rec, score).design()
        if it2 is not None:
            designed.append((f"{cls} L{lvl} {label}", it2))
            check(f"{wh.CLASS_TITLE[cls]} L{lvl} {goal} {label}: all ingredients >= small pool", v2 >= v - 1e-9,
                  f"({v2:.2f} vs {v:.2f})")

    # 2 + 3. every designed craft can be made, and its code reads back as the same item
    for cls in wh.CLASSES:
        for lvl, goal in ((30, "spells"), (75, "melee"), (106, "balanced")):
            for slot in (0, 3, 4, 7, "weapon"):
                score, rec = setup(d, cls, lvl, goal, slot)
                v, it = wc.Designer(d, rec, score, atk_spds=tuple(wh.CRAFT_ATK_SPDS) if slot == "weapon" else ("SLOW",),
                                    kicks=2).design()
                if it is not None:
                    designed.append((f"{cls} L{lvl} {slot}", it))
    bad = []
    for name, it in designed:
        p = made_in_game(d, it)
        if p or it.craft_problems or it.missing_durability is not None:
            bad.append(f"{name}: {p or it.craft_problems}")
    check("every designed craft can be made in game", not bad and designed,
          f"({len(designed)} crafts)" + (" " + "; ".join(bad[:3]) if bad else ""))
    bad = []
    for name, it in designed:
        bits = wh.Bits()
        wh.encode_craft_bits(bits, it)
        back = wh.crafted_from_code(d, "CR-" + bits.to_b64())
        if back.max_stats != it.max_stats or back.reqs != it.reqs or back.bonus != it.bonus or back.damage != it.damage:
            bad.append(name)
    check("every designed craft's code reads back the same", not bad, ", ".join(bad[:3]))

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed in {time.time() - t0:.0f}s.")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
