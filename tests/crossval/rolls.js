const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console, Math, Map, Array, Object, JSON}; vm.createContext(ctx);
const bu = fs.readFileSync(R + 'build_utils.js', 'utf8');
const pick = (re) => bu.match(re)[0];
vm.runInContext(pick(/let nonRolledIDs = \[[\s\S]*?\];/).replace('let', 'var'), ctx);
vm.runInContext(pick(/let rolledIDs = \[[\s\S]*?\];/).replace('let', 'var'), ctx);
vm.runInContext(pick(/let reversedIDs[^\n]*/).replace('let', 'var'), ctx);
vm.runInContext(pick(/function idRound[\s\S]*?\n}\n/), ctx);
vm.runInContext(pick(/function expandItem[\s\S]*?\n}\n/), ctx);
const items = JSON.parse(fs.readFileSync(process.env.WB_DATA + 'items.json', 'utf8')).items;
const py = JSON.parse(fs.readFileSync('py_rolls.json', 'utf8'));
let checked = 0, bad = [];
for (const it of items) {
  if (!py[it.name]) continue;
  const ex = vm.runInContext('expandItem', ctx)(it);
  for (const [k, v] of ex.get('maxRolls')) {
    if (v === 0 && !(k in py[it.name].max)) continue;
    checked++;
    const pmax = py[it.name].max[k] ?? 0, pmin = py[it.name].min[k] ?? 0;
    if (pmax !== v || pmin !== ex.get('minRolls').get(k)) bad.push([it.name, k, v, pmax, ex.get('minRolls').get(k), pmin]);
  }
}
if (bad.length) console.log(JSON.stringify(bad.slice(0, 8)));
console.log('identical:', checked - bad.length, '/', checked, 'stat rolls');
