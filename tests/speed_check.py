"""
Checks that the fast skill point solver gives exactly the same answers as the reference version
(same points per skill, same totals, same equip order), and that the quick "can this be worn" test agrees.

    python3 tests/speed_check.py            # 3,000 random gear sets
    python3 tests/speed_check.py 10000      # more

Every line should say 0 differences.
"""
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import wynn_helper as wh      # noqa: E402
import wynn_search as ws      # noqa: E402


def main():
    n_per = int(sys.argv[1]) // 6 if len(sys.argv) > 1 else 500
    d = wh.load_game_data(offline=True)
    rng = random.Random(2026)
    cases = [("warrior", 60), ("mage", 100), ("archer", 106), ("shaman", 80), ("assassin", 45), ("mage", 30)]
    diff = wear_diff = n = 0
    t_ref = t_fast = 0.0
    for cls, lvl in cases:
        b = wh.Build(d, wh.Tree(d, cls), lvl, "spells")
        pools = {sl: ws.item_pool(b, wh.SLOT_TYPES[sl]) for sl in range(8)}
        weapons = ws.item_pool(b, wh.CLASS_WEAPON[cls])
        neg = {sl: [it for it in pools[sl] if any(x < 0 for x in it.bonus)] for sl in range(8)}
        for _ in range(n_per):
            armor = []
            for sl in range(8):
                r = rng.random()
                if r < 0.1 or not pools[sl]:
                    armor.append(None)
                elif r < 0.45 and neg[sl]:
                    armor.append(rng.choice(neg[sl]))
                else:
                    armor.append(rng.choice(pools[sl]))
            wp = rng.choice(weapons)
            arm = [a for a in armor if a is not None]
            t = time.time()
            ref = wh._solve_skill_points_reference(arm, wp)
            t_ref += time.time() - t
            t = time.time()
            fast = wh._solve_skill_points_uncached(arm, wp)
            t_fast += time.time() - t
            n += 1
            if (list(ref[0]) != list(fast[0]) or list(ref[1]) != list(fast[1])
                    or [id(x) for x in ref[2]] != [id(x) for x in fast[2]]):
                diff += 1
                print("DIFFERENT:", cls, lvl, [a.name for a in arm], wp.name, ref[:2], fast[:2])
            avail = wh.skill_points_available(lvl)
            ok_ref = sum(ref[0]) <= avail and max(ref[0]) <= wh.MAX_ASSIGNED_PER_SKILL
            if wh._wearable(armor, wp, lvl) != ok_ref:
                wear_diff += 1
                print("WEARABLE DIFFERS:", cls, lvl, [a.name for a in arm], wp.name)
    # Items sent from another process (the search's workers) carry that process's item numbers. They must
    # never be mistaken for this process's numbers: pretend to be a new process and check again.
    import pickle
    cross = 0
    sample = [it for it in d.items.values() if it.category != "weapon"][:400]
    for it in sample:
        wh._sp_sig(it)
    sent = pickle.loads(pickle.dumps(sample))
    wh._SIG_IDS.clear(); wh._SP_CACHE.clear(); wh._WEAR_CACHE.clear()
    wh._SIG_EPOCH = os.urandom(8)
    wpn = d.find_item("Void Catalyst")[0]
    import copy
    for it in [it for it in d.items.values() if it.category != "weapon"][-300:]:   # this "process" numbers and
        local = copy.copy(it)                                                     # solves other items first
        local.__dict__.pop("_spsig", None)
        wh.solve_skill_points([local], wpn)
    for it in sent:
        if wh.solve_skill_points([it], wpn)[0] != list(wh._solve_skill_points_reference([it], wpn)[0]):
            cross += 1
    print(f"skill points     {n} gear sets, {diff} differences (reference {t_ref:.1f}s, fast {t_fast:.1f}s)")
    print(f"between processes {len(sent)} items sent from another process, {cross} differences")
    diff += cross
    print(f"can be worn      {n} gear sets, {wear_diff} differences")
    sys.exit(1 if diff or wear_diff else 0)


if __name__ == "__main__":
    main()
