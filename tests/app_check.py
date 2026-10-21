"""
App check: the things that would break the app for everyone if they slipped into an update.

  * every Python file compiles, and the page's JavaScript has no syntax errors (needs Node.js; skipped without)
  * no real API key anywhere in the files, and api_key.txt is only the blank template
  * version.json matches the newest entry in versions.json
  * the updater never touches your key, settings, history or saved versions: an update and a switch back are run
    on a copy of the app, and the personal files are checked afterwards
  * the app's server starts and answers the page and its data requests, for every class
  * the character library works (on a temporary copy, never your own)
  * share codes: every class round-trips exactly; damaged or tampered codes are refused or cleaned

    python3 tests/app_check.py

Every line should say PASS (or SKIP when Node.js isn't installed).
"""
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["WYNN_HELPER_NO_POPUP"] = "1"

RESULTS = []
KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
SKIP_DIRS = {".git", "__pycache__", "_versions", "_previous_version", "crossval/work", "crossval/wynnbuilder",
             "node_modules"}
PERSONAL = ("api_key.txt", "wynn_app_config.json", "wynn_data/build_history.json", "wynn_data/best_builds.json",
            "wynn_data/library.json")


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail}", flush=True)


def skip(name, why):
    print(f"SKIP  {name:<52} ({why})", flush=True)


