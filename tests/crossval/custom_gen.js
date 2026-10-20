const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console, Math, Map, Set, Array, Object, JSON, Number, String, Error, RangeError, TypeError, Uint32Array, structuredClone, parseFloat, parseInt, isNaN,
             document: {getElementById: () => null, createElement: () => ({})}, window: {location: {hash: "", protocol: "https:", host: "wynnbuilder.github.io", pathname: "/builder/"}}};
ctx.navigator = {userAgent: "node", vendor: ""}; ctx.screen = {width: 1920}; ctx.addEventListener = () => {}; ctx.window.addEventListener = () => {};
vm.createContext(ctx);
for (const f of ['utils.js', 'build_utils.js', 'powders.js', 'craft.js', 'custom.js']) vm.runInContext(fs.readFileSync(R + f, 'utf8'), ctx, {filename: f});
let seed = 11; const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
const ri = (a, b) => a + Math.floor(rnd() * (b - a + 1));
const pick = a => a[Math.floor(rnd() * a.length)];
const names = ["Sky Blade", "Ångström Wand", "Test", "Fire & Ice", "Möbius-Ring", "x", "Kaïros", "The  Long Name Of Doom"];
const types = ["helmet","chestplate","leggings","boots","ring","bracelet","necklace","wand","spear","bow","dagger","relik"];
const rolled = ["hprPct","mr","sdPct","mdPct","ls","ms","spd","poison","hpBonus","hprRaw","sdRaw","mdRaw","fDamPct","eDefPct","spPct1","spRaw3","rSdRaw","critDamPct","damRaw","rDamPct","nDamRaw","healPct","maxMana"];
const out = [];
for (let n = 0; n < 800; n++) {
  ctx.__s = {name: pick(names), type: pick(types), fix: rnd() < 0.4, tier: pick(["Normal","Unique","Rare","Legendary","Fabled","Mythic","Set"]),
    cls: pick(["Warrior","Assassin","Mage","Archer","Shaman"]), atk: pick(["SUPER_SLOW","SLOW","NORMAL","FAST","SUPER_FAST"]),
    lvl: ri(1, 120), hp: ri(-500, 5000), slots: ri(0, 4), reqs: [ri(0,120),ri(0,60),0,ri(0,90),ri(0,10)], sp: [ri(-20,20),0,ri(0,15),ri(-5,5),0],
    defs: [ri(-200,200),0,ri(0,300),ri(-50,0),5], dam: [[ri(0,100),ri(100,300)], [0,0], [ri(10,50), ri(50,90)], [0,0], [0,0], [ri(0,5), ri(5,9)]],
    rolls: rolled.filter(() => rnd() < 0.35).map(k => { const a = ri(-300, 300); return [k, a, a + ri(0, 200)]; })};
  const r = vm.runInContext(`(function(){
    const s = __s; const m = new Map(); m.set("minRolls", new Map()); m.set("maxRolls", new Map());
    m.set("name", s.name); m.set("type", s.type); m.set("tier", s.tier); m.set("lvl", s.lvl); m.set("hp", s.hp); m.set("slots", s.slots);
    if (s.fix) m.set("fixID", true);
    const el = ["n","e","t","w","f","a"];
    if (["wand","spear","bow","dagger","relik"].includes(s.type)) { m.set("atkSpd", s.atk); m.set("classReq", s.cls); el.forEach((e, i) => m.set(e + "Dam", s.dam[i].join("-"))); }
    ["str","dex","int","def","agi"].forEach((k, i) => { m.set(k + "Req", s.reqs[i]); m.set(k, s.sp[i]); });
    ["e","t","w","f","a"].forEach((e, i) => m.set(e + "Def", s.defs[i]));
    for (const [k, a, b] of s.rolls) { m.get("minRolls").set(k, a); m.get("maxRolls").set(k, s.fix ? a : b); }
    const c = new Custom(m);
    const hash = encodeCustom(c, true).toB64();
    const back = decodeCustom({hash: hash});
    const exp = {name: back.statMap.get("displayName"), max: {}, min: {}};
    for (const [k, v] of back.statMap.get("maxRolls")) if (v) exp.max[k] = v;
    for (const [k, v] of back.statMap.get("minRolls")) if (v) exp.min[k] = v;
    return {hash, exp};
  })()`, ctx);
  out.push({spec: ctx.__s, hash: r.hash, exp: r.exp});
}
fs.writeFileSync('custom_cases.json', JSON.stringify(out));
console.log('wrote', out.length);
