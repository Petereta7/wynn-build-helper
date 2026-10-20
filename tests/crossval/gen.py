import os
import json, random, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
random.seed(7)
cases = []
keys = ["sdPct","mdPct","damPct","sdRaw","mdRaw","damRaw","rSdPct","rMdPct","rDamPct","rSdRaw","rDamRaw",
        "critDamPct"] + [p+s for p in "netwfa" for s in ("DamPct","SdPct","MdPct","SdRaw","MdRaw","DamRaw","DamAddMin","DamAddMax")]
for _ in range(2000):
    stats = {k: random.choice([0,0,0, random.randint(-30,120)]) for k in keys}
    for s in wh.SKILLS:
        stats[s] = random.randint(0, 160)
    dmg = []
    for i in range(6):
        lo = random.choice([0, random.randint(0,200)]); hi = lo + random.choice([0, random.randint(0,300)])
        dmg.append([lo, hi])
    if all(h == 0 for _, h in dmg): dmg[0] = [10, 20]
    conv = [random.choice([0, random.randint(0,400)]) for _ in range(6)]
    if conv[0] == 0 and random.random() < 0.5: conv[0] = random.randint(1,300)
    dm = {f"m{i}": random.randint(-20,60) for i in range(random.randint(0,2))}
    pf = random.choice(["1.Heal", "3.Meteor", "0.Melee", None])
    if random.random() < 0.6: dm["X:3.Meteor"] = random.randint(-10, 80)
    if random.random() < 0.6: dm["Y;" + random.choice("netwfam")] = random.randint(-10, 80)
    ign = ["m0"] if random.random() < 0.3 else []
    if random.random() < 0.5: stats["tConvBase:3.Meteor"] = random.randint(0, 60)
    if random.random() < 0.5: stats["eConvBase"] = random.randint(0, 40)
    case = dict(stats=stats, dmg=dmg, atk=random.choice(wh.ATTACK_SPEEDS), conv=conv,
                use_spell=random.random()<0.5, ignore_speed=random.random()<0.5,
                ignore_str=random.random()<0.2, dm=dm, pf=pf, ign=ign)
    class W: pass
    w = W(); w.damage=[tuple(x) for x in dmg]; w.atk_spd=case["atk"]
    n, c = wh.spell_part_damage(dict(stats), w, conv, case["use_spell"], case["ignore_speed"], case["ignore_str"], dm, part_filter=pf, ignored_mults=tuple(ign))
    case["py"] = [n, c]
    cases.append(case)
json.dump(cases, open("cases.json","w"))
print(len(cases), "cases")