def repo_files():
    """Files that would be published: the git-tracked ones when git is there, otherwise everything."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = [f for f in out.splitlines() if f]
        if files:
            return files
    except Exception:
        pass
    files = []
    for dp, dn, fn in os.walk(ROOT):
        rel_dir = os.path.relpath(dp, ROOT).replace(os.sep, "/")
        dn[:] = [d for d in dn if (d if rel_dir == "." else f"{rel_dir}/{d}").replace("tests/", "") not in SKIP_DIRS
                 and d not in SKIP_DIRS]
        files += [os.path.normpath(os.path.join(rel_dir, f)).replace(os.sep, "/") for f in fn]
    return files


def check_code():
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(repo_files()):
            if f.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(ROOT, f), cfile=os.path.join(tmp, f"{i}.pyc"), doraise=True)
                except Exception as exc:
                    bad.append(f"{f}: {exc}")
    check("every Python file compiles", not bad, "; ".join(bad[:2]))

    node = shutil.which("node")
    if not node:
        skip("the page's JavaScript has no syntax errors", "Node.js isn't installed")
        return
    with open(os.path.join(ROOT, "app", "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    with tempfile.TemporaryDirectory() as tmp:
        js = os.path.join(tmp, "page.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write("\n;\n".join(scripts))
        r = subprocess.run([node, "--check", js], capture_output=True, text=True)
    check("the page's JavaScript has no syntax errors", r.returncode == 0 and scripts,
          f"({len(scripts)} script blocks)" if r.returncode == 0 else r.stderr.strip()[-300:])


def check_release_files():
    found = []
    for f in repo_files():
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p) or os.path.getsize(p) > 20 * 1024 * 1024:
            continue
        with open(p, "rb") as fh:
            text = fh.read().decode("utf-8", "ignore")
        if KEY_RE.search(text):
            found.append(f)
    check("no real API key in any file", not found, ", ".join(found))

    key_lines = []
    kp = os.path.join(ROOT, "api_key.txt")
    if os.path.exists(kp):
        with open(kp, encoding="utf-8") as fh:
            key_lines = [l for l in fh.read().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    check("api_key.txt is only the blank template", not key_lines)

    tracked = set(repo_files())
    leaked = [f for f in PERSONAL[1:] if f in tracked]
    check("personal files aren't published", not leaked, ", ".join(leaked))

    with open(os.path.join(ROOT, "version.json"), encoding="utf-8") as fh:
        ver = json.load(fh)
    with open(os.path.join(ROOT, "versions.json"), encoding="utf-8") as fh:
        vers = json.load(fh)["versions"]
    import updater
    check("updater only offers checked versions (stable)", updater.BRANCH == "stable" and updater.configured(),
          f"(reads {updater.OWNER}/{updater.REPO}@{updater.BRANCH})")
    names = [v["version"] for v in vers]
    ordered = names == sorted(names, key=updater._vtuple, reverse=True) and len(set(names)) == len(names)
    check("version.json = newest entry in versions.json", names and names[0] == ver["version"] and ordered,
          f"({ver['version']} vs {names[0] if names else 'none'})")


def check_updater():
    import updater
    with tempfile.TemporaryDirectory() as tmp:
        app = os.path.join(tmp, "app")
        shutil.copytree(ROOT, app, ignore=shutil.ignore_patterns(".git", "__pycache__", "_versions", "_previous_version",
                                                                  "wynnbuilder", "work"))
        personal = {"api_key.txt": "sk-ant-test-" + "x" * 30 + "\n",
                    "wynn_app_config.json": '{"model": "mine", "sound": false}\n',
                    "wynn_data/build_history.json": '[{"id": "mine"}]\n',
                    "wynn_data/best_builds.json": '{"mine": 1}\n',
                    "wynn_data/library.json": '{"characters": [{"id": "mine"}]}\n',
                    "_versions/0.9.0/version.json": '{"version": "0.9.0"}\n'}
        for rel, text in personal.items():
            p = os.path.join(app, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)

        # What the "download" contains: this version with a higher number and the personal files overwritten.
        with open(os.path.join(ROOT, "version.json"), encoding="utf-8") as fh:
            cur = json.load(fh)["version"]
        major, minor, patch = updater._vtuple(cur)
        newer = f"{major}.{minor}.{patch + 1}"
        files = {}
        for f in repo_files():
            p = os.path.join(ROOT, f)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    files[f] = fh.read()
        files["version.json"] = json.dumps({"version": newer, "notes": "test"}).encode()
        files["wynn_helper.py"] = files["wynn_helper.py"] + b"\n# test update\n"
        for rel in personal:
            files[rel] = b"OVERWRITTEN"

        saved = {k: getattr(updater, k) for k in ("HERE", "VERSION_FILE", "BACKUP_DIR", "SNAP_DIR", "_files_from_zip")}
        try:
            updater.HERE, updater.VERSION_FILE = app, os.path.join(app, "version.json")
            updater.BACKUP_DIR, updater.SNAP_DIR = os.path.join(app, "_previous_version"), os.path.join(app, "_versions")
            updater._files_from_zip = lambda url: dict(files)
            log = io.StringIO()
            res = updater.install(newer, log=lambda m: log.write(m + "\n"))
            ok_up = res.get("ok") and updater.current_version()["version"] == newer
            res2 = updater.install(version=cur, log=lambda m: log.write(m + "\n"))     # switch back (from the copy kept)
            ok_back = res2.get("ok") and updater.current_version()["version"] == cur
            with open(os.path.join(app, "wynn_helper.py"), "rb") as fh:
                ok_back = ok_back and not fh.read().endswith(b"# test update\n")
        finally:
            for k, v in saved.items():
                setattr(updater, k, v)

        check("updater installs a newer version", ok_up, "" if ok_up else str(res.get("error")))
        check("updater switches back to the version kept", ok_back, "" if ok_back else str(res2.get("error")))
        changed = []
        for rel, text in personal.items():
            with open(os.path.join(app, *rel.split("/")), encoding="utf-8") as fh:
                if fh.read() != text:
                    changed.append(rel)
        check("updates never touch key, settings, history", not changed, ", ".join(changed))


def check_server():
    import wynn_app
    from http.server import ThreadingHTTPServer
    wynn_app.STATE["data_checked"] = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), wynn_app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=60) as r:
            body = r.read()
        return json.loads(body) if r.headers.get("Content-Type", "").startswith("application/json") else body

    try:
        page = get("/")
        check("app page loads", b"<html" in page.lower() and b"</html>" in page.lower())
        meta = get("/api/meta")
        import wynn_helper as wh
        check("app data: classes, goals, versions", meta["classes"] == wh.CLASSES and meta["goals"]
              and meta["app_version"] != "?", f"(app {meta['app_version']}, game data {meta['version']})")
        bad = []
        for cls in wh.CLASSES:
            names = get(f"/api/abilities?class={cls}")
            items = get(f"/api/items?q=an&kind={wh.CLASS_WEAPON[cls]}")
            if not names or not items:
                bad.append(f"{cls}: {len(names)} abilities, {len(items)} items")
        check("abilities and item search for every class", not bad, "; ".join(bad))
        h, live, prog = get("/api/history"), get("/api/live"), get("/api/progress")
        check("history, live view and progress answer", isinstance(h, list) and "id" in live and "progress" in prog)
    finally:
        srv.shutdown()


def check_library():
    """Characters and their saved builds: create, save a build from History, view it, mark current, rename,
    remove, delete; on a temporary copy, never your own library."""
    import wynn_app
    from http.server import ThreadingHTTPServer
    with tempfile.TemporaryDirectory() as tmp:
        saved = (wynn_app.LIBRARY_PATH, wynn_app.HISTORY_PATH)
        wynn_app.LIBRARY_PATH, wynn_app.HISTORY_PATH = os.path.join(tmp, "library.json"), os.path.join(tmp, "history.json")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wynn_app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        try:
            item = {"name": "Crafted Ring (Lv 20-23)", "tier": "Crafted", "craft": {"link": "https://wynnbuilder.github.io/crafter/#ABC"}}
            wynn_app.save_history([{"id": "h1", "class": "mage", "class_title": "Mage", "level": 20, "goal": "spells",
                                    "label": "Spell damage per second", "numbers": {"goal": 1239.0},
                                    "items": [{"name": "Temporal Cage", "tier": "Unique"}, None, item], "sp": {}}])
            ok = True
            lib = call("/api/library")
            ok &= len(lib["classes"]) == 5 and lib["characters"] == []
            r = call("/api/library", {"action": "create", "class": "mage", "name": "Sparky", "level": 18})
            cid = r["character_id"]
            ok &= r["ok"] and r["characters"][0]["name"] == "Sparky" and r["classes"][2]["count"] == 1
            r = call("/api/library", {"action": "save", "history_id": "h1", "character_id": cid})
            c = r["characters"][0]
            ok &= r["ok"] and len(c["builds"]) == 1 and c["current"] == c["builds"][0]["id"] and c["level"] == 20
            ok &= c["builds"][0]["gear"] == ["Temporal Cage", "CR-ABC"]
            r2 = call("/api/library", {"action": "save", "history_id": "h1", "character_id": cid})     # saved once only
            ok &= len(r2["characters"][0]["builds"]) == 1
            bid = c["builds"][0]["id"]
            full = call(f"/api/library/build?c={cid}&b={bid}")
            ok &= full["character"]["name"] == "Sparky" and full["numbers"]["goal"] == 1239.0 and "tree" in full
            wynn_app.save_history([])                                     # clearing History keeps the library
            ok &= len(call("/api/library")["characters"][0]["builds"]) == 1
            r = call("/api/library", {"action": "save", "history_id": "nope", "character_id": cid})
            ok &= not r["ok"]
            r = call("/api/library", {"action": "update", "id": cid, "name": "  Zap  ", "level": 25})
            ok &= r["characters"][0]["name"] == "Zap" and r["characters"][0]["level"] == 25
            # the bot's side: finding characters by what the player calls them, its instructions, its tool,
            # and a build for a character filling in class, level and gear
            ok_bot = wynn_app.find_character("zap")[0]["id"] == cid and wynn_app.find_character("my mage")[0]["id"] == cid
            ok_bot &= wynn_app.find_character("nobody")[0] is None
            ctx = wynn_app.characters_context()
            ok_bot &= "Zap: level 25 Mage" in ctx and "Temporal Cage" in ctx and "a crafted piece" in ctx
            seen = {}
            real_run = wynn_app.wh.run
            wynn_app.wh.run = lambda a: (seen.update(cls=a.cls, level=a.level, gear=a.gear), print("fake report"))
            try:
                out = wynn_app.run_report({"character": "Zap", "goal": "spells"})
                ok_bot &= seen == {"cls": "mage", "level": 25, "gear": "Temporal Cage, CR-ABC"} and out["character_id"] == cid
                out = wynn_app.run_report({"character": "Zap", "level": 30, "use_saved_gear": False})
                ok_bot &= seen["level"] == 30 and not seen["gear"]
            finally:
                wynn_app.wh.run = real_run
            wynn_app.save_history([{"id": "h2", "class": "mage", "class_title": "Mage", "level": 30, "goal": "melee",
                                    "label": "Average DPS", "numbers": {"goal": 5.0}, "items": [], "sp": {}}])
            wynn_app.STATE["last_history_id"] = "h2"
            msg = wynn_app.manage_characters({"action": "save_last_build", "name": "Zap"})
            ok_bot &= msg.startswith("Done") and "2 saved build" in msg
            msg = wynn_app.manage_characters({"action": "update", "name": "Zap", "level": 31})
            ok_bot &= "level 31" in msg
            ok_bot &= not wynn_app.manage_characters({"action": "update", "name": "nobody", "level": 3}).startswith("Done")
            check("the bot finds and uses your characters", ok_bot)
            r = call("/api/library", {"action": "remove", "character_id": cid, "build_id": bid})
            ok &= len(r["characters"][0]["builds"]) == 1 and r["characters"][0]["current"] != bid
            r = call("/api/library", {"action": "delete", "id": cid})
            ok &= r["characters"] == []
            check("character library (create, save, view, edit, delete)", ok)
        except Exception as exc:
            check("character library (create, save, view, edit, delete)", False, f"{type(exc).__name__}: {exc}")
        finally:
            srv.shutdown()
            wynn_app.LIBRARY_PATH, wynn_app.HISTORY_PATH = saved


def _share_entry(wynn_app, wh, d, cls, n):
    """A saved build for this class made of real items: powdered weapon and helmet, a crafted ring, an empty slot."""
    import random
    rng = random.Random(n)
    craft_cases = json.load(open(os.path.join(HERE, "golden", "craft_cases.json"), encoding="utf-8"))
    by_name = {r.get("name"): rid for rid, r in d.recipes.items()}
    ring = next(c for c in craft_cases if c["recipe"].startswith("Ring-") and c["recipe"] in by_name)
    crafted_ring = wh.make_crafted(d, by_name[ring["recipe"]], ring["mats"], ring["ings"], ring["atk"])
    pick = lambda t, **kw: rng.choice([it for it in d.items.values() if it.type == t and it.level <= 100
                                       and all(getattr(it, k) >= v for k, v in kw.items())])
    wpn = wh.weapon_with_powders(pick(wh.CLASS_WEAPON[cls], slots=2), [wh.Powder(rng.randrange(5), 6)] * 2)
    helmet = wh.armor_with_powders(pick("helmet", slots=1), [wh.Powder(rng.randrange(5), 4)])
    armor = [helmet, pick("chestplate"), None, pick("boots"), crafted_ring, pick("ring"), pick("bracelet"), pick("necklace")]
    tree = wh.Tree(d, cls)
    owned = set(tree.roots)
    for _ in range(25):
        cand = [nd.id for nd in tree.nodes if nd.id not in owned and any(p in owned for p in nd.parents)]
        if cand:
            owned.add(rng.choice(cand))
    owned = sorted(owned)
    return {"id": f"s{n}", "date": "Oct 21 09:00", "class": cls, "class_title": wh.CLASS_TITLE[cls], "level": 100,
            "goal": "spells", "label": "Spell damage per second", "focus": None, "floors": {"hp": 9000.0},
            "toggles_on": ["Bullwhip"], "toggle_share": {"Coursing Restraints": 0.33},
            "numbers": {"goal": 12345.678, "spell": 12345.678, "melee": 0.0, "ehp": 54321.0, "hp": 9100.0, "spd": -7.0, "mr": 12.0},
            "items": [wynn_app.item_card(a, wh.SLOT_LABELS[i]) for i, a in enumerate(armor)] + [wynn_app.item_card(wpn, "Weapon")],
            "sp": {"assigned": [10, 20, 30, 40, 0], "extra": [5, 0, 0, 0, 1], "gear": [3, -2, 0, 0, 7]},
            "owned": owned, "order": tree.validate(frozenset(owned), 100)["order"],
            "link": "https://wynnbuilder.github.io/builder/#9_0Abc-_xyz", "settings": ["Party buffs: Warscream"], "rolls": "max"}


def check_sharing():
    """Share codes: a saved build for every class (real, powdered and crafted items) survives the trip exactly;
    codes pasted inside a message or broken over lines still open; damaged or tampered codes are refused or
    cleaned; a friend's build saves to the library once. On temporary copies, never your own files."""
    import base64
    import zlib
    import wynn_app
    import wynn_helper as wh
    from http.server import ThreadingHTTPServer
    wynn_app.STATE["data_checked"] = True
    d = wynn_app.game_data()

    def pack(p):          # a code made by hand (as a tampered one would be)
        raw = zlib.compress(json.dumps(p).encode(), 9)
        return wynn_app.SHARE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=").replace("_", ".")
    bad = []
    codes = {}
    for n, cls in enumerate(wh.CLASSES):
        try:
            e = _share_entry(wynn_app, wh, d, cls, n)
            code = wynn_app.share_code(e, "Sparky")
            codes[cls] = (e, code)
            for label, text in (("alone", code), ("in a message", f"yo try this {code} it's nuts"),
                                ("over lines", "\n".join(code[i:i + 50] for i in range(0, len(code), 50)) + "\nthanks")):
                b, err = wynn_app.decode_share(text)
                if b is None:
                    bad.append(f"{cls} {label}: {err}")
                    continue
                want = {"items": e["items"], "owned": e["owned"], "order": e["order"], "sp": e["sp"], "link": e["link"],
                        "settings": e["settings"], "floors": e["floors"], "toggles_on": e["toggles_on"],
                        "toggle_share": e["toggle_share"], "label": e["label"], "level": 100, "class": cls,
                        "numbers": {k: round(v, 2) for k, v in e["numbers"].items()}}
                diff = [k for k, v in want.items() if b[k] != v]
                if diff or b["shared_from"]["name"] != "Sparky" or b["shared_from"]["missing"]:
                    bad.append(f"{cls} {label}: {diff or b['shared_from']}")
            if "_" in code or len(code) > 1900:
                bad.append(f"{cls}: code has '_' (italics in Discord) or is too long ({len(code)})")
        except Exception as exc:
            bad.append(f"{cls}: {type(exc).__name__}: {exc}")
    check("share codes: every class, exact round trip", not bad, "; ".join(bad[:3]))

    e, code = codes["mage"]
    p = wynn_app.read_share_code(code)[0]
    refused = []
    for label, text in (("cut short", code[:-12]), ("garbled", code[:40] + "Q" + code[41:]), ("not a code", "hello"),
                        ("empty", wynn_app.SHARE_PREFIX), ("huge", pack({"v": 1, "pad": "0" * 5_000_000})),
                        ("bad class", pack(dict(p, c="paladin"))), ("bad items", pack(dict(p, i=[1, 2])))):
        if wynn_app.decode_share(text)[0] is not None:
            refused.append(label)
    newer = wynn_app.decode_share(pack(dict(p, v=2)))[1]
    t = dict(p, lk="javascript:alert(1)", nm="<b>x</b>" * 20, s=["ok", 5, "x" * 999],
             i=["Not A Real Item", ["CR-zzzz", "t6"]] + p["i"][2:], o=p["o"] + [99999, "x"], n={"goal": "1e999", "spell": 5})
    b, _ = wynn_app.decode_share(pack(t))
    cleaned = (b is not None and b["link"] == "" and len(b["shared_from"]["name"]) <= 40 and b["settings"] == ["ok", "x" * 200]
               and b["items"][0]["missing"] and b["items"][1]["missing"] and b["owned"] == e["owned"]
               and b["numbers"]["goal"] == 0.0 and b["numbers"]["spell"] == 5.0
               and b["shared_from"]["missing"] == ["Not A Real Item", "Crafted chestplate"]
               and b["items"][2:] == e["items"][2:])
    check("share codes: damaged ones refused, tampered ones cleaned", not refused and "newer version" in newer and cleaned,
          f"accepted: {refused}" if refused else ("" if cleaned else "tampered code not cleaned"))

    with tempfile.TemporaryDirectory() as tmp:
        saved = (wynn_app.LIBRARY_PATH, wynn_app.HISTORY_PATH)
        wynn_app.LIBRARY_PATH, wynn_app.HISTORY_PATH = os.path.join(tmp, "library.json"), os.path.join(tmp, "history.json")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wynn_app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        try:
            e, _code = codes["shaman"]
            wynn_app.save_history([e])
            ok = True
            r = call("/api/share", {"history_id": e["id"]})
            ok &= r["ok"] and r["code"].startswith("WBH1-")
            code = r["code"]
            o = call("/api/share/open", {"code": "here " + code})
            ok &= o["ok"] and o["build"]["items"] == e["items"] and "tree" in o["build"] and o["build"]["shared_from"]["name"] == ""
            ok &= not call("/api/share/open", {"code": "WBH1-nope"})["ok"]
            r = call("/api/library", {"action": "import", "code": code, "new_name": "Friend's shaman"})
            cid = r["character_id"]
            c = r["characters"][0]
            ok &= r["ok"] and c["name"] == "Friend's shaman" and c["class"] == "shaman" and len(c["builds"]) == 1
            ok &= c["builds"][0]["shared"] and c["current"] == c["builds"][0]["id"]
            r = call("/api/library", {"action": "import", "code": code, "character_id": cid})       # saved once only
            ok &= len(r["characters"][0]["builds"]) == 1
            bid = r["characters"][0]["builds"][0]["id"]
            full = call(f"/api/library/build?c={cid}&b={bid}")
            ok &= full["items"] == e["items"] and full["owned"] == e["owned"]
            r = call("/api/share", {"character_id": cid, "build_id": bid})                          # share it on
            ok &= r["ok"] and wynn_app.decode_share(r["code"])[0]["shared_from"]["name"] == "Friend's shaman"
            m = call("/api/library", {"action": "create", "class": "mage", "name": "Zap", "level": 50})
            r = call("/api/library", {"action": "import", "code": code, "character_id": m["character_id"]})
            ok &= not r["ok"] and "Shaman" in r["error"]
            ok &= not call("/api/share", {"history_id": "gone"})["ok"]
            check("share codes: open, save to library, share on", ok)
        except Exception as exc:
            check("share codes: open, save to library, share on", False, f"{type(exc).__name__}: {exc}")
        finally:
            srv.shutdown()
            wynn_app.LIBRARY_PATH, wynn_app.HISTORY_PATH = saved


def main():
    check_code()
    check_release_files()
    check_updater()
    check_server()
    check_library()
    check_sharing()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed.")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
