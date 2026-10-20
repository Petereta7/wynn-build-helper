"""
Checks for ability toggles (buffs that only count while a toggle is on):

  1. A toggle that's fully on gives exactly the same numbers as before (the on/off switch Wynnbuilder has).
  2. A toggle that's up part of the time adds exactly that share of its effect to every spell.
  3. Wearing "the best Mystic Mask" equals the best of trying each mask by hand.
  4. With no toggles, nothing changes.

    python3 tests/toggle_check.py

Every line should say PASS.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wynn_helper as wh      # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name:<58} {detail}")


def spell_damages(build, owned, armor, weapon):
    e, _ = build.allocate_free_points(owned, armor, weapon)
    r = build.evaluate(owned, armor, weapon, e)
    return {k: v["damage"] for k, v in r["results"].items()}, r


def main():
    d = wh.load_game_data(offline=True)

    # 1 + 4: "fully on" through the share path equals the plain on switch; no toggles equals no toggles
    tree = wh.Tree(d, "shaman")
    owned = frozenset(n.id for n in tree.nodes if n.row <= 24)          # a big tree with Bullwhip in it
    a = tree.spells(owned, (), frozenset({"Activate Bullwhip"}))
    b = tree.spells(owned, (), {"Activate Bullwhip": 1.0})
    check("fully on = the plain on switch", repr(a[:3]) == repr(b[:3]))
    c = tree.spells(owned, (), frozenset())
    e = tree.spells(owned, (), {})
    check("no toggles = no toggles", repr(c[:3]) == repr(e[:3]))

    # 2: part of the time adds exactly that share (Coursing Restraints, Archer)
    at = wh.Tree(d, "archer")
    cr = at.find("Coursing Restraints")
    need = set()
    stack = [cr.id]
    while stack:                                       # Coursing Restraints and everything it needs
        n = at.by_id[stack.pop()]
        if n.id in need:
            continue
        need.add(n.id)
        stack.extend(n.parents[:1] + n.deps)
    owned_a = frozenset(need) | frozenset(at.roots)
    wpn = wh.default_weapon(d, "archer", 80)
    armor = [None] * 8
    vals = {}
    for share in (0.0, 1 / 3, 1.0):
        bld = wh.Build(d, at, 80, "spells", toggle_share={"Coursing Restraints": share} if 0 < share < 1 else None,
                       toggles=["Coursing Restraints"] if share >= 1 else ())
        vals[share], _ = spell_damages(bld, owned_a, armor, wpn)
    worst = 0.0
    for k in vals[0.0]:
        lo, hi, mid = vals[0.0][k], vals[1.0][k], vals[1 / 3][k]
        if hi - lo > 1e-9:
            worst = max(worst, abs(mid - (lo + (hi - lo) / 3)) / (hi - lo))
    changed = sum(1 for k in vals[0.0] if vals[1.0][k] - vals[0.0][k] > 1e-9)
    check("a 1/3 share adds exactly 1/3 of the effect", changed > 0 and worst < 1e-9,
          f"({changed} spells affected, largest gap {worst:.2e})")

    # 3: the best mask equals the best of the three tried by hand
    st = wh.Tree(d, "shaman")
    mm = st.find("Mystic Masks")
    need = set()
    stack = [mm.id]
    while stack:
        n = st.by_id[stack.pop()]
        if n.id in need:
            continue
        need.add(n.id)
        stack.extend(n.parents[:1] + n.deps)
    owned_s = frozenset(need) | frozenset(st.roots)
    wpn = wh.default_weapon(d, "shaman", 80)
    for goal in ("spells", "survival"):
        auto = wh.Build(d, st, 80, goal, auto_masks=True)
        ea, _ = auto.allocate_free_points(owned_s, armor, wpn)
        ra = auto.evaluate(owned_s, armor, wpn, ea)
        by_hand = {}
        for m in wh.MASKS:
            hb = wh.Build(d, st, 80, goal, toggles=[m])
            by_hand[m] = hb.evaluate(owned_s, armor, wpn, ea)["value"]
        best = max(by_hand, key=by_hand.get)
        check(f"best mask for {goal} = best of the three by hand", ra.get("mask") == best
              and abs(ra["value"] - by_hand[best]) < 1e-9, f"({ra.get('mask')})")

    # 5: a default buff the tree can't keep up isn't counted (Bullwhip needs Uproot hits, or Haunting Memory
    #    once Mystic Masks turns Uproot into mask switching)
    bw = wh.Build(d, st, 80, "spells", toggles=["Activate Bullwhip"], auto_toggles={"Activate Bullwhip"})
    ids = lambda *names: frozenset(st.find(n).id for n in names)
    ok = ("Activate Bullwhip" in bw.toggle_map(frozenset()) and
          "Activate Bullwhip" not in bw.toggle_map(ids("Mystic Masks")) and
          "Activate Bullwhip" in bw.toggle_map(ids("Mystic Masks", "Haunting Memory")))
    check("Bullwhip only counts when the tree can keep it up", ok)

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed.")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
