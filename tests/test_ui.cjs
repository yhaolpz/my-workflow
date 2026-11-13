const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');const path=require('node:path');
const core=require('../assets/workstation/app.js');
const context={window:{}};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../assets/workstation/bundle.js'),'utf8'),context);
const bundle=context.window.MW_BUNDLE;
const state=JSON.parse(JSON.stringify(bundle.defaults));state.workspaceRoot='C:\\MyWorkflow';state.projects.push({id:'p-1',name:'我的剧',assets:[],shots:[],jobs:[]});
assert.equal(core.validateState(state),true);
assert.throws(()=>core.apiGraph({nodes:[]}));
assert.equal(core.safeRelative('../private'),false);assert.equal(core.safeRelative('C:\\private'),false);
assert.equal(core.safeRelative('projects/p-1/assets/a.png'),true);
assert.equal(core.esc('<img src=x onerror=alert(1)>'),'&lt;img src=x onerror=alert(1)&gt;');
for(const t of bundle.prompts){const text=core.makePrompt(bundle,state,t.id,'p-1');assert.ok(text.includes('p-1'));assert.ok(text.includes('C:\\MyWorkflow'));assert.ok(!text.includes('{{'));}
assert.equal(core.slotsOf({'1':{class_type:'A',inputs:{text:'x',image:['2',0]}}}).length,1);
console.log('UI data contract, template context, path and escaping checks passed');
