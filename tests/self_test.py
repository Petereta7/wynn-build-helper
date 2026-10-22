#!/usr/bin/env python3
"""
Quick self-test (Python only, no internet).

    python3 tests/self_test.py

Re-runs the helper on inputs whose results were confirmed identical to Wynnbuilder's own code
(tests/golden, made by tests/crossval/run_all.py), plus a few known builds and a tree check for
every class. Every line should say PASS.
"""
import json
import os
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "golden")
sys.path.insert(0, ROOT)
import wynn_helper as wh  # noqa: E402

results = []


def report(name, passed, total, note=""):
    ok = passed == total
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name:<44} {passed}/{total}" + (f"  {note}" if note and not ok else ""))


def golden(fname):
    path = os.path.join(GOLDEN, fname)
    if not os.path.exists(path):
        print(f"SKIP  {fname} (not found)")
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def near(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def main():
    t0 = time.time()
    ver = (golden("version.json") or {}).get("wynnbuilder_data")
    d = wh.load_game_data(offline=True, version=ver)
    if ver and d.version != ver:
        print(f"NOTE  reference results are for game data {ver}, but only {d.version} is saved; "
              "re-run tests/crossval/run_all.py --refresh-golden after a patch.")
    enc = d.enc

    # ---- spell damage
    cases = golden("cases.json")
    if cases:
        ok = 0
        for c in cases:
            w = SimpleNamespace(damage=[tuple(x) for x in c["dmg"]], atk_spd=c["atk"])
            n, cr = wh.spell_part_damage(dict(c["stats"]), w, c["conv"], c["use_spell"], c["ignore_speed"],
                                         c["ignore_str"], c["dm"], part_filter=c["pf"], ignored_mults=tuple(c["ign"]))
            ok += near(n, c["py"][0]) and near(cr, c["py"][1])
        report("spell damage", ok, len(cases))

    # ---- skill points (incl. negative bonuses and crafted pieces)
    cases = golden("sp_cases.json")
    if cases:
        ok = 0
        for c in cases:
            armor = [SimpleNamespace(reqs=a["reqs"], bonus=a["bonus"], crafted=a.get("crafted", False)) for a in c["armor"]]
            weapon = SimpleNamespace(reqs=c["weapon"]["reqs"], bonus=c["weapon"]["bonus"], crafted=False)
            assigned, final, _ = wh.solve_skill_points(armor, weapon)
            ok += sum(assigned) == c["py_total"] and final == c["py_final"]
        report("skill points", ok, len(cases))

    # ---- skill points in Wynncraft's equip order (boots first). A friend's level 50 build where helmet-first
    # gave 97 points; Wynnbuilder's page (and the game) need 99: Agility 27, not 25.
    gear = [d.find_item(n)[0] for n in ("Ajax", "Giant's Bracer", "Inclemency", "Heartbreak", "Horseshoe",
                                        "Horseshoe", "Buster Bracer", "Desert Wretch")]
    if all(gear):
        wpn = wh.weapon_with_powders(d.find_item("Alarm")[0], [wh.Powder(0, 5)])
        got = [wh.solve_skill_points(gear, wpn)[0], wh._solve_skill_points_reference(gear, wpn)[0],
               wh.solve_skill_points(list(reversed(gear)), wpn)[0]]
        ok = sum(g == [20, 52, 0, 0, 27] for g in got) + (not wh._wearable(gear, wpn, 50)) + wh._wearable(gear, wpn, 51)
        report("skill points in the game's equip order", ok, 5, f"got {got}")

    # ---- weapon powders
    cases = golden("powder_cases.json")
    if cases:
        by_raw = {it.raw["name"]: it for it in d.items.values()}
        ok = 0
        for c in cases:
            it = by_raw.get(c["name"])
            if it is None:
                continue
            pw = [wh.Powder(p // 7, p % 7 + 1) for p in c["powders"]]
            got = wh.weapon_with_powders(it, pw).damage
            ok += all(near(got[i][j], c["py"][i][j]) for i in range(6) for j in range(2))
        report("weapon powders", ok, len(cases))

    # ---- defenses / effective HP
    cases = golden("ehp_cases.json")
    if cases:
        ok = 0
        for c in cases:
            r = wh.defense_stats(c["stats"], SimpleNamespace(type=c["wtype"]), c["dm"])
            exp = c["py"]
            ok += (near(r["hp"], exp[0]) and near(r["ehp_agi"], exp[1]) and near(r["ehp"], exp[2])
                   and near(r["hpr"], exp[3]) and all(near(a, b) for a, b in zip(r["elem_def"], exp[4])))
        report("defenses / effective HP", ok, len(cases))

    # ---- crafted items
    cases = golden("craft_cases.json")
    if cases:
        rec_by_name = {r["name"]: int(r["id"]) for r in d.recipes.values()}
        ok = 0
        for c in cases:
            try:
                it = wh.make_crafted(d, rec_by_name[c["recipe"]], c["mats"], c["ings"], c["atk"])
            except Exception:
                continue
            good = it.reqs == c["reqs"] and it.bonus == c["bonus"] and it.slots == c["slots"]
            if "dura" in c:                    # durability, and whether the craft works (Wynnbuilder's numbers)
                good = good and it.durability == c["dura"] and it.missing_durability == c["missing"]
            for key, mine in (("min", it.min_stats), ("max", it.max_stats)):
                a = {k: v for k, v in mine.items() if v}
                b = {k: v for k, v in c[key].items() if v and k not in wh.SKILLS}
                good = good and a.keys() == b.keys() and all(near(a[k], b[k]) for k in a)
            if "damage" in c:
                pw = [wh.Powder(p // 7, p % 7 + 1) for p in c["powders"]]
                dmg = wh.weapon_with_powders(it, pw).damage
                good = good and all(near(dmg[i][j], c["damage"][i][j]) for i in range(6) for j in range(2))
            bits = wh.Bits()
            wh.encode_craft_bits(bits, it)
            ok += good and bits.to_b64() == c["hash"]
        report("crafted items", ok, len(cases))

    # ---- custom items (encoded by Wynnbuilder itself)
    cases = golden("custom_cases.json")
    if cases:
        ok = 0
        for c in cases:
            s = c["spec"]
            it = wh.custom_from_code("CI-" + c["hash"])
            good = (it.name == c["exp"]["name"] and it.type == s["type"] and it.level == s["lvl"]
                    and it.reqs == s["reqs"] and it.bonus == s["sp"])
            for key, mine in (("min", it.min_stats), ("max", it.max_stats)):
                a = {k: v for k, v in mine.items() if k in wh.ROLLED_IDS and v}
                good = good and a == {k: float(v) for k, v in c["exp"][key].items()}
            ok += good
        report("custom items", ok, len(cases))

    # ---- links: encode the same build Wynnbuilder encoded, and decode it back
    cases = golden("link_cases.json")
    if cases:
        by_id = {int(it.raw["id"]): it for it in d.items.values()}
        tome_by_id = {int(t.raw["id"]): t for t in d.tomes}
        ok = 0
        for c in cases:
            tree = wh.Tree(d, c["cls"])
            armor = []
            for i, iid in enumerate(c["equipment"][:8]):
                it = by_id.get(iid) if iid is not None else None
                if it is not None and i < 4 and c["powders"][i]:
                    it = wh.armor_with_powders(it, [wh.Powder(p // 7, p % 7 + 1) for p in c["powders"][i]])
                armor.append(it)
            weapon = wh.weapon_with_powders(by_id[c["equipment"][8]], [wh.Powder(p // 7, p % 7 + 1) for p in c["powders"][4]])
            tomes = [tome_by_id[t] for t in c["tomes"] if t is not None]
            pool = {int(a["id"]): a for a in d.aspects.get(wh.CLASS_TITLE[c["cls"]], [])}
            aspects = [(pool[a], t) for a, t in c["aspects"]]
            link = wh.encode_build_link(d, tree, enc, 35, c["level"], armor, weapon, tomes, aspects, c["owned"],
                                        c["auto"], c["final"])
            dec = wh.decode_build_link(d, enc, c["link"])
            ok += link == c["link"] and sorted(dec["tree"]) == sorted(c["owned"]) and dec["level"] == c["level"]
        report("Wynnbuilder links", ok, len(cases))

    cases = golden("link2_cases.json")
    if cases:
        ok = 0
        for c in cases:
            dec = wh.decode_build_link(d, enc, c["link"])
            tree = wh.Tree(d, c["cls"])
            items = dec["armor"] + [dec["weapon"]]
            armor = []
            for i, it in enumerate(dec["armor"]):
                if it is not None and i in dec["powders"]:
                    it = wh.armor_with_powders(it, dec["powders"][i])
                armor.append(it)
            weapon = wh.weapon_with_powders(dec["weapon"], dec["powders"].get(8, []))
            link = wh.encode_build_link(d, tree, enc, 35, c["level"], armor, weapon, [], [], {c["root"]},
                                        c["auto"], c["auto"])
            kinds_ok = all((e.get("id") is None and it is None) or
                           ("craft" in e and it is not None and it.crafted) or
                           ("custom" in e and it is not None and it.custom) or
                           (e.get("id") is not None and it is not None and int(it.raw["id"]) == e["id"])
                           for e, it in zip(c["equipment"], items))
            ok += link == c["link"] and kinds_ok and not dec["crafted_slots"]
        report("links with crafted/custom items", ok, len(cases))

    # ---- known numbers
    pw = wh.poison_dps({"poison": 1000}, SimpleNamespace(atk_spd="NORMAL"))
    report("poison (Wynnbuilder formula)", int(pw == (2000.0, 6201.0)), 1, f"got {pw}")
    gear = [d.find_item(n)[0] for n in ("Ridge", "Phosphene", "Adventurer's Pants", "Morph-Gold")]
    armor = [gear[0], gear[1], gear[2], gear[3], None, None, None, None]
    assigned, _, _ = wh.solve_skill_points(armor, d.find_item("Haqherphix")[0])
    report("known build: skill points", int(assigned == [27, 27, 7, 7, 7]), 1, f"got {assigned}")

    # ---- tree search gives a legal tree for every class
    ok = total = 0
    for cls in wh.CLASSES:
        tree = wh.Tree(d, cls)
        for level, goal in ((35, "spells"), (106, "balanced")):
            build = wh.Build(d, tree, level, goal)
            weapon = wh.default_weapon(d, cls, level)
            owned, _ = wh.optimize_tree(build, [None] * 8, weapon, wh.ability_points(level), beam_width=8)
            total += 1
            ok += tree.validate(owned, level)["ok"]
    report("legal tree for every class", ok, total)

    print()
    print(f"{sum(results)}/{len(results)} checks passed in {time.time() - t0:.0f}s.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
