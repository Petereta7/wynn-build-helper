import os
import json, sys
sys.path.insert(0, os.environ.get("HELPER_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
import wynn_helper as wh
d = wh.load_game_data(offline=True, data_dir=os.environ.get("WB_DATA_ROOT", wh.DATA_DIR))
out = {it.raw["name"]: {"min": it.min_stats, "max": it.max_stats} for it in d.items.values()}
json.dump(out, open("py_rolls.json", "w"))
print(len(out), "items")
