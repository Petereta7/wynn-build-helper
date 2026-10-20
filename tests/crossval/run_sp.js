const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console: {log(){}}, Math, Map, Array, Object, JSON, Infinity, performance: {now: () => 0}};
vm.createContext(ctx);
vm.runInContext('var skp_order = ["str","dex","int","def","agi"];', ctx);
vm.runInContext(fs.readFileSync(R + 'skillpoints.js', 'utf8').replace(/const /g, 'var '), ctx);
const cases = JSON.parse(fs.readFileSync('sp_cases.json', 'utf8'));
const mk = (o) => { const m = new Map([['skillpoints', o.bonus], ['reqs', o.reqs], ['set', null], ['crafted', !!o.crafted]]); return m; };
let mismatch = 0, ties = 0, examples = [];
for (const [i, c] of cases.entries()) {
  const eq = c.armor.map(mk); while (eq.length < 9) eq.push(mk({reqs:[0,0,0,0,0], bonus:[0,0,0,0,0]})); const w = mk(c.weapon);
  const res = vm.runInContext('calculate_skillpoints', ctx)(eq, w);
  // returns [best_order, best_skillpoints, final_skillpoints, best_total, best_activeSetCounts]
  const jsTotal = res[3], jsFinal = res[2];
  if (jsTotal !== c.py_total) {
    mismatch++; if (examples.length < 5) examples.push({i, js: jsTotal, py: c.py_total, jsFinal, pyFinal: c.py_final});
  } else if (JSON.stringify(jsFinal) !== JSON.stringify(c.py_final)) {
    ties++;   // same number of assigned points, equally good equip order
  }
}
if (examples.length) console.log(JSON.stringify(examples));
console.log('identical:', cases.length - mismatch - ties, '/', cases.length, '; ties (same total points):', ties, '; mismatches:', mismatch);
