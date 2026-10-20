import os
import sys, json
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as w
cases = json.load(open('custom_cases.json'))
ok, bad = 0, []
for c in cases:
    s = c['spec']
    it = w.custom_from_code('CI-' + c['hash'])
    errs = []
    if it.name != c['exp']['name']: errs.append(('name', it.name, c['exp']['name']))
    if it.type != s['type'] or it.level != s['lvl'] or it.slots != s['slots']: errs.append(('basic', it.type, it.level, it.slots))
    if it.reqs != s['reqs'] or it.bonus != s['sp']: errs.append(('sp', it.reqs, it.bonus))
    if int(it.max_stats.get('hp', 0)) != s['hp']: errs.append(('hp', it.max_stats.get('hp'), s['hp']))
    for i, e in enumerate('etwfa'):
        if int(it.max_stats.get(e + 'Def', 0)) != s['defs'][i]: errs.append(('def', e))
    for k in ('min', 'max'):
        mine = {kk: v for kk, v in (it.min_stats if k == 'min' else it.max_stats).items() if kk in w.ROLLED_IDS and v}
        exp = {kk: float(v) for kk, v in c['exp'][k].items()}
        if mine != exp: errs.append((k, mine, exp))
    if it.category == 'weapon':
        exp_d = [tuple(float(x) for x in p) for p in s['dam']]
        if it.damage != exp_d or it.atk_spd != s['atk'] or it.class_req != s['cls'].lower(): errs.append(('wpn', it.damage, exp_d))
    if it.link_code[1] != c['hash']: errs.append(('code',))
    if not errs: ok += 1
    elif len(bad) < 4: bad.append(errs[:3])
for b in bad: print(b)
print('custom items identical:', ok, '/', len(cases))
