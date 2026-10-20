const fs = require('fs'); const vm = require('vm');
const R = process.env.WB_REPO + '/js/';
const ctx = {console, Math, Map, Array, Object, JSON, structuredClone, parseInt, parseFloat, isNaN, Number, String, Set};
vm.createContext(ctx);
// Pull only the definitions we need, so DOM-dependent code in these files isn't executed.
const bu = fs.readFileSync(R+'build_utils.js','utf8');
const pick = (src, re) => { const m = src.match(re); if (!m) throw new Error('missing '+re); return m[0]; };
vm.runInContext(pick(bu, /function skillPointsToPercentage[\s\S]*?\n}\n/), ctx);
vm.runInContext(pick(bu, /const skillpoint_damage_mult[^\n]*\n/).replace('const','var'), ctx);
vm.runInContext(pick(bu, /const skp_order[^\n]*\n/).replace('const','var'), ctx);
vm.runInContext(pick(bu, /const skp_elements[^\n]*\n/).replace('const','var'), ctx);
vm.runInContext(pick(bu, /const attackSpeeds[^\n]*\n/).replace('const','var'), ctx);
vm.runInContext(pick(bu, /const baseDamageMultiplier[^\n]*\n/).replace('const','var'), ctx);
const pw = fs.readFileSync(R+'powders.js','utf8');
vm.runInContext(pick(pw, /const damage_keys[^\n]*\n/).replace('const','var'), ctx);
vm.runInContext(pick(pw, /const damage_present_key[^\n]*\n/).replace('const','var'), ctx);
const dc = fs.readFileSync(R+'damage_calc.js','utf8');
vm.runInContext(pick(dc, /function calculateSpellDamage[\s\S]*?\n}\n/), ctx);
class DMap extends Map { get(k) { return super.has(k) ? super.get(k) : 0; } }
const cases = JSON.parse(fs.readFileSync('cases.json','utf8'));
let worst = 0, out = [];
for (const c of cases) {
  const stats = new DMap(Object.entries(c.stats));
  stats.set('damMult', new Map(Object.entries(c.dm)));
  const weapon = new DMap();
  const keys = ['nDam_','eDam_','tDam_','wDam_','fDam_','aDam_'];
  keys.forEach((k,i)=>weapon.set(k, c.dmg[i]));
  weapon.set('damagePresent', c.dmg.map(d=>d[1]>0));
  weapon.set('atkSpd', c.atk); weapon.set('tier','Rare');
  ctx.__args = [stats, weapon, c.conv, c.use_spell, c.ignore_speed, (c.pf === null ? undefined : c.pf), c.ignore_str, c.ign];
  const [norm, crit] = vm.runInContext('calculateSpellDamage(...__args)', ctx);
  const js = [(norm[0]+norm[1])/2, (crit[0]+crit[1])/2];
  for (let i=0;i<2;i++){ const d=Math.abs(js[i]-c.py[i]); const rel=d/Math.max(1,Math.abs(js[i])); worst=Math.max(worst,rel); }
  out.push(js);
}
console.log('cases', cases.length, 'worst relative difference', worst.toExponential(3));
