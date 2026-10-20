const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const D = process.env.WB_DATA;
const ctx = {console, Math, Map, Set, Array, Object, JSON, Number, String, Error, RangeError, TypeError, Uint32Array, structuredClone, parseFloat, parseInt, isNaN,
             document: {getElementById: () => null, createElement: () => ({})}, window: {location: {hash: "", protocol: "https:", host: "wynnbuilder.github.io", pathname: "/builder/"}}};
ctx.navigator = {userAgent: "node", vendor: ""}; ctx.screen = {width: 1920}; ctx.addEventListener = () => {}; ctx.window.addEventListener = () => {};
vm.createContext(ctx);
for (const f of ['utils.js', 'build_utils.js', 'powders.js', 'craft.js', 'custom.js']) vm.runInContext(fs.readFileSync(R + f, 'utf8'), ctx, {filename: f});
ctx.__ings = JSON.parse(fs.readFileSync(D + 'ingreds.json', 'utf8'));
ctx.__recipes = JSON.parse(fs.readFileSync(D + 'recipes.json', 'utf8')).recipes;
vm.runInContext(`
var ingMap = new Map(), ingIDMap = new Map(), recipeMap = new Map(), recipeIDMap = new Map();
(function(){
  let ing = {name: "No Ingredient", displayName: "No Ingredient", tier: 0, lvl: 0, skills: [], ids: {},
    itemIDs: {"dura": 0, "strReq": 0, "dexReq": 0,"intReq": 0,"defReq": 0,"agiReq": 0,},
    consumableIDs: {"dura": 0, "charges": 0}, posMods: {"left": 0, "right": 0, "above": 0, "under": 0, "touching": 0, "notTouching": 0}, id: 4000};
  ingMap.set(ing.displayName, ing); ingIDMap.set(ing.id, ing.displayName);
  let powder_ing_info = [[-35,0],[-52.5,0],[-70,10],[-91,20],[-112,28],[-133,36], [-154,44]];
  for (let i = 0; i < 5; i ++) for (let powder_tier = 0; powder_tier < POWDER_TIERS; ++powder_tier) {
    let powder_info = powder_ing_info[powder_tier];
    let ing = {name: "" + damageClasses[i+1] + " Powder " + ROMAN_NUMERAL_MAP.get(powder_tier + 1), tier: 0, lvl: 0, skills: [], ids: {}, isPowder: true,
      pid: POWDER_TIERS*i + powder_tier, itemIDs: {"dura": powder_info[0], "strReq": 0, "dexReq": 0,"intReq": 0,"defReq": 0,"agiReq": 0},
      consumableIDs: {"dura": 0, "charges": 0}, posMods: {"left": 0, "right": 0, "above": 0, "under": 0, "touching": 0, "notTouching": 0}};
    ing.id = 4001 + ing.pid; ing.displayName = ing.name; ing.itemIDs[skp_order[i] + "Req"] = powder_info[1];
    ingMap.set(ing.displayName, ing); ingIDMap.set(ing.id, ing.displayName);
  }
  for (const ing of __ings) { ingMap.set(ing.displayName, ing); ingIDMap.set(ing.id, ing.displayName); }
  for (const r of __recipes) { recipeMap.set(r.name, r); recipeIDMap.set(r.id, r.name); }
})();`, ctx);
const cases = JSON.parse(fs.readFileSync('craft_cases.json', 'utf8'));
let ok = 0, bad = [];
const near = (a, b) => Math.abs(a - b) < 1e-6;
for (const c of cases) {
  ctx.__c = c;
  const res = vm.runInContext(`(function(){
    const recipe = expandRecipe(recipeMap.get(__c.recipe));
    const ings = __c.ings.map(id => expandIngredient(ingMap.get(ingIDMap.get(id))));
    const craft = new Craft(recipe, __c.mats, ings, __c.atk, "");
    const sm = craft.statMap;
    const out = {reqs: sm.get("reqs"), sp: sm.get("skillpoints"), min: {}, max: {}, slots: sm.get("slots"), hash: encodeCraft(craft).toB64()};
    for (const [k, v] of sm.get("maxRolls")) if (v) out.max[k] = v;
    for (const [k, v] of sm.get("minRolls")) if (v) out.min[k] = v;
    for (const k of ["hp", "eDef", "tDef", "wDef", "fDef", "aDef"]) { const v = sm.get(k); if (typeof v === "number" && v) { out.max[k] = (out.max[k]||0) + v; out.min[k] = (out.min[k]||0) + v; } }
    if (sm.get("category") === "weapon") {
      sm.set("powders", __c.powders);
      apply_weapon_powders(sm);
      out.damage = damage_keys.map(k => sm.get(k)[1]);
    }
    return out;
  })()`, ctx);
  const errs = [];
  if (JSON.stringify(res.reqs) !== JSON.stringify(c.reqs)) errs.push(['reqs', res.reqs, c.reqs]);
  if (JSON.stringify(res.sp) !== JSON.stringify(c.bonus)) errs.push(['sp', res.sp, c.bonus]);
  if (res.slots !== c.slots) errs.push(['slots', res.slots, c.slots]);
  for (const key of ['min', 'max']) {
    const a = res[key], b = {}; for (const [k, v] of Object.entries(c[key])) if (v) b[k] = v;
    for (const s of ['str','dex','int','def','agi']) delete a[s];
    const ks = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of ks) if (!near(a[k] || 0, b[k] || 0)) errs.push([key, k, a[k], b[k]]);
  }
  if (c.damage) for (let i = 0; i < 6; i++) for (let j = 0; j < 2; j++) if (!near(res.damage[i][j], c.damage[i][j])) errs.push(['dmg', i, j, res.damage[i][j], c.damage[i][j]]);
  if (res.hash !== c.hash) errs.push(['hash', res.hash, c.hash]);
  if (errs.length === 0) ok++; else if (bad.length < 5) bad.push([c.recipe, errs.slice(0, 4)]);
}
if (bad.length) console.log(JSON.stringify(bad));
console.log('crafted items identical:', ok, '/', cases.length);
