"""
Goal mix and mana check:

  1. A main goal mixed with a second one scores exactly main^(1-w) x second^w, where both come from the same
     build scored for each goal alone (every class).
  2. Second goals that can't be mixed (the same goal twice, "ability", weight 0) fall back to the main goal.
  3. The search's shortcuts never drop what the second goal needs: fast scoring = full scoring, and the item
     filter keeps every item the second goal could use.
  4. Mana check: the numbers match the spell damage they describe (sustained casts x damage = the rotation's
     damage per second; full-speed time = mana bar / mana lost per second; mana bar = 100 + bonus + Int %).
  5. Armor powders in the search: added where they help, never for a damage-only goal, the element of an enemy
     hit chosen against it.

    python3 tests/goal_check.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wynn_helper as wh      # noqa: E402
import wynn_search as ws      # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<64} {detail}", flush=True)


def setup(d, cls, level):
    tree = wh.Tree(d, cls)
    owned = frozenset(n.id for n in tree.nodes if n.row <= 10)
    if not tree.validate(owned, level)["ok"]:
        owned = frozenset(tree.roots)
    wpn = wh.default_weapon(d, cls, level)
    return tree, owned, wpn


def main():
    d = wh.load_game_data(offline=True)

    # 1. the mix is exact, every class
    bad = []
    for cls in wh.CLASSES:
        tree, owned, wpn = setup(d, cls, 80)
        for g1, g2, w in (("spells", "survival", 0.3), ("melee", "survival", 0.5), ("survival", "spells", 0.2)):
            e = [0] * 5
            mix = wh.Build(d, tree, 80, g1, goal2=g2, goal2_weight=w).evaluate(owned, [None] * 8, wpn, e)
            a = wh.Build(d, tree, 80, g1).evaluate(owned, [None] * 8, wpn, e)["main"]
            b = wh.Build(d, tree, 80, g2).evaluate(owned, [None] * 8, wpn, e)["main"]
            want = max(a, 1e-9) ** (1 - w) * max(b, 1e-9) ** w
            if abs(mix["main"] - want) > 1e-9 * max(1, want) or mix["goal_parts"] != {g1: a, g2: b}:
                bad.append(f"{cls} {g1}+{g2}: {mix['main']} vs {want}")
    check("goal mix = main^(1-w) x second^w, every class", not bad, "; ".join(bad[:2]))

    # 2. what can't be mixed
    ok = (wh.clean_goal2("spells", "spells", 30) == (None, 0.0) and wh.clean_goal2("spells", "ability", 30) == (None, 0.0)
          and wh.clean_goal2("spells", "survival", 0) == (None, 0.0) and wh.clean_goal2("spells", "survival", 30) == ("survival", 0.3)
          and wh.clean_goal2("melee", "survival", 0.95) == ("survival", 0.9))
    check("unusable second goals fall back to the main goal", ok)

    # 3. shortcuts keep what the second goal needs
    bad = []
    for cls in wh.CLASSES:
        tree, owned, wpn = setup(d, cls, 80)
        for g1, g2 in (("melee", "survival"), ("melee", "spells"), ("melee", "healing")):
            bld = wh.Build(d, tree, 80, g1, goal2=g2, goal2_weight=0.3)
            fast = bld.evaluate(owned, [None] * 8, wpn, [0] * 5, fast=True)["main"]
            full = bld.evaluate(owned, [None] * 8, wpn, [0] * 5)["main"]
            if abs(fast - full) > 1e-9 * max(1, full):
                bad.append(f"{cls} {g1}+{g2}: fast {fast:.2f} vs full {full:.2f}")
    check("fast scoring = full scoring with a second goal", not bad, "; ".join(bad[:2]))
    tree = wh.Tree(d, "warrior")
    alone, _ = ws.safe_filter(wh.Build(d, tree, 80, "melee"))
    surv, _ = ws.safe_filter(wh.Build(d, tree, 80, "survival"))
    mixed, _ = ws.safe_filter(wh.Build(d, tree, 80, "melee", goal2="survival", goal2_weight=0.3))
    lost = [it.name for sl in range(8) for it in surv[sl] if it not in mixed[sl]]
    ok = all(len(mixed[sl]) >= len(alone[sl]) for sl in range(8)) and not lost
    check("item filter keeps what either goal can use", ok,
          f"(helmets: melee {len(alone[0])}, survival {len(surv[0])}, mix {len(mixed[0])})" + (f" lost {lost[:3]}" if lost else ""))

    # 4. mana check agrees with the spell damage
    bad, seen = [], 0
    for cls in wh.CLASSES:
        for lvl in (30, 80, 106):
            tree = wh.Tree(d, cls)
            build = wh.Build(d, tree, lvl, "spells")
            wpn = wh.default_weapon(d, cls, lvl)
            owned, _ = wh.optimize_tree(build, [None] * 8, wpn, wh.ability_points(lvl), beam_width=6)
            r = build.evaluate(owned, [None] * 8, wpn, [0] * 5)
            m = r.get("mana")
            if not r["rotation"]:
                continue
            seen += 1
            rs = [r["results"][b] for b in r["rotation"]]
            dmg = sum(x["cast"] for x in rs) / len(rs)
            bar = 100 + r["stats"].get("maxMana", 0) + math.floor(wh.skill_pct(r["stats"].get("int", 0)) * 100)
            probs = []
            if abs(m["used"] - 3 * sum(x["cost"] for x in rs) / len(rs)) > 1e-9:
                probs.append("used")
            if abs(m["sustained_rate"] * dmg - r["spell_dps"]) > 1e-6 * max(1, r["spell_dps"]):
                probs.append(f"sustained {m['sustained_rate'] * dmg:.1f} vs {r['spell_dps']:.1f}")
            if m["bar"] != max(1, bar):
                probs.append(f"bar {m['bar']} vs {bar}")
            if m["seconds"] is not None and abs(m["seconds"] - m["bar"] / -m["net"]) > 1e-9:
                probs.append("seconds")
            if m["seconds"] is None and m["net"] < -1e-9:
                probs.append("says never runs out but loses mana")
            if probs:
                bad.append(f"{cls} L{lvl}: {', '.join(probs)}")
    check("mana check matches the spell damage it describes", not bad and seen >= 10,
          f"({seen} builds)" + (" " + "; ".join(bad[:2]) if bad else ""))

    # 4b. spells + main attacks together ("mixed"): never below spells alone or main attacks alone, the parts add up
    # (casting r spells a second leaves 1 - r/3 of the time for main attacks), every class
    bad, seen = [], 0
    for cls in wh.CLASSES:
        for lvl in (40, 80, 106):
            tree = wh.Tree(d, cls)
            build = wh.Build(d, tree, lvl, "mixed")
            wpn = wh.default_weapon(d, cls, lvl)
            owned, _ = wh.optimize_tree(build, [None] * 8, wpn, wh.ability_points(lvl), beam_width=6)
            r = build.evaluate(owned, [None] * 8, wpn, [0] * 5)
            gv, mi = r["goal_values"], r["mixed"]
            if r["main"] < max(gv["spells"], gv["melee"]) - 1e-6 * max(1, r["main"]):
                bad.append(f"{cls} L{lvl}: mixed {r['main']:.1f} < spells {gv['spells']:.1f} / melee {gv['melee']:.1f}")
            if mi["rotation"]:
                seen += 1
                if abs(mi["melee_share"] - max(0.0, 1 - mi["rate"] * 3 / wh.CLICKS_PER_SECOND)) > 1e-12 \
                        or abs(mi["melee_part"] - r["melee_dps"] * mi["melee_share"]) > 1e-6 * max(1, mi["melee_part"]):
                    bad.append(f"{cls} L{lvl}: parts don't add up")
    check("spells + main attacks together: never below either alone", not bad and seen >= 5,
          f"({seen} with spells)" + (" " + "; ".join(bad[:2]) if bad else ""))

    # 5. armor powders in the search: added where they help (survival), never for a damage-only goal, and the
    #    element of an enemy hit is the one chosen against it
    lvl = 80
    pieces = []
    for sl in range(4):
        pool = [it for it in d.items.values() if it.type == wh.SLOT_TYPES[sl] and it.slots >= 2 and 70 <= it.level <= lvl
                and not any(it.reqs)]
        pieces.append(max(pool, key=lambda it: it.slots) if pool else None)
    probs = []
    for cls in wh.CLASSES:
        tree, owned, wpn = setup(d, cls, lvl)
        a = pieces + [None] * 4
        e = [0] * 5
        surv = wh.Build(d, tree, lvl, "survival")
        a2 = ws._armor_powders(surv, owned, a, wpn, e)
        if not any(getattr(x, "powders", None) for x in a2[:4]):
            probs.append(f"{cls}: no powders for survival")
        elif surv.evaluate(owned, a2, wpn, e)["value"] <= surv.evaluate(owned, a, wpn, e)["value"]:
            probs.append(f"{cls}: powders didn't help")
        dmg = wh.Build(d, tree, lvl, "melee")
        if any(getattr(x, "powders", None) for x in ws._armor_powders(dmg, owned, a, wpn, e)[:4]):
            probs.append(f"{cls}: powders on a damage-only build")
        fire = wh.Build(d, tree, lvl, "survival", enemy={4: 3000})
        a3 = ws._armor_powders(fire, owned, a, wpn, e)
        els = {p.elem for x in a3[:4] if getattr(x, "powders", None) for p in x.powders}
        if els != {3}:
            probs.append(f"{cls}: against fire chose {sorted(els)}")
    check("armor powders: where they help, the right element", not probs, "; ".join(probs[:3]))

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed.")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
