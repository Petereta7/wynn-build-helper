const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console, Math, Map, Array, Object, JSON, Number}; vm.createContext(ctx);
const src = fs.readFileSync(R + 'powders.js', 'utf8');
vm.runInContext('var skp_elements = ["e","t","w","f","a"];', ctx);
vm.runInContext(src.match(/let powderIDs[\s\S]*?let powderStats = \[[\s\S]*?\];/)[0].replace(/let /g, 'var '), ctx);
vm.runInContext(src.match(/function calc_weapon_powder[\s\S]*?\n}\n/)[0], ctx);
const items = Object.fromEntries(JSON.parse(fs.readFileSync(process.env.WB_DATA + 'items.json','utf8')).items.map(i=>[i.name,i]));
const cases = JSON.parse(fs.readFileSync('powder_cases.json','utf8'));
let worst = 0;
for (const c of cases) {
  const it = items[c.name];
  const m = new Map([['powders', c.powders], ['nDam', it.nDam||'0-0'], ['eDam', it.eDam||'0-0'], ['tDam', it.tDam||'0-0'],
                     ['wDam', it.wDam||'0-0'], ['fDam', it.fDam||'0-0'], ['aDam', it.aDam||'0-0'], ['tier', it.tier]]);
  const [dmg] = vm.runInContext('calc_weapon_powder', ctx)(m);
  for (let i = 0; i < 6; i++) for (let j = 0; j < 2; j++) worst = Math.max(worst, Math.abs(dmg[i][j] - c.py[i][j]));
}
console.log('cases', cases.length, 'worst absolute difference', worst);
