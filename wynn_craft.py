"""
Crafted items designed for a build.

For one slot of a build (a helmet, a ring, the weapon...), find the crafted item that makes the whole build
best for its goal, with the rest of the build as it is. A crafted item is a recipe (the highest level range
the character can wear, which gives the most health or damage), two materials (tier 3, the strongest), for
weapons an attack speed, and six ingredients in a 3 x 2 grid. Some ingredients change how strong the
ingredients next to, above, below or away from them are, so where each one goes matters.

Only crafts that work in game are made (Wynnbuilder's crafter rules): every ingredient usable by the
recipe's profession and not above the recipe's level range, and durability left over.

The stats of every craft come from wynn_helper.make_crafted, which gives exactly what Wynnbuilder's crafter
gives (checked side by side in tests/crossval).

How the six ingredients are found: every usable ingredient is first scored alone in the build. The strongest
of those, the ingredients that make their neighbours stronger, and the empty slot make the candidates. Then,
from a few different starting grids, each grid position is tried with every candidate, and pairs of
positions are swapped, until nothing improves; then a few random shake-ups. tests/craft_check.py compares
this with trying every possible grid on small cases.
"""
from __future__ import annotations

import random

import wynn_helper as wh

MATS = (3, 3)            # material tiers: tier 3 gives the most health or damage and the most durability
NO_ING = 4000


def recipe_for(data, kind: str, level: int, min_level: int = 1):
    """The highest level-range recipe of this type the character can wear (most health or damage, and the
    widest choice of ingredients). None if there isn't one."""
    best = None
    for r in (getattr(data, "recipes", {}) or {}).values():
        if r["type"].lower() != kind:
            continue
        lo = (r.get("lvl") or {}).get("minimum", 0)
        if lo > level:
            continue
        if best is None or lo > best["lvl"]["minimum"]:
            best = r
    return best


def usable_ingredients(data, recipe) -> list:
    """Every ingredient the recipe can use (its profession, not above its level range), powders and the
    empty slot included."""
    skill, top = recipe["skill"], recipe["lvl"]["maximum"]
    return [ing for ing in data.ingredients.values()
            if skill in (ing.get("skills") or ()) and (ing.get("lvl") or 0) <= top]


def stat_key(it):
    """Everything about a crafted item that scoring a build can see (requirements, skill points, every stat
    roll, damage, attack speed, powder slots, powders used as ingredients). Equal keys = equal items."""
    return (it.category, it.type, tuple(it.reqs), tuple(it.bonus), tuple(sorted(it.max_stats.items())),
            tuple(sorted(it.min_stats.items())), tuple(map(tuple, it.damage)) if it.damage else None,
            it.atk_spd, it.slots, tuple(getattr(it, "ingred_powders", None) or ()))


def _gt(a, b) -> bool:
    """a is really better than b (not just rounding; two impossible grids are equal)."""
    return a > b + 1e-12 * max(1.0, abs(b))


def _boost(ing) -> float:
    pm = ing.get("posMods") or {}
    return sum(max(0, v) for v in pm.values())


