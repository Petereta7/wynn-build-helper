"""
Build check: runs the whole app (the same path as asking the bot for a build) once for every class, then checks
that what it hands back is something you can really wear, and that it's never worse than before.

For every class it checks:

  * the app finished and gave a report and a results screen
  * every item is in level range, the weapon is the class's own type
  * skill points, worked out from scratch: gear needs + spare points fit the level, no skill over 100
  * the ability tree is legal and fits the ability points for the level
  * any minimums asked for are met
  * "build from my items": only the player's items, and a ring they have one of is worn once
  * crafted pieces (when asked for) can be made in game, and their crafter codes read back the same
  * the build as a share code opens as exactly the same build (items, powders, crafts, tree, skill points)
  * every ability in the tree says why it's there, and a "spare" one really doesn't change the goal's number
  * the score is at least the score recorded in tests/golden/build_baseline.json (builds never get worse)

    python3 tests/build_check.py              (all cases; a few minutes)
    python3 tests/build_check.py mage shaman  (only cases for those classes)
    python3 tests/build_check.py --record     (after an improvement: save the new, higher scores)

--record never lowers a recorded score. Every line should say PASS.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["WYNN_HELPER_NO_POPUP"] = "1"

import wynn_app as app          # noqa: E402
import wynn_helper as wh        # noqa: E402
import wynn_search as ws        # noqa: E402

BASELINE = os.path.join(HERE, "golden", "build_baseline.json")
# The best build anyone has found for each case under today's rules (any search, any starting point). Not a check:
# shows how close the search gets, so search changes can be judged against a known target.
BEST_KNOWN = os.path.join(HERE, "golden", "best_known.json")

# One case per class (each with a different goal), plus one with minimums and one that uses an ability buff.
# Crafted items are off by default (the player asks for them), so the cases that test them ask for them.
CASES = [
    {"name": "warrior L50 melee", "class": "warrior", "level": 50, "goal": "melee", "crafted": True},
    {"name": "mage L40 spells", "class": "mage", "level": 40, "goal": "spells", "crafted": True},
    {"name": "archer L60 melee", "class": "archer", "level": 60, "goal": "melee", "crafted": True},
    {"name": "shaman L60 spells", "class": "shaman", "level": 60, "goal": "spells", "crafted": True},
    {"name": "assassin L50 balanced", "class": "assassin", "level": 50, "goal": "balanced", "crafted": True},
    {"name": "mage L45 survival, min 2500 health", "class": "mage", "level": 45, "goal": "survival",
     "minimums": {"health": 2500}, "crafted": True},
    {"name": "assassin L55 spells, Aerial Ace 50%", "class": "assassin", "level": 55, "goal": "spells",
     "toggles": ["Aerial Ace=50%"], "crafted": True},
    # crafted items are off unless asked for: this one asks for nothing, so it checks the default too
    {"name": "warrior L50 melee, no crafted items", "class": "warrior", "level": 50, "goal": "melee"},
    {"name": "shaman L70 spells 70% + survival 30%", "class": "shaman", "level": 70, "goal": "spells",
     "second_goal": "survival", "second_goal_weight": 30, "crafted": True},
    # "max DPS": the most damage in any style (here main attacks + spells + summons together win)
    {"name": "shaman L60 most damage (any style)", "class": "shaman", "level": 60, "goal": "mixed"},
    # "build from my items": only these items, and Brass Knuckle (the ring this build wears twice when it can)
    # is listed once, so it may only be worn once
    {"name": "archer L60 melee, only my items", "class": "archer", "level": 60, "goal": "melee",
     "only_items": ["Heliophobia", "Call of the Void", "Illuminite", "Nesaak's Shadow", "Balm", "Cage of Bones",
                    "Unrefined Leggings", "Creek", "Nettle Tangle", "Bladerunners", "Tainted Step", "Galena",
                    "Brass Knuckle", "Fingertrap", "Return", "Insulation", "Adventurous", "Bamboo Cuff", "Devotion",
                    "Shiny Coin", "Luminis", "Ballista", "Dorian", "Flawless Light Jungle Bow", "Scarlet Veil",
                    "Heartbreak", "Binding Brace", "Electric Torc", "Soundwave"]},
]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<46} {detail}", flush=True)


def problems_with(case, res):
    """Everything wrong with the build the app returned (empty list = fine)."""
    out = []
    if not res.get("ok"):
        return ["the app didn't finish: " + res.get("text", "")[-300:].strip()]
    if not res.get("summary"):
        out.append("no results screen data")
    best = wh.LAST_RESULT.get("best")
    if not best:
        return out + ["no whole build in the result"]
    lvl, cls = case["level"], case["class"]
    armor, weapon, extra = [a for a in best["armor"] if a is not None], best["weapon"], best["extra"]
    for it in armor + [weapon]:
        if it.level > lvl:
            out.append(f"{it.name} needs level {it.level}")
    if weapon.type != wh.CLASS_WEAPON[cls]:
        out.append(f"weapon {weapon.name} is a {weapon.type}, not a {wh.CLASS_WEAPON[cls]}")
    assigned, _, _ = wh._solve_skill_points_uncached(armor, weapon)
    if sum(assigned) + sum(extra) > wh.skill_points_available(lvl):
        out.append(f"skill points over the limit ({sum(assigned)} for gear + {sum(extra)} spare "
                   f"> {wh.skill_points_available(lvl)})")
    if any(a + e > wh.MAX_ASSIGNED_PER_SKILL for a, e in zip(assigned, extra)) or min(extra) < 0:
        out.append(f"a skill is over {wh.MAX_ASSIGNED_PER_SKILL} or below 0 ({assigned} + {extra})")
    tree = wh.LAST_RESULT.get("tree")
    v = tree.validate(best["owned"], lvl)
    if not v["ok"]:
        out.append("ability tree isn't legal: " + "; ".join(map(str, v.get("errors", [])[:3])))
    elif v.get("ap", 0) > wh.ability_points(lvl):
        out.append(f"ability tree uses {v['ap']} points, only {wh.ability_points(lvl)} at this level")
    for it in armor + [weapon]:                  # crafted pieces: can be made in game, and the code reads back
        if not getattr(it, "crafted", False) or not getattr(it, "link_code", None):
            continue
        if case.get("crafted") is not True:
            out.append(f"crafted {it.type} without asking for crafted items")
        if it.craft_problems or it.missing_durability is not None:
            out.append(f"crafted {it.type} can't be made: {it.craft_problems}")
        back = wh.crafted_from_code(wh.LAST_RESULT["build"].data, "CR-" + wh.craft_code(it))
        if getattr(it, "powders", None) and it.category == "armor":     # powders aren't part of a craft's code
            back = wh.armor_with_powders(back, it.powders)
        if back.max_stats != it.max_stats or back.reqs != it.reqs or back.bonus != it.bonus:
            out.append(f"crafted {it.type}'s code doesn't read back the same")
    if case.get("only_items"):                   # only the player's items, a ring they have one of worn once
        counts = wh.parse_owned_items(wh.LAST_RESULT["build"].data, case["only_items"], cls)["counts"]
        for it in armor + [weapon]:
            if wh.item_key(it) not in counts:
                out.append(f"{it.name} isn't one of the player's items")
        r1, r2 = best["armor"][4], best["armor"][5]
        if r1 is not None and r2 is not None and wh.item_key(r1) == wh.item_key(r2) and counts.get(wh.item_key(r1), 0) < 2:
            out.append(f"wears two {r1.name} but the player has one")
    fin = best["eval"]
    if case.get("minimums") and not fin.get("floors_met", True):
        out.append("misses the minimums: " + "; ".join(wh.floor_misses(wh.parse_floors(
            ", ".join(f"{k}={v}" for k, v in case["minimums"].items())), fin.get("floor_values", {}))))
    return out


def main():
    argv = sys.argv[1:]
    record = "--record" in argv
    one = None
    if "--case" in argv:                      # one case by its name (tests/run_all.py runs cases side by side)
        i = argv.index("--case")
        one = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    shard = None
    if "--shard" in argv:                     # "--shard 2/4": every 4th case starting at the 2nd (CI runs them side by side)
        i = argv.index("--shard")
        k, n = (int(x) for x in argv[i + 1].split("/"))
        shard = (k - 1, n)
        argv = argv[:i] + argv[i + 2:]
    only = {a.lower() for a in argv if not a.startswith("--")}
    cases = [c for c in CASES if (one is None or c["name"] == one) and (not only or c["class"] in only)]
    if shard is not None:
        cases = cases[shard[0]::shard[1]]
    if one is not None and not cases:
        sys.exit(f"No case named {one!r}.")
    base = {}
    if os.path.exists(BASELINE):
        with open(BASELINE, encoding="utf-8") as fh:
            base = json.load(fh)

    # Same answer on every computer: no remembered builds from earlier runs, nothing saved to history.
    ws.remembered_starts = lambda *a, **k: []
    ws.remember = lambda *a, **k: None
    app.save_history = lambda *a, **k: None
    app.STATE["data_checked"] = True          # use the game data in the repository

    known = {}
    if os.path.exists(BEST_KNOWN):
        with open(BEST_KNOWN, encoding="utf-8") as fh:
            known = json.load(fh)
    t_all = time.time()
    for case in cases:
        params = {k: v for k, v in case.items() if k != "name"}
        params.update(deep_search=False, include_level_plan=False)
        t = time.time()
        res = app.run_report(params)
        secs = time.time() - t
        bad = problems_with(case, res)
        check(f"{case['name']}: legal build", not bad, f"({secs:.0f}s)" + (" " + "; ".join(bad) if bad else ""))
        best = wh.LAST_RESULT.get("best")
        if not best or not res.get("ok"):
            continue
        S = res.get("summary") or {}
        if S.get("best"):                     # the build as a share code opens as exactly the same build
            e = dict({k: S.get(k) for k in ("class", "class_title", "level", "goal", "label", "focus", "floors",
                                             "toggles_on", "toggle_share", "settings", "rolls")},
                     numbers=S["best"]["numbers"], items=S["best"]["items"], sp=S["best"]["sp"],
                     owned=S["best"]["owned"], order=S["best"]["order"], link=S.get("best_link"))
            code = app.share_code(e, "Friend")
            back, err = app.decode_share(code)
            same = back is not None and all(back[k] == e[k] for k in ("items", "owned", "order", "sp", "label")) \
                and not back["shared_from"]["missing"]
            check(f"{case['name']}: share code opens the same build", same, f"({len(code)} characters)" + (f" {err}" if err else ""))
            # every ability in the tree says why it's there, and the reasons hold up
            roles = S["best"].get("roles") or {}
            tree, R = wh.LAST_RESULT["tree"], wh.LAST_RESULT
            okr = len(roles) == len(S["best"]["owned"]) and all(r["role"] in ("start", "goal", "path", "spare", "extra")
                                                                 for r in roles.values())
            bad_r = []
            for nid, r in roles.items():
                if r["role"] in ("spare", "extra") and tree.validate(frozenset(best["owned"]) - {int(nid)}, case["level"])["ok"]:
                    with_ = R["build"].evaluate(frozenset(best["owned"]), best["armor"], best["weapon"], best["extra"])["main"]
                    without = R["build"].evaluate(frozenset(best["owned"]) - {int(nid)}, best["armor"], best["weapon"], best["extra"])["main"]
                    if without > 0 and with_ / without - 1 >= 0.0005:
                        bad_r.append(tree.by_id[int(nid)].name)
                if r["role"] == "goal" and not (r["gain"] or 0) > 0:
                    bad_r.append(tree.by_id[int(nid)].name)
            check(f"{case['name']}: each ability says why it's there", okr and not bad_r,
                  f"({sum(1 for r in roles.values() if r['role'] == 'spare')} spare of {len(roles)})" + (f" wrong: {bad_r[:3]}" if bad_r else ""))
        score = best["eval"]["main"]
        print(f"SCORE\t{case['name']}\t{score!r}", flush=True)
        print(f"LINK\t{case['name']}\t{wh.LAST_RESULT.get('best_link')}", flush=True)
        print(f"ITEMS\t{case['name']}\t" + ", ".join(x.name if x else "-" for x in best["armor"] + [best["weapon"]]), flush=True)
        was = base.get(case["name"])
        if was is None:
            check(f"{case['name']}: not worse than before", True, f"({score:,.1f}; no score recorded yet)")
        else:
            check(f"{case['name']}: not worse than before", score >= was * (1 - 1e-9),
                  f"({score:,.1f} vs {was:,.1f} recorded" + (", better: run with --record)" if score > was * (1 + 1e-9)
                                                              else ")"))
        if record and (was is None or score > was):
            base[case["name"]] = score
        k = known.get(case["name"])
        if k and k.get("score"):
            print(f"      best known build: {k['score']:,.1f} (this search reaches {score / k['score'] * 100:.1f}%)", flush=True)
        if record and (not k or score > k.get("score", 0)):
            known[case["name"]] = {"score": score, "link": wh.LAST_RESULT.get("best_link"), "found_by": "current search"}

    if record:
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(base, fh, indent=2, sort_keys=True)
            fh.write("\n")
        if one is None and not only and shard is None:
            with open(BEST_KNOWN, "w", encoding="utf-8") as fh:
                json.dump(known, fh, indent=1, sort_keys=True)
                fh.write("\n")
        print(f"\nRecorded scores saved to {os.path.relpath(BASELINE, ROOT)}.")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed in {time.time() - t_all:.0f}s.")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
