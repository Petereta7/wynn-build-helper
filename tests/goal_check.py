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
        for g1, g2, w in (("spells", "survival", 0.3), ("melee", "survival", 0.5), ("survival", "spells", 0.2),
                          ("survival", "mana", 0.3)):
            e = [0] * 5
            mix = wh.Build(d, tree, 80, g1, goal2=g2, goal2_weight=w).evaluate(owned, [None] * 8, wpn, e)
            a = wh.Build(d, tree, 80, g1).evaluate(owned, [None] * 8, wpn, e)["main"]
            rb = wh.Build(d, tree, 80, g2).evaluate(owned, [None] * 8, wpn, e)
            b = rb["main"]
            if rb["ls_share"] != mix["ls_share"]:
                # life steal counts by the main goal's way of fighting (spells: none), so the survival part of a
                # spells mix can differ from survival alone; the mix of the parts must still be exact
                b = mix["goal_parts"][g2]
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
            extra = wh.repeat_extra_mana(r["rotation"])
            if abs(m["used"] - 3 * (sum(x["cost"] for x in rs) + extra) / len(rs) / wh.mana_refund_mult(r["stats"])) > 1e-9:
                probs.append("used")
            sus = wh.rotation_dps(rs, m["sustained_rate"])
            if abs(sus - r["spell_dps"]) > 1e-6 * max(1, r["spell_dps"]):
                probs.append(f"sustained {sus:.1f} vs {r['spell_dps']:.1f}")
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

    # 4c. mana steal only works on main attack hits (wiki): never counted for a spells-only rotation; in the mixed
    # goal only for the share of time spent main-attacking. Mage's Paradox refund lowers spell costs as in
    # Wynnbuilder's mana calculator (cost / 1.34).
    bad, seen = [], 0
    for cls in wh.CLASSES:
        tree = wh.Tree(d, cls)
        lvl = 80
        wpn = wh.default_weapon(d, cls, lvl)
        steal = [it for it in d.items.values() if it.type == "helmet" and it.level <= lvl
                 and (it.raw.get("ms") or 0) >= 5 and wh._wearable([it] + [None] * 7, wpn, lvl)][:1]
        if not steal:
            continue
        arm = steal + [None] * 7
        for goal in ("spells", "mixed"):
            build = wh.Build(d, tree, lvl, goal)
            owned, _ = wh.optimize_tree(build, arm, wpn, wh.ability_points(lvl), beam_width=6)
            r = build.evaluate(owned, arm, wpn, [0] * 5)
            st = r["stats"]
            if st.get("ms", 0) <= 0:
                continue
            seen += 1
            if abs(r["mana_income"] - (wh.BASE_MANA_PER_SECOND + st.get("mr", 0) / 5)) > 1e-9:
                bad.append(f"{cls} {goal}: income {r['mana_income']:.2f} counts mana steal")
            mi = r.get("mixed") or {}
            if goal == "mixed" and mi.get("rotation"):
                rs = [r["results"][b] for b in mi["rotation"]]
                mult = wh.mana_refund_mult(st)
                spend = mi["rate"] * (sum(x["cost"] / mult - x["mana_gained"] for x in rs)
                                      + wh.repeat_extra_mana(mi["rotation"]) / mult) / len(rs)
                gain = r["mana_income"] + st["ms"] / 3 * mi["melee_share"]
                if spend > gain + 1e-6:
                    bad.append(f"{cls} mixed: spends {spend:.2f}/s, gets {gain:.2f}/s")
    if abs(wh.mana_refund_mult({"manaMult.Paradox": 34, "mr": 5}) - 1.34) > 1e-12 or wh.mana_refund_mult({}) != 1.0:
        bad.append("Paradox refund isn't cost / 1.34")
    check("mana steal only from main attacks; Paradox refund", not bad and seen >= 3,
          f"({seen} builds with mana steal)" + (" " + "; ".join(bad[:2]) if bad else ""))

    # 4c2. life steal also only works on main attack hits: none for spell-only builds (survival heal and the
    # never-lose-health minimum), all for main attacks, the main-attack share for most damage in any style
    bad, seen = [], 0
    for cls in wh.CLASSES:
        tree = wh.Tree(d, cls)
        lvl = 80
        wpn = wh.default_weapon(d, cls, lvl)
        steal = [it for it in d.items.values() if it.type == "helmet" and it.level <= lvl
                 and (it.raw.get("ls") or 0) >= 50 and wh._wearable([it] + [None] * 7, wpn, lvl)][:1]
        if not steal:
            continue
        arm = steal + [None] * 7
        for goal, want in (("spells", 0.0), ("melee", 1.0), ("mixed", None)):
            build = wh.Build(d, tree, lvl, goal, floors={"net": -1e9})
            owned, _ = wh.optimize_tree(build, arm, wpn, wh.ability_points(lvl), beam_width=6)
            r = build.evaluate(owned, arm, wpn, [0] * 5)
            share = r["ls_share"]
            if want is not None and share != want:
                bad.append(f"{cls} {goal}: life steal share {share}")
            if goal == "mixed":
                mi = r["mixed"]
                exp = mi["melee_share"] if mi.get("rotation") else 1.0
                if abs(share - exp) > 1e-12:
                    bad.append(f"{cls} mixed: share {share} vs {exp}")
            net = r["defense"].get("hpr", 0) / 4 + r["stats"].get("ls", 0) / 3 * share
            if abs(r["floor_values"]["net"] - net) > 1e-9:
                bad.append(f"{cls} {goal}: net health {r['floor_values']['net']:.2f} vs {net:.2f}")
            seen += 1
    check("life steal only from main attacks", not bad and seen >= 9,
          f"({seen} builds)" + (" " + "; ".join(bad[:2]) if bad else ""))

    # 4c3. Arrow Shield only hits enemies that come close: never part of a damage-per-second rotation (burst keeps it)
    bad, seen = [], 0
    tree = wh.Tree(d, "archer")
    kids = {}
    for n in tree.nodes:
        for p in n.parents:
            kids.setdefault(p, []).append(n.id)

    def path_to(name):                     # the shortest legal path from the start to an ability
        t = next(n.id for n in tree.nodes if n.name == name)
        prev, todo = {r: None for r in tree.roots}, list(tree.roots)
        while todo:
            c = todo.pop(0)
            for k in kids.get(c, []):
                if k not in prev:
                    prev[k] = c
                    todo.append(k)
        out, c = set(), t
        while c is not None:
            out.add(c)
            c = prev.get(c)
        return out
    owned = frozenset(path_to("Arrow Shield"))   # no Guardian Angels (it replaces the shield)
    for lvl in (30, 60, 90):
        wpn = wh.default_weapon(d, "archer", lvl)
        if not tree.validate(owned, lvl)["ok"]:
            continue
        for goal in ("spells", "mixed"):
            build = wh.Build(d, tree, lvl, goal)
            r = build.evaluate(owned, [None] * 8, wpn, [0] * 5)
            res = r["results"]
            if not any(x.get("name") == "Arrow Shield" and x.get("damage", 0) > 0 for x in res.values()):
                continue
            seen += 1
            rot = r["rotation"] if goal == "spells" else (r["mixed"] or {}).get("rotation")
            if rot:
                rs = [dict(res[b], cast=0.0) if res[b].get("name") == "Arrow Shield" else res[b] for b in rot]
                rate = r["mana"]["sustained_rate"] if goal == "spells" else r["mixed"]["rate"]
                want = wh.rotation_dps(rs, rate)
                got = r["spell_dps"] if goal == "spells" else r["mixed"]["spell_part"]
                if abs(got - want) > 1e-6 * max(1, want):
                    bad.append(f"L{lvl} {goal}: rotation {got:.1f} counts Arrow Shield ({want:.1f} without)")
            if r["burst"] < sum(x["damage"] for b, x in res.items() if b in (1, 2, 3, 4) and not x.get("is_dps")) - 1e-6:
                bad.append(f"L{lvl}: burst leaves a spell out")
    check("Arrow Shield (hits only enemies up close) not in damage per second", not bad and seen >= 2,
          f"({seen} builds)" + (" " + "; ".join(bad[:2]) if bad else ""))

    # 4c4. main archetype: the planned tree has more of that archetype's abilities than any other's, every class and
    # every archetype
    bad, seen = [], 0
    for cls in wh.CLASSES:
        tree = wh.Tree(d, cls)
        wpn = wh.default_weapon(d, cls, 60)
        for arch in wh.tree_archetypes(tree):
            b = wh.Build(d, tree, 60, "mixed", archetype=arch)
            own, _ = wh.optimize_tree(b, [None] * 8, wpn, wh.ability_points(60), beam_width=8)
            mine, other = wh.archetype_lead(tree, own, arch)
            seen += 1
            if mine <= other:
                bad.append(f"{cls} {arch}: {mine} vs {other}")
            if wh.match_archetype(tree, arch.lower()[:5]) != arch:
                bad.append(f"{cls} {arch}: name not understood")
            b2 = wh.Build(d, tree, 60, "mixed", avoid_archetypes=[arch])     # and leaving it out completely
            own2, _ = wh.optimize_tree(b2, [None] * 8, wpn, wh.ability_points(60), beam_width=8)
            if wh.avoided_core(tree, own2, (arch,)):
                bad.append(f"{cls} without {arch}: {len(wh.avoided_core(tree, own2, (arch,)))} of its play-style abilities")
    check("main archetype kept most / left-out archetype's play style kept out", not bad and seen == 15,
          f"({seen} class/archetype pairs)" + (" " + "; ".join(bad[:3]) if bad else ""))

    # 4d. a lasting spell (summons, totems, storms: shown as damage per second) never counts above one copy up all the
    # time, however often it's recast (recasting refreshes it)
    rs = [{"cast": 757864.0, "damage": 93563.0, "is_dps": True}, {"cast": 398820.0, "damage": 398820.0, "is_dps": False}]
    v = wh.rotation_dps(rs, 3.0)
    ok = abs(v - (93563.0 + 398820.0 * 1.5)) < 1e-6 and wh.rotation_breaks(rs, 3.0) == [93563.0 * 2 / 757864.0]
    check("lasting spells count at most one copy in a rotation", ok)

    # 4e. casting the same spell again (owner's in-game test, Oct 2026): 2nd in a row the same, 3rd +5, the spell
    # right after a double +5, alternating free; and the rotations found are the best of every order of 2-4 different
    # spells each cast once or twice (an independent brute force with rotation_dps / repeat_extra_mana)
    costs = {"Escape": 15, "Arrow Shield": 25}
    ok = (wh.repeated_cast_costs(["Escape", "Escape", "Arrow Shield", "Arrow Shield", "Arrow Shield"], costs)
          == [15, 15, 30, 25, 30]
          and wh.repeated_cast_costs(["Escape", "Arrow Shield"] * 2, costs) == [15, 25, 15, 25]
          and wh.repeat_extra_mana([1, 1, 2]) == 5 and wh.repeat_extra_mana([1, 2, 1, 2]) == 0
          and wh.repeat_extra_mana([1, 1, 2, 2, 3]) == 10 and wh.repeat_extra_mana([2, 1, 1]) == 5)
    bad, seen, doubled = [], 0, 0
    import itertools
    import random
    rng = random.Random(5)
    for cls in wh.CLASSES:
        tree = wh.Tree(d, cls)
        for _ in range(12):
            lvl = rng.choice([45, 80, 106])
            own = set(tree.roots)
            for _ in range(rng.randrange(15, 50)):
                cand = [nd for nd in tree.nodes if nd.id not in own and (not nd.parents or any(p in own for p in nd.parents))]
                if not cand:
                    break
                own.add(rng.choice(cand).id)
            arm = [rng.choice([it for it in d.items.values() if it.type == wh.SLOT_TYPES[sl] and it.level <= lvl])
                   for sl in range(8)]
            wpn = rng.choice([it for it in d.items.values() if it.type == wh.CLASS_WEAPON[cls] and it.level <= lvl])
            r = wh.Build(d, tree, lvl, "mixed").evaluate(frozenset(own), arm, wpn, [rng.randrange(30) for _ in range(5)])
            res, mm, inc = r["results"], wh.mana_refund_mult(r["stats"]), r["mana_income"]
            ms = r["stats"].get("ms", 0) / 3
            sp = r["special"]["damage"] / r["special"]["cycle"] if r["special"] and r["special"]["damage"] > 0 else 0.0
            rest = r["extra_dps"]                       # summons etc. + weapon special, added to every mixed option
            rot = {b: (dict(x, cast=0.0) if x.get("name") in wh.CONTACT_SPELLS else x) for b, x in res.items()}
            usable = [b for b in res if b in (1, 2, 3, 4)]
            best, best_mix = 0.0, r["melee_dps"] + r["poison_melee"] + sp
            for size in (2, 3, 4):
                for combo in itertools.combinations(usable, size):
                    for runs in itertools.product(range(1, wh.MAX_SPELL_RUN + 1), repeat=size):
                        seq = [b for b, m in zip(combo, runs) for _ in range(m)]
                        rs = [rot[b] for b in seq]
                        if sum(x["cast"] for x in rs) <= 0:
                            continue
                        net = (sum(x["cost"] / mm - x["mana_gained"] for x in rs) + wh.repeat_extra_mana(seq) / mm) / len(seq)
                        best = max(best, wh.rotation_dps(rs, 3.0 if net <= 0 else min(3.0, inc / net)))
                        denom, rmax = net + ms / 3, 3.0
                        if denom > 0:
                            rmax = min(3.0, max(0.0, (inc + ms) / denom))
                        elif rmax * denom > inc + ms:
                            rmax = 0.0
                        for rt in [rmax] + wh.rotation_breaks(rs, rmax):
                            share = max(0.0, 1 - rt / 3)
                            best_mix = max(best_mix, wh.rotation_dps(rs, rt) + r["melee_dps"] * share + rest
                                           + max(r["poison_spell"], r["poison_melee"] if share > 0 else 0.0))
            seen += 1
            doubled += bool(r["rotation"]) and len(set(r["rotation"])) < len(r["rotation"])
            if abs(best - r["spell_dps"]) > 1e-9 * max(1.0, best):
                bad.append(f"{cls} L{lvl}: {r['spell_dps']:.1f} vs every order {best:.1f}")
            if abs(best_mix - r["goal_values"]["mixed"]) > 1e-9 * max(1.0, best_mix):
                bad.append(f"{cls} L{lvl} any style: {r['goal_values']['mixed']:.1f} vs every order {best_mix:.1f}")
    check("casting the same spell again: in-game costs; best of every cast order",
          ok and not bad and (doubled > 0 or wh.MAX_SPELL_RUN < 2),
          f"({seen} builds, {doubled} with a spell cast twice)" + (" " + "; ".join(bad[:2]) if bad else ""))

    # 4c. life steal / mana steal minimums: a bracelet with negative life steal misses "no negative life steal"
    kb = d.find_item("Knucklebones")[0]
    tree, owned, wpn = setup(d, "archer", 106)
    arm = [None] * 6 + [kb, None]
    no_floor = wh.Build(d, tree, 106, "melee").evaluate(owned, arm, wpn, [0] * 5)
    with_floor = wh.Build(d, tree, 106, "melee", floors={"life steal": 0}).evaluate(owned, arm, wpn, [0] * 5)
    empty = wh.Build(d, tree, 106, "melee", floors={"ls": 0, "ms": 0}).evaluate(owned, [None] * 8, wpn, [0] * 5)
    ok = (kb.max_stats.get("ls", 0) < 0 and not with_floor["floors_met"] and with_floor["value"] < no_floor["value"]
          and empty["floors_met"] and wh.parse_floors("life steal=0, mana steal=3") == {"ls": 0.0, "ms": 3.0})
    # never losing health: health regen / 4 + life steal / 3 (wiki: regen every 4 s, life steal per 3 s), so a negative
    # one is fine when the other makes up for it
    net = wh.Build(d, tree, 106, "melee", floors={"net health": 0}).evaluate(owned, arm, wpn, [0] * 5)
    fv = net["floor_values"]
    ok = ok and abs(fv["net"] - (fv["hpr"] / 4 + fv["ls"] / 3)) < 1e-9 and net["floors_met"] == (fv["net"] >= 0) \
        and wh.parse_floors("never lose health=0") == {"net": 0.0}
    check("life steal, mana steal and never-lose-health minimums", ok, f"(Knucklebones life steal {kb.max_stats.get('ls')})")

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