class Designer:
    """Designs the crafted item for one slot of one build.

    score(item) -> value of the whole build with `item` in the slot (None if it can't be worn)."""

    def __init__(self, data, recipe, score, atk_spds=("SLOW",), top=28, boosters=10, seed=0, kicks=24, pool=None):
        self.data, self.recipe, self.score = data, recipe, score
        self.rid = int(recipe["id"])
        self.atk_spds = atk_spds
        self.top, self.n_boost, self.kicks = top, boosters, kicks
        self.rng = random.Random(seed)
        self.memo = {}
        self.by_item = {}
        self.evals = 0
        self.pool = pool          # only these ingredients (tests); default: every usable one

    def make(self, ids, atk):
        return wh.make_crafted(self.data, self.rid, MATS, list(ids), atk)

    def value(self, ids, atk):
        key = (tuple(ids), atk)
        v = self.memo.get(key)
        if v is None:
            it = self.make(ids, atk)
            if it.craft_problems:
                v = None
            else:
                # Different grids often make exactly the same item (the same ingredients in another order, when
                # none of them changes its neighbours). The same item always gets the same score, so it's scored
                # once (about 4 grids in 10).
                sk = stat_key(it)
                v = self.by_item.get(sk)
                if v is None:
                    self.evals += 1
                    v = self.score(it)
                    self.by_item[sk] = -1e300 if v is None else v
            self.memo[key] = v = (-1e300 if v is None else v)
        return v

    def candidates(self, atk, pool):
        solo = []
        for ing in pool:
            if ing["id"] == NO_ING:
                continue
            solo.append((self.value([ing["id"]] + [NO_ING] * 5, atk), ing))
        solo.sort(key=lambda x: -x[0])
        base = self.value([NO_ING] * 6, atk)
        keep = [ing for v, ing in solo[: self.top] if v > -1e299]
        boosters = sorted((ing for ing in pool if _boost(ing) > 0 and ing not in keep),
                          key=lambda ing: -_boost(ing))[: self.n_boost]
        ids = [NO_ING] + [ing["id"] for ing in keep + boosters]
        return ids, base, [ing["id"] for v, ing in solo if v > base]

    def improve(self, ids, atk, cand):
        ids = list(ids)
        v = self.value(ids, atk)
        improved = True
        while improved:
            improved = False
            for pos in range(6):                      # each position: every candidate
                best_c, best_v = ids[pos], v
                for c in cand:
                    if c == ids[pos]:
                        continue
                    t = ids[:pos] + [c] + ids[pos + 1:]
                    tv = self.value(t, atk)
                    if _gt(tv, best_v):
                        best_c, best_v = c, tv
                if best_c != ids[pos]:
                    ids[pos], v, improved = best_c, best_v, True
            for i in range(6):                        # pairs of positions swapped
                for j in range(i + 1, 6):
                    if ids[i] == ids[j]:
                        continue
                    t = list(ids)
                    t[i], t[j] = t[j], t[i]
                    tv = self.value(t, atk)
                    if _gt(tv, v):
                        ids, v, improved = t, tv, True
        return v, ids

    def design(self):
        """(value, crafted item) of the best craft found, or (None, None)."""
        pool = self.pool if self.pool is not None else usable_ingredients(self.data, self.recipe)
        best = (None, None, None)
        for atk in self.atk_spds:
            cand, base, good = self.candidates(atk, pool)
            starts = [[NO_ING] * 6]
            if good:
                starts.append([good[0]] * 6)                      # six of the strongest single ingredient
                starts.append((good[:6] + [NO_ING] * 6)[:6])      # the six strongest, one each
            boost = [c for c in cand if _boost(self.data.ingredients[c]) > 0]
            if boost and good:                                    # a booster in the middle, the best around it
                starts.append([good[0], good[0], boost[0], good[0], good[0], good[0]])
            results = [self.improve(s, atk, cand) for s in starts]
            v, ids = max(results, key=lambda r: r[0])
            for _ in range(self.kicks):                           # shake up two or three positions
                t = list(ids)
                for pos in self.rng.sample(range(6), self.rng.choice([2, 3])):
                    t[pos] = self.rng.choice(cand)
                tv, tids = self.improve(t, atk, cand)
                if _gt(tv, v):
                    v, ids = tv, tids
            if v > -1e299 and (best[0] is None or v > best[0]):
                best = (v, ids, atk)
        if best[0] is None:
            return None, None
        return best[0], self.make(best[1], best[2])


def describe(it) -> list:
    """Lines describing how to make a crafted item: recipe, materials, the ingredient grid."""
    if not getattr(it, "link_code", None) or it.link_code[0] != "craft":
        return []
    _k, rid, mats, ing_ids, atk = it.link_code
    names = [("—" if i == NO_ING else _ing_name(i)) for i in ing_ids]
    out = [f"Recipe {it.recipe_name}, materials tier {mats[0]} and tier {mats[1]}"
           + (f", {atk.lower()} attack speed" if it.category == "weapon" else "")]
    w = max(len(n) for n in names)
    for row in range(3):
        out.append(f"  [ {names[2 * row]:<{w}} ] [ {names[2 * row + 1]:<{w}} ]")
    return out


_ING_NAMES = {}


def _ing_name(i):
    return _ING_NAMES.get(i, f"ingredient {i}")


def remember_names(data):
    for i, ing in data.ingredients.items():
        _ING_NAMES[i] = ing.get("displayName") or ing["name"]
