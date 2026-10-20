import os
import sys, json, random
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as w
d = w.load_game_data(offline=True, data_dir=os.environ.get("WB_DATA_ROOT", w.DATA_DIR))
random.seed(7)
recipes = [r for r in d.recipes.values() if r['type'].lower() not in ('potion', 'scroll', 'food')]
ings = [i for i in d.ingredients.values() if i['id'] < 4000]
POWDER_SKILLS = {"ARMOURING", "TAILORING", "WEAPONSMITHING", "WOODWORKING", "JEWELING"}
cases = []
for n in range(3000):
    r = random.choice(recipes)
    skill = r['skill']
    pool = [i for i in ings if skill in i.get('skills', [])]
    ids = []
    for k in range(6):
        x = random.random()
        if x < 0.2:
            ids.append(4000)
        elif x < 0.32 and skill in POWDER_SKILLS:
            ids.append(4001 + random.randrange(35))
        else:
            ids.append(random.choice(pool)['id'])
    mats = [random.randint(1, 3), random.randint(1, 3)]
    atk = random.choice(w.CRAFT_ATK_SPDS)
    it = w.make_crafted(d, int(r['id']), mats, ids, atk)
    powders = []
    if it.category == 'weapon' and it.slots and random.random() < 0.7:
        powders = [w.Powder(random.randrange(5), random.randint(1, 7)) for _ in range(random.randint(1, it.slots))]
    out = {"recipe": r['name'], "mats": mats, "ings": ids, "atk": atk,
           "reqs": it.reqs, "bonus": it.bonus, "min": it.min_stats, "max": it.max_stats, "slots": it.slots,
           "powders": [p.elem * 7 + p.tier - 1 for p in powders]}
    if it.category == 'weapon':
        out["damage"] = [list(x) for x in w.weapon_with_powders(it, powders).damage]
    bits = w.Bits(); w.encode_craft_bits(bits, it); out["hash"] = bits.to_b64()
    # round trip through our decoder
    back = w.crafted_from_code(d, "CR-" + out["hash"])
    out["rt"] = back.max_stats == it.max_stats and back.reqs == it.reqs
    cases.append(out)
json.dump(cases, open('craft_cases.json', 'w'))
print(len(cases), sum(c['rt'] for c in cases), 'round-trips ok')
