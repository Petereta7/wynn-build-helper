const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console, Math, Map, Set, Array, Object, JSON, Number, String, Error, RangeError, TypeError, Uint32Array,
             document: {getElementById: () => null}, window: {location: {hash: "", protocol: "https:", host: "wynnbuilder.github.io", pathname: "/builder/"}}};
ctx.navigator = {userAgent: "node", vendor: ""}; ctx.screen = {width: 1920}; ctx.addEventListener = () => {}; ctx.window.addEventListener = () => {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(R + 'utils.js', 'utf8'), ctx);
vm.runInContext('var ENC = ' + fs.readFileSync('encoding_consts.json', 'utf8') + '; var POWDER_TIERS = 7; var wynn_version_id = 35;', ctx);
const pw = fs.readFileSync(R + 'powders.js', 'utf8');
vm.runInContext(pw.match(/function encodePowderIdx[\s\S]*?\n}\n/)[0], ctx);
vm.runInContext('function assert(){}', ctx);
let src = fs.readFileSync(R + 'builder/build_encode_decode.js', 'utf8');
vm.runInContext(src, ctx);
const cases = JSON.parse(fs.readFileSync('link_cases.json', 'utf8'));
const item = (id) => ({statMap: new Map(id === null ? [['NONE', true]] : [['id', id]])});
let same = 0, diffs = [];
for (const c of cases) {
  const eq = c.equipment.map(item);
  const build = {equipment: eq.slice(0, 8), weapon: eq[8], tomes: c.tomes.map(item), total_skillpoints: c.auto, level: c.level};
  const nodes = {}; for (const k of Object.keys(c.children)) nodes[k] = {ability: {id: Number(k)}, children: []};
  for (const [k, ch] of Object.entries(c.children)) nodes[k].children = ch.map(x => nodes[x]);
  const atree = [nodes[c.root]];
  const state = new Map(Object.keys(nodes).map(k => [Number(k), {active: c.owned.includes(Number(k))}]));
  const aspects = []; for (let i = 0; i < 5; i++) aspects.push(i < c.aspects.length ? [{id: c.aspects[i][0]}, c.aspects[i][1]] : [{NONE: true}, 1]);
  ctx.__a = [build, c.powders, c.final, atree, state, aspects];
  const vec = vm.runInContext('encodeBuild(...__a)', ctx);
  const js = 'https://wynnbuilder.github.io/builder/#' + vec.toB64();
  if (js === c.link) same++; else if (diffs.length < 3) diffs.push([c.cls, js.slice(-40), c.link.slice(-40)]);
}
if (diffs.length) console.log(JSON.stringify(diffs));
console.log('identical links', same, '/', cases.length);
