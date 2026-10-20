#!/usr/bin/env python3
"""
Accuracy test: on small random cases, check EVERY gear combination by brute force and compare with what
the build search finds. Run:  python3 tests/accuracy_test.py [first_seed] [how_many]
Each case takes 1-4 minutes (the brute force part); the search itself takes about a second.
"""
import os
os.cpu_count = lambda: 1          # compare answers, not speed (and keep it simple on any computer)
import sys, random, itertools, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wynn_helper as w, wynn_search as s
DATA = w.load_game_data(offline=True)
def case(seed):
  rng = random.Random(seed)
  d = DATA
  cls = rng.choice(w.CLASSES); goal = rng.choice(["melee", "melee", "spells", "survival", "balanced"]); lvl = rng.choice([30, 45, 60, 80, 100])
  tree = w.Tree(d, cls); b = w.Build(d, tree, lvl, goal)
  pools, _ = s.safe_filter(b)
  own = frozenset(tree.roots)
  sizes = {0: 2, 1: 2, 2: 2, 3: 2, 4: 2, 6: 2, 7: 2, "weapon": 2}
  small = {}
  for sl, n in sizes.items():
      # pick from the strongest items alone, so the case isn't trivially easy
      scored = []
      for it in pools[sl]:
          a = [None] * 8; wpn = None
          if sl == "weapon":
              wpn = it
          else:
              a[sl] = it
          wp = wpn or w.default_weapon(d, cls, lvl)
          if not w._wearable(a, wp, lvl):
              continue
          scored.append((b.evaluate(own, a, wp, [0]*5, fast=True)["value"], it))
      scored.sort(key=lambda x: -x[0])
      top = [it for _, it in scored[:8]]
      small[sl] = rng.sample(top, min(n, len(top)))
  small[5] = small[4]
  def final_score(a, wpn):
      pw, _ = w.best_weapon_powders(b, own, a, wpn, [0]*5)  # every powder order, not just one element
      wp = w.weapon_with_powders(wpn, pw)
      if not w._wearable(a, wp, lvl):
          return None
      e, _ = s.refine_points(b, own, a, wp, [0]*5)
      return b.evaluate(own, a, wp, e, fast=True)["value"]
  t = time.time(); best = (-1, None); tops = []
  opt = {k: (v + [None] if k != "weapon" else v) for k, v in small.items()}
  ring_pairs = list(itertools.combinations_with_replacement(opt[4], 2))
  for h, c, l, bo, (r1, r2), br, ne, wpn in itertools.product(opt[0], opt[1], opt[2], opt[3], ring_pairs, opt[6], opt[7], opt["weapon"]):
      a = [h, c, l, bo, r1, r2, br, ne]
      v = final_score(a, wpn)
      if v is not None:
          tops.append((v, a, wpn))
          tops = sorted(tops, key=lambda x: -x[0])[:6]
      if v is not None and v > best[0]:
          best = (v, [x.name if x else None for x in a], wpn.name)
  tb = time.time() - t
  t = time.time()
  r = s.search_best_build(b, own, [None]*8, small["weapon"][0], small["weapon"][0], ap_budget=0, pools_override=small)
  def exact(a, wpn):
      # every powder combination, each with the exact skill point split
      tier = w.max_powder_tier(getattr(wpn, "level_high", wpn.level)); out = None
      for seq in (itertools.product(range(5), repeat=wpn.slots) if wpn.slots else [()]):
          wp = w.weapon_with_powders(wpn, [w.Powder(e_, tier) for e_ in seq])
          if not w._wearable(a, wp, lvl):
              continue
          e_, _ = s.refine_points(b, own, a, wp, [0]*5)
          v_ = b.evaluate(own, a, wp, e_, fast=True)["value"]
          out = v_ if out is None or v_ > out else out
      return out
  true_best = max((exact(a, wpn) or -1) for _v, a, wpn in tops) if tops else -1
  best = (true_best, best[1], best[2])
  found = exact(r["armor"], r["base_weapon"])
  ok = found is not None and found >= best[0] * 0.9999
  within = (found or 0) / best[0] if best[0] > 0 else 1
  print(f"case {seed} {cls} L{lvl} {goal}: brute {best[0]:.1f} ({tb:.0f}s) search {found:.1f} ({time.time()-t:.0f}s) {'OK' if ok else 'MISSED'} ({within*100:.3f}% of best)")
  if not ok:
      print("  best:", best[1], best[2]); print("  found:", [x.name if x else None for x in r['armor']], r['base_weapon'].name)
  sys.stdout.flush()
  return ok


def main():
  first = int(sys.argv[1]) if len(sys.argv) > 1 else 100
  n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
  results = [case(sd) for sd in range(first, first + n)]
  print(f"{sum(results)}/{len(results)} cases: the search found the true best build")
  sys.exit(0 if all(results) else 1)

if __name__ == "__main__":
  main()
