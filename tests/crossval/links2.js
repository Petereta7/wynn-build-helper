const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const D = process.env.WB_DATA;
const ctx = {console, Math, Map, Set, Array, Object, JSON, Number, String, Error, RangeError, TypeError, Uint32Array, structuredClone, parseFloat, parseInt, isNaN,
             document: {getElementById: () => null, createElement: () => ({})}, window: {location: {hash: "", protocol: "https:", host: "wynnbuilder.github.io", pathname: "/builder/"}}};
ctx.navigator = {userAgent: "node", vendor: ""}; ctx.screen = {width: 1920}; ctx.addEventListener = () => {}; ctx.window.addEventListener = () => {};
vm.createContext(ctx);
for (const f of ['utils.js', 'build_utils.js', 'powders.js', 'craft.js', 'custom.js']) vm.runInContext(fs.readFileSync(R + f, 'utf8'), ctx, {filename: f});
vm.runInContext('var ENC = ' + fs.readFileSync('encoding_consts.json', 'utf8') + '; var wynn_version_id = 35; function assert(){}', ctx);
vm.runInContext(fs.readFileSync(R + 'builder/build_encode_decode.js', 'utf8'), ctx);
ctx.__ings = JSON.parse(fs.readFileSync(D + 'ingreds.json', 'utf8'));
ctx.__recipes = JSON.parse(fs.readFileSync(D + 'recipes.json', 'utf8')).recipes;
vm.runInContext(`var ingMap = new Map(), ingIDMap = new Map(), recipeMap = new Map(), recipeIDMap = new Map();
  ingMap.set("No Ingredient", {name: "No Ingredient", displayName: "No Ingredient", ids: {}, itemIDs: {"dura": 0, "strReq": 0, "dexReq": 0,"intReq": 0,"defReq": 0,"agiReq": 0}, consumableIDs: {"dura": 0, "charges": 0}, posMods: {"left": 0, "right": 0, "above": 0, "under": 0, "touching": 0, "notTouching": 0}, id: 4000});
  ingIDMap.set(4000, "No Ingredient");
  for (const ing of __ings) { ingMap.set(ing.displayName, ing); ingIDMap.set(ing.id, ing.displayName); }
  for (const r of __recipes) { recipeMap.set(r.name, r); recipeIDMap.set(r.id, r.name); }`, ctx);
const cases = JSON.parse(fs.readFileSync('link2_cases.json', 'utf8'));
let same = 0, diffs = [];
for (const c of cases) {
  ctx.__c = c;
  const js = vm.runInContext(`(function(){
    const mk = (e) => {
      if (e.craft) { const r = expandRecipe(recipeMap.get(e.craft.recipe)); const ings = e.craft.ings.map(id => expandIngredient(ingMap.get(ingIDMap.get(id))));
        const cr = new Craft(r, e.craft.mats, ings, e.craft.atk, ""); cr.setHash(encodeCraft(cr).toB64()); return cr; }
      if (e.custom) return decodeCustom({hash: e.custom});
      return {statMap: new Map(e.id === null ? [['NONE', true]] : [['id', e.id]])};
    };
    const eq = __c.equipment.map(mk);
    const build = {equipment: eq.slice(0, 8), weapon: eq[8], tomes: Array(14).fill(0).map(() => ({statMap: new Map([['NONE', true]])})), total_skillpoints: __c.auto, level: __c.level};
    const root = {ability: {id: __c.root}, children: __c.children[__c.root].map(x => ({ability: {id: x}, children: []}))};
    const state = new Map([[__c.root, {active: true}]]); for (const x of __c.children[__c.root]) state.set(x, {active: false});
    const aspects = []; for (let i = 0; i < 5; i++) aspects.push([{NONE: true}, 1]);
    return 'https://wynnbuilder.github.io/builder/#' + encodeBuild(build, __c.powders, __c.auto, [root], state, aspects).toB64();
  })()`, ctx);
  if (js === c.link) same++; else if (diffs.length < 3) diffs.push([js.slice(30, 120), c.link.slice(30, 120)]);
}
if (diffs.length) console.log(diffs);
console.log('identical links with crafted/custom items:', same, '/', cases.length);
