const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console, Math, Map}; vm.createContext(ctx);
const bu = fs.readFileSync(R + 'build_utils.js', 'utf8'); const ut = fs.readFileSync(R + 'utils.js', 'utf8');
const bg = fs.readFileSync(R + 'builder/builder_graph.js', 'utf8');
const pick = (src, re) => src.match(re)[0];
vm.runInContext(pick(bu, /function skillPointsToPercentage[\s\S]*?\n}\n/), ctx);
vm.runInContext(pick(bu, /const skillpoint_final_mult[^\n]*\n/).replace('const', 'var'), ctx);
vm.runInContext(pick(bu, /const skp_elements[^\n]*\n/).replace('const', 'var'), ctx);
vm.runInContext(pick(ut, /function rawToPct\(raw, pct\)[\s\S]*?\n}\n/), ctx);
vm.runInContext(pick(ut, /function rawToPctUncapped[\s\S]*?\n}\n/), ctx);
vm.runInContext(pick(bg, /function getDefenseStats[\s\S]*?\n}\n/), ctx);
const cm = {relik: 0.6, bow: 0.7, wand: 0.8, dagger: 1.0, spear: 1.0};
class DMap extends Map { get(k) { return super.has(k) ? super.get(k) : 0; } }
let worst = 0;
for (const c of JSON.parse(fs.readFileSync('ehp_cases.json', 'utf8'))) {
  const st = new DMap(Object.entries(c.stats)); st.set('classDef', cm[c.wtype]); st.set('defMult', new Map(Object.entries(c.dm)));
  const r = vm.runInContext('getDefenseStats', ctx)(st);
  const js = [r[0], r[1][0], r[1][1], r[2], r[5]];
  const rel = (a, b) => Math.abs(a - b) / Math.max(1, Math.abs(a));
  for (let i = 0; i < 4; i++) worst = Math.max(worst, rel(js[i], c.py[i]));
  for (let i = 0; i < 5; i++) worst = Math.max(worst, rel(js[4][i], c.py[4][i]));
}
console.log('worst relative difference', worst);
