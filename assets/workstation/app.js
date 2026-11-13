(function () {
  'use strict';
  const clone = x => JSON.parse(JSON.stringify(x));
  const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stages = [
    {title:'剧本与分镜',ai:'扩写、资产、镜头与提示词',user:'定故事、看图、验收',ids:['story','storyboard']},
    {title:'自动生成视频',ai:'检查、提交、跟踪与下载',user:'接好自己的生成链路',ids:['configure','generate']},
    {title:'查错与剪辑',ai:'审片、有限重做、剪辑',user:'看表演、提修改、验收',ids:['review','edit','repair']},
    {title:'存规则，下次复用',ai:'复盘问题、读取已存规则',user:'确认经验与个人品味',ids:['rules']}
  ];
  const statuses = {planned:'待生成',prepared:'准备中',queued:'排队中',running:'生成中',submission_unknown:'提交待确认',generated:'待审片',reviewed:'审片通过',rejected:'需要修改',failed:'执行失败'};
  function validateState(s) {
    if (!s || s.schemaVersion !== 1 || !Array.isArray(s.projects) || !Array.isArray(s.rules) || !Array.isArray(s.profiles) || !s.config) throw Error('不是有效的工作区 JSON');
    const ids = new Set();
    s.projects.forEach(p => {
      if (!/^[a-zA-Z0-9_-]+$/.test(p.id) || ids.has(p.id)) throw Error('项目 ID 无效或重复');
      ids.add(p.id);
      ['assets','shots','jobs'].forEach(k => {if(!Array.isArray(p[k])) throw Error('项目缺少 '+k);});
      p.assets.forEach(a => {if(!safeRelative(a.path)) throw Error('素材路径必须位于工作目录内');});
    });
    [['maxJobsPerRun',100],['maxSecondsPerRun',3600],['maxAttempts',10]].forEach(([key,max]) => {if(!Number.isFinite(s.config[key]) || s.config[key]<1 || s.config[key]>max) throw Error(key+' 超出范围');});
    return true;
  }
  function safeRelative(p) {return typeof p==='string' && !/^(?:\/|\\|[a-zA-Z]:)/.test(p) && !p.split(/[\\/]/).includes('..') && !p.includes('\\');}
  function apiGraph(data) {
    if (!data || Array.isArray(data) || data.nodes) throw Error('这是画布 JSON，请从 ComfyUI 导出 API 格式');
    const g=data.prompt || data;
    if(!Object.keys(g).length || !Object.values(g).every(n=>n && typeof n.class_type==='string' && n.inputs && typeof n.inputs==='object')) throw Error('不是有效的 API 节点图');
    return g;
  }
  function slotsOf(g) {return Object.entries(g).flatMap(([id,n])=>Object.entries(n.inputs).filter(([,v])=>['string','number','boolean'].includes(typeof v)).map(([k,v])=>({slot:id+'.'+k,class:n.class_type,value:v})));}
  function makePrompt(bundle, state, id, pid) {
    const t=bundle.prompts.find(t=>t.id===id); if(!t) throw Error('未知模板');
    const p=state.projects.find(p=>p.id===pid) || {id:'请选择项目',name:'当前项目'};
    const c=state.config;
    const values={workspace:state.workspaceRoot || '你的工作目录',projectId:p.id,projectName:p.name,budget:`本批最多 ${c.maxJobsPerRun} 个任务、${c.maxSecondsPerRun} 秒，单镜最多尝试 ${c.maxAttempts} 次`};
    return t.body.replace(/\{\{(\w+)\}\}/g,(_,key)=>values[key]??'');
  }
  const core={esc,validateState,safeRelative,apiGraph,slotsOf,makePrompt};
  if(typeof module!=='undefined' && module.exports) module.exports=core;
  if(typeof document==='undefined') return;
  const bundle=window.MW_BUNDLE;
  let state=clone(window.MW_WORKSPACE || bundle.defaults), directory=null, dirty=false, formDirty=false, baseRevision=state.revision;
  let selectedProject=state.projects[0]?.id || '', assetProject='', importProfile='', currentStage=0;
  const $=sel=>document.querySelector(sel);
  const uid=prefix=>prefix+'-'+(crypto.randomUUID?crypto.randomUUID().replace(/-/g,''):Date.now().toString(36)+Math.random().toString(36).slice(2)).slice(0,12);
  let toastTimer;
  function toast(text){$('#toast').textContent=text;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),4200);}
  function error(e){toast(e.message || String(e));}
  async function copy(text){
    try{await navigator.clipboard.writeText(text);toast('已复制，回到你的 AI 助手发送');return true;}
    catch(_){const t=document.createElement('textarea');t.value=text;document.body.append(t);t.select();const ok=document.execCommand('copy');t.remove();if(ok){toast('已复制，回到你的 AI 助手发送');return true;}showDialog('复制内容',`<textarea class="code" rows="14">${esc(text)}</textarea><p class="dialog-note">当前浏览器不允许自动复制，请选中复制。</p>`);return false;}
  }
  async function fileIn(root,path,create=false){if(!safeRelative(path)) throw Error('无效文件路径');const parts=path.split('/');let dir=root;for(const p of parts.slice(0,-1))dir=await dir.getDirectoryHandle(p,{create});return dir.getFileHandle(parts.at(-1),{create});}
  async function readDisk(path){return JSON.parse(await (await (await fileIn(directory,path)).getFile()).text());}
  async function writeFile(path,text){const h=await fileIn(directory,path,true);const w=await h.createWritable();try{await w.write(text);await w.close();}catch(e){await w.abort().catch(()=>{});throw e;}}
  async function exists(path){try{await fileIn(directory,path);return true;}catch(e){if(e.name==='NotFoundError')return false;throw e;}}
  function rulesMarkdown(s){return '# 我的工作流个人规则\n\n'+s.rules.filter(r=>r.enabled!==false).map(r=>`## ${r.title}\n范围：${r.scope} ${r.projectId||''}\n${r.content}\n`).join('\n');}
  async function persist(next){
    validateState(next);
    if(directory){
      if(await exists('.mw.lock'))throw Error('AI 正在更新这个工作区，请稍后保存');
      const disk=await readDisk('workspace.json');
      if(disk.workspaceId!==state.workspaceId || disk.revision!==state.revision)throw Error('AI 已更新磁盘数据，请先刷新，避免覆盖');
      next.revision=state.revision+1;next.updatedAt=new Date().toISOString();
      await writeFile('workspace.json',JSON.stringify(next,null,2)+'\n');
      state=next;dirty=false;formDirty=false;baseRevision=next.revision;
      try{await writeFile('workspace.snapshot.js','window.MW_WORKSPACE = '+JSON.stringify(state).replace(/</g,'\\u003c')+';\n');await writeFile('rules.md',rulesMarkdown(state));}catch(_){toast('配置已存入磁盘；请让 AI refresh 更新辅助快照');}
    }else{
      if(!dirty)baseRevision=state.revision;
      next.revision=state.revision+1;next.updatedAt=new Date().toISOString();state=next;dirty=true;formDirty=false;
      toast('改动留在当前页面，请连接目录保存，或导出 JSON 交给 AI');
    }
    chrome();
  }
  async function change(fn){const n=clone(state);fn(n);await persist(n);render();}
  function chrome(){
    $('#workspace-path').textContent=state.workspaceRoot || '让 AI 启动工作台，或导入已有 workspace.json';
    $('#workspace-path').title=state.workspaceRoot||'';
    $('#save-status').textContent=directory?'已保存到本地':dirty?'未写入磁盘':'本地快照';
    $('#save-status').classList.toggle('pending',dirty);
    $('#connect-button').textContent=directory?'已连接 · '+directory.name:'连接工作目录';
    $('#storage-note').textContent=directory?'页面和 AI 共用同一份本地文件':dirty?'有未导出的改动 · AI 还不能读取':'连接工作目录后，可直接保存配置与规则';
    $('#recent-projects').innerHTML=state.projects.slice(-5).reverse().map(p=>`<a class="recent-item" href="#project/${encodeURIComponent(p.id)}/overview">${esc(p.name)}</a>`).join('');
    const nav=location.hash.split('/')[0].slice(1)||'projects';
    document.querySelectorAll('[data-nav]').forEach(a=>a.classList.toggle('active',a.dataset.nav===(nav==='project'?'projects':nav)));
  }
  async function connect(){
    if(!window.showDirectoryPicker){showDialog('连接本地目录',`<p class="dialog-note">这个浏览器没有目录读写接口。可用 Chrome / Edge 打开，或导入 JSON、编辑后导出给 AI。</p><p class="dialog-note">工作台本身不需要后台服务。</p>`);return;}
    const h=await showDirectoryPicker({id:'my-workflow',mode:'readwrite'});
    let data;
    try{const f=await h.getFileHandle('workspace.json');data=JSON.parse(await(await f.getFile()).text());validateState(data);}catch(_){throw Error('请选择 AI 已初始化的 MyWorkflow 目录，里面应有 workspace.json');}
    if(dirty){
      if(data.workspaceId!==state.workspaceId || data.revision!==baseRevision)throw Error('当前改动与所选目录版本不同，请先导出备份再刷新');
      const pending=clone(state);state=data;directory=h;await persist(pending);
    }else{directory=h;state=data;selectedProject=state.projects[0]?.id||'';}
    render();toast('已连接工作目录，AI 和页面读取相同文件');
  }
  function showDialog(title,body){$('#dialog-content').innerHTML=`<div class="dialog-head"><h2>${esc(title)}</h2><button class="close" aria-label="关闭" data-close>×</button></div>${body}`;$('#dialog').showModal();$('[data-close]').onclick=()=>$('#dialog').close();}
  function art(index){
    const colors=[['#e4e0ef','#b5a2ca','#715e87'],['#e1e9d4','#94af79','#516b4a'],['#f0e2cf','#c69e70','#87684d']];const [bg,mid,dark]=colors[index%3];
    return `<svg viewBox="0 0 440 220" aria-hidden="true"><rect width="440" height="220" fill="${bg}"/><circle cx="368" cy="42" r="80" fill="${mid}" opacity=".3"/><path d="M0 185Q100 130 210 180T440 160V220H0Z" fill="${mid}" opacity=".25"/><rect x="113" y="54" width="206" height="132" rx="8" fill="${dark}"/><path d="M99 60L130 30H300L333 60Z" fill="${mid}"/><rect x="133" y="73" width="71" height="113" rx="4" fill="${bg}"/><rect x="222" y="76" width="77" height="75" rx="4" fill="${bg}"/><path d="M228 113H294M260 82V145" stroke="${mid}" stroke-width="3"/><circle cx="192" cy="129" r="3" fill="${dark}"/><path d="M100 189H332" stroke="${dark}" stroke-width="5" stroke-linecap="round"/><path d="M72 184V130M72 152Q40 131 48 117Q80 119 72 152M72 166Q100 145 110 152Q105 175 72 166" stroke="${dark}" fill="${mid}" stroke-width="3"/><rect x="54" y="184" width="36" height="17" rx="4" fill="${mid}"/><path d="M340 181L354 115L372 181M346 153H367" stroke="${dark}" stroke-width="5" fill="none"/><rect x="340" y="178" width="44" height="10" rx="4" fill="${mid}"/></svg>`;
  }
  function heading(label,title,description,extra=''){return `<div class="page-heading"><div><span class="label">${esc(label)}</span><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${extra}</div>`;}
  function flow(active=0){return `<div class="workflow-strip">${stages.map((s,i)=>`<button class="stage-card ${active===i+1?'active':''}" data-stage="${i+1}"><span class="number">0${i+1}</span><h3>${s.title}</h3><div class="division"><b>AI</b>${s.ai}</div><div class="division user"><b>你</b>${s.user}</div></button>`).join('')}</div>`;}
  function projectOptions(){return `<option value="">选择一部剧</option>`+state.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===selectedProject?'selected':''}>${esc(p.name)}</option>`).join('');}
  function projectHome(){
    const all=state.projects.flatMap(p=>p.shots),generated=all.filter(s=>['generated','reviewed'].includes(s.status)).length;
    return heading('YOUR STORIES','把故事，变成下一部短剧','剧本、资产、分镜和成片，在一个地方接着做。',`<div class="stats"><div class="stat"><strong>${state.projects.length}</strong><span>部短剧</span></div><div class="stat"><strong>${all.length}</strong><span>个镜头</span></div><div class="stat"><strong>${generated}</strong><span>段已生成</span></div></div>`)+
    (state.projects.length?`<div class="project-grid">${state.projects.map((p,i)=>`<button class="project-card" data-project="${esc(p.id)}"><div class="project-art">${art(i)}<span class="cover-badge">${p.isDemo?'示例项目':'本地项目'}</span></div><div class="project-card-body"><h2>${esc(p.name)}</h2><p>${esc(p.logline||'从一个想法开始，和 AI 一起把它讲清楚。')}</p><div class="project-meta"><span>${p.shots.length} 镜 · ${p.assets.length} 项资产</span><span class="pill">${stages[(p.stage||1)-1]?.title||'剧本与分镜'}</span></div></div></button>`).join('')}<button class="new-card" data-new-project><span class="plus">＋</span><strong>开启一个新故事</strong><small>把想法交给你的 AI</small></button></div>`:`<div class="empty"><span class="label">START WITH AN IDEA</span><h2>你的第一个故事，从这里开始</h2><p>先确定想讲什么，再让 AI 把剧本、资产和分镜整理好。<br>这套工作台陪你把四个环节接起来。</p><button class="button dark" data-new-project>＋ 新建短剧</button><button class="button" data-demo>看看示例</button></div>`)+`<div class="section-title"><h2>四个环节，一直做下去</h2><span>每步都有对应的提示词</span></div>${flow()}`;
  }
  function projectPage(pid,tab){
    const p=state.projects.find(p=>p.id===pid);if(!p)return heading('PROJECT','项目不存在','请回到剧目库选择。');selectedProject=pid;
    let body=`<a class="back" href="#projects">← 返回剧目库</a>`+heading('PROJECT',p.name,p.logline||'故事、资产和镜头都保存在这个项目里。',`<span class="pill">${p.isDemo?'示例 · 尚未生成':'本地项目'}</span>`);
    body+=`<div class="tabs">${[['overview','项目总览'],['story','故事与剧本'],['assets','人物与场景'],['shots','镜头与提示词'],['jobs','生成与审片']].map(([id,label])=>`<button data-tab="${id}" class="${tab===id?'active':''}">${label}</button>`).join('')}</div>`;
    if(p.isDemo)body+='<div class="demo-note">示例用于了解组织方式。未提交 GPU 生成任务，也没有虚构的成片结果。</div>';
    if(tab==='overview'){
      const done=p.shots.filter(x=>x.status==='reviewed').length;
      body+=flow(p.stage||1)+`<div class="two-columns" style="margin-top:24px"><section class="panel"><span class="label">THE STORY</span><h2 style="margin-top:12px">先把故事讲清楚</h2><div class="story">${esc(p.story||p.logline||'写下一句话的想法，让 AI 和你一起扩写。')}</div><div class="form-actions"><button class="button small" data-tab="story">编辑故事</button><button class="button small mint" data-copy-prompt="story">复制本阶段提示词</button></div></section><section class="panel"><span class="label">PRODUCTION</span><h2 style="margin-top:12px">当前的制作进度</h2><div class="mini-metrics"><div class="mini-metric"><strong>${p.assets.length}</strong><small>人物 / 场景资产</small></div><div class="mini-metric"><strong>${p.shots.length}</strong><small>已规划镜头</small></div><div class="mini-metric"><strong>${done}</strong><small>审片通过</small></div></div><p class="overview-note">在你正在用的 AI 助手中执行。这里看故事、选素材、检查结果，留下下一次能复用的经验。</p></section></div>`;
      if(p.deliveries?.length)body+=`<div class="section-title"><h2>成片与预览</h2></div><div class="panel">${p.deliveries.map(d=>`<p><a class="clip-link" href="${mediaUrl(d.path)}" target="_blank" rel="noopener">打开成片 ↗</a> <small>${esc(d.path)}</small></p>`).join('')}</div>`;
    }else if(tab==='story')body+=`<section class="panel"><form id="story-form"><div class="field"><span>一句话故事</span><input name="logline" value="${esc(p.logline)}"></div><div class="field"><span>故事大纲 / 已确认剧本</span><textarea name="story" rows="13">${esc(p.story)}</textarea></div><div class="form-actions"><button class="button dark">保存故事</button><button type="button" class="button" data-copy-prompt="story">让 AI 接着扩写</button><button type="button" class="button" data-copy-prompt="storyboard">整理资产与分镜</button></div></form></section>`;
    else if(tab==='assets')body+=`<div class="section-title"><h2>人物、场景与参考素材</h2><button class="button mint" data-upload-assets>＋ 添加本地素材</button></div>`+(p.assets.length?`<div class="asset-grid">${p.assets.map(a=>`<article class="asset-card"><div class="asset-preview">${assetPreview(a)}</div><div class="asset-card-body"><span class="pill">${esc(kindName(a.kind))}</span><h3 style="margin-top:10px">${esc(a.title)}</h3><small>${esc(a.path)}</small><div style="margin-top:12px"><a class="clip-link" href="${mediaUrl(a.path)}" target="_blank" rel="noopener">查看原文件 ↗</a></div></div></article>`).join('')}</div>`:'<div class="empty"><h2>把人物与场景先定下来</h2><p>身份图、空景和构图参考各有用途。<br>让 AI 生成并登记到当前项目，或添加已有素材。</p><button class="button" data-copy-prompt="storyboard">复制资产与分镜提示词</button></div>');
    else if(tab==='shots')body+=`<div class="section-title"><h2>按镜头继续制作</h2><div><button class="button small" data-copy-prompt="generate">复制批量生成指令</button> <button class="button small mint" data-new-shot>＋ 添加镜头</button></div></div>`+(p.shots.length?`<div class="shot-list">${p.shots.map(s=>`<article class="shot-card"><div class="shot-top"><h3><span class="id">${esc(s.id)}</span>${esc(s.title)}</h3><div class="shot-meta"><span class="pill ${s.profile==='action'?'peach':''}">${s.profile==='action'?'动作':'文戏'}</span><span class="hint">${esc(s.seconds)} 秒</span><span class="pill lilac">${esc(statuses[s.status]||s.status)}</span><button class="text-button" data-edit-shot="${esc(s.id)}">编辑</button></div></div><div class="shot-body">${s.media?`<video src="${mediaUrl(s.media.path)}" controls preload="metadata"></video>`:''}<div class="shot-ref">参考素材：${s.referenceAssets?.map(id=>esc(p.assets.find(a=>a.id===id)?.title||id)).join(' · ')||'尚未指定'}</div><details><summary>查看完整生成提示词</summary><pre class="code-block">${esc(s.prompt||'尚未生成提示词。让 AI 读取官方 H3 指南后填写。')}</pre></details>${s.prompt?`<button class="text-button" data-copy-shot="${esc(s.id)}">复制提示词</button>`:''}${s.review?`<p class="hint">审片：${esc(s.review.note)} · ${esc(s.review.status)}</p>`:''}</div></article>`).join('')}</div>`:'<div class="empty"><h2>把故事拆成具体镜头</h2><p>每镜写清机位、人物、动作、台词与起止状态。</p><button class="button" data-copy-prompt="storyboard">复制分镜提示词</button></div>');
    else body+=`<div class="section-title"><h2>任务和审片记录</h2><div><button class="button small" data-copy-prompt="review">查错与重做</button> <button class="button small mint" data-copy-prompt="edit">剪辑并交预览</button></div></div>`+(p.jobs.length?`<section class="panel"><table class="job-table"><thead><tr><th>镜头</th><th>任务 ID</th><th>状态</th><th>产物</th></tr></thead><tbody>${p.jobs.map(j=>`<tr><td>${esc(j.shotId)}</td><td><code>${esc(j.promptId||j.id)}</code></td><td><span class="pill">${esc(statuses[j.status]||j.status)}</span></td><td>${j.files?.length||0} 个文件${j.reason?'<br>'+esc(j.reason):''}</td></tr>`).join('')}</tbody></table></section>`:'<div class="empty"><h2>等待第一个生成任务</h2><p>配置好真实工作流，把阶段指令发给 AI。<br>任务 ID、原片和审片结果会记录在这里。</p><button class="button" data-copy-prompt="configure">先检查连接与工作流</button></div>');
    return body;
  }
  function kindName(k){return ({character:'人物',scene:'场景',storyboard:'分镜参考',voice:'声音',video:'视频',reference:'参考素材'})[k]||k;}
  function mediaUrl(path){if(!safeRelative(path))return '#';return path.split('/').map(encodeURIComponent).join('/');}
  function assetPreview(a){const path=mediaUrl(a.path);if(/\.(png|jpe?g|webp|gif|svg)$/i.test(a.path))return `<img src="${path}" alt="${esc(a.title)}" loading="lazy">`;if(/\.(mp4|webm|mov)$/i.test(a.path))return `<video src="${path}" controls preload="metadata"></video>`;if(/\.(wav|mp3|flac|m4a)$/i.test(a.path))return `<audio src="${path}" controls preload="metadata"></audio>`;return '◇';}
  function promptsPage(){const list=currentStage?bundle.prompts.filter(p=>p.stage===currentStage):bundle.prompts;return heading('A PROMPT FOR EACH STEP','把下一步，交给你的 AI','模板会带上当前项目、工作目录和生成预算。')+`<div class="prompt-toolbar"><div class="tabs" style="margin:0;border:0;gap:20px">${[0,1,2,3,4].map(i=>`<button class="${currentStage===i?'active':''}" data-filter-stage="${i}">${i?'0'+i+' '+stages[i-1].title:'全部'}</button>`).join('')}</div><select id="prompt-project" aria-label="选择当前项目">${projectOptions()}</select></div><div class="prompt-grid">${list.map(t=>`<article class="prompt-card"><span class="pill">0${t.stage} ${stages[t.stage-1].title}</span><h3>${esc(t.title)}</h3><p>${esc(t.goal)}</p><button class="button small mint" data-copy-prompt="${t.id}">复制完整提示词 ↗</button> <button class="text-button" data-show-prompt="${t.id}">查看内容</button></article>`).join('')}</div>`;}
  function rulesPage(){return heading('GET BETTER EACH TIME','把经验，留下来','避坑经验和个人品味分开保存，下一个故事接着用。',`<button class="button dark" data-new-rule>＋ 添加规则</button>`)+`<div class="notice">个人规则保存在工作目录；Skill 更新时保留。每条规则可限定只作用于某一部剧。</div><div class="rule-grid">${state.rules.map(r=>`<article class="rule-card ${r.enabled===false?'disabled':''}"><div class="rule-top"><span class="pill ${r.category==='taste'?'lilac':''}">${r.category==='taste'?'个人品味':'避坑经验'}</span><small class="hint">${r.scope==='project'?esc(state.projects.find(p=>p.id===r.projectId)?.name||'项目规则'):'全局复用'}</small></div><h3>${esc(r.title)}</h3><p>${esc(r.content)}</p><div class="rule-actions"><button class="text-button" data-edit-rule="${esc(r.id)}">编辑</button><button class="text-button" data-toggle-rule="${esc(r.id)}">${r.enabled===false?'启用':'停用'}</button></div></article>`).join('')}</div><div class="section-title"><h2>Skill 内置的方法</h2><button class="text-button" data-handbook>查看完整说明 ↗</button></div><div class="flow-preview">${stages.map((s,i)=>`<div class="flow-mini"><span>0${i+1}</span><strong>${s.title}</strong><small>${s.ai}</small></div>`).join('')}</div>`;}
  function optionSlots(p,selected,type){const slots=p.slots || (p.workflowGraph?slotsOf(p.workflowGraph):[]);return '<option value="">未绑定</option>'+slots.filter(s=>type==='text'?typeof s.value==='string':type==='number'?typeof s.value==='number':true).map(s=>`<option value="${esc(s.slot)}" ${s.slot===selected?'selected':''}>${esc(s.slot)} · ${esc(s.class)}</option>`).join('');}
  function settingsPage(){const c=state.config;return heading('YOUR GENERATION SETUP','用你自己的生成链路','连接一次，把文戏、动作和预算配置好。')+`<div class="settings-layout"><form id="settings-form" class="config-stack"><section class="panel"><span class="label">COMFYUI</span><h2 style="margin-top:10px">连接与运行范围</h2><label class="field"><span>ComfyUI 基础地址</span><input name="comfyUrl" value="${esc(c.comfyUrl)}" placeholder="http://127.0.0.1:8188" required><small>填写你自己的本地或远程地址，不附带账号密码与密钥参数。</small></label><label class="field"><span>认证环境变量名（可选）</span><input name="tokenEnv" value="${esc(c.tokenEnv)}" placeholder="例如 COMFY_TOKEN"><small>只保存变量名，令牌放在助手的运行环境中。</small></label><div class="form-row"><label class="field"><span>每批最多几个任务</span><input name="maxJobsPerRun" type="number" min="1" max="100" value="${c.maxJobsPerRun}"></label><label class="field"><span>每批最多生成多少秒</span><input name="maxSecondsPerRun" type="number" min="1" max="3600" value="${c.maxSecondsPerRun}"></label></div><div class="form-row"><label class="field"><span>单镜最多尝试次数</span><input name="maxAttempts" type="number" min="1" max="10" value="${c.maxAttempts}"></label><label class="field"><span>剪辑方式</span><select name="editor"><option value="remotion" ${c.editor==='remotion'?'selected':''}>Remotion 可编辑工程</option><option value="ffmpeg" ${c.editor==='ffmpeg'?'selected':''}>FFmpeg 基础拼接</option></select></label></div></section><section class="panel"><span class="label">WORKFLOW PRESETS</span><h2 style="margin-top:10px">文戏与动作，分别设置</h2><p>导入你真正使用的 API 格式工作流。名字用于识别，节点图才决定执行内容。</p>${state.profiles.map(p=>`<div class="profile-panel"><div class="profile-head"><h3>${esc(p.name)}</h3><button type="button" class="button small" data-import-workflow="${esc(p.id)}">导入 API JSON</button></div><div class="field compact"><span>工作流名称</span><input name="${p.id}-name" value="${esc(p.workflowName)}" placeholder="例如：我使用的 H3 文戏工作流"></div><div class="workflow-file">${p.workflowPath||p.workflowGraph?'已导入 · '+esc(p.workflowPath||'内嵌 API 节点图'):'尚未导入工作流'}</div><label class="field compact"><span>提示词槽位</span><select name="${p.id}-prompt">${optionSlots(p,p.bindings.prompt,'text')}</select></label><div class="form-row"><label class="field compact"><span>时长槽位（秒）</span><select name="${p.id}-duration">${optionSlots(p,p.bindings.duration,'number')}</select></label><label class="field compact"><span>Seed 槽位</span><select name="${p.id}-seed">${optionSlots(p,p.bindings.seed,'number')}</select></label></div><label class="field compact"><span>原图固定秒数（未绑定时长时填写）</span><input name="${p.id}-fixed" type="number" min="0" max="60" step="0.001" value="${p.fixedSeconds||0}"></label><label class="field compact"><span>参考素材槽位</span><textarea class="code" name="${p.id}-refs" rows="3" placeholder='[{"slot":"12.image","index":0}]'>${esc(JSON.stringify(p.referenceBindings||[],null,2))}</textarea><small>index 从 0 起，对应每镜选择的参考素材顺序。</small></label><label class="field compact"><span>参数覆盖（例如 LoRA 开关）</span><textarea class="code" name="${p.id}-overrides" rows="3" placeholder='{"18.strength_model":0}'>${esc(JSON.stringify(p.overrides||{},null,2))}</textarea></label></div>`).join('')}</section><div class="form-actions"><button class="button dark">保存生成设置</button><button type="button" class="button" data-copy-prompt="configure">复制连接检查指令</button></div></form><aside class="config-stack"><section class="panel"><span class="label">A SMALL CHECKLIST</span><h2 style="margin-top:10px">让这套链路真正跑起来</h2><ol class="steps-list"><li>ComfyUI 已启动，当前助手可以访问。</li><li>导出并导入 API 格式 JSON。</li><li>绑定实际的提示词、时长与参考输入。</li><li>让 AI 先检查节点和权重。</li><li>明确授权范围后，再开始生成。</li></ol><div class="notice">检查与执行由你正在用的 AI 助手完成，工作台不会偷偷提交 GPU 任务。</div></section><section class="panel"><span class="label">MINIMAX H3</span><h2 style="margin-top:10px">提示词，跟着官方走</h2><p>基础模式和全参考模式使用不同结构。英文描述与准确中文台词分开。</p><a class="clip-link" href="${esc(c.guideUrl)}" target="_blank" rel="noopener">打开官方提示词指南 ↗</a><div class="divider"></div><p>让 AI 运行 <code>guide refresh</code> 保存最新入口和参考文本。刷新失败时保留上次成功缓存。</p><button class="button small" data-guide-command>复制更新指令</button></section><section class="panel"><span class="label">LOCAL FILES</span><h2 style="margin-top:10px">自己的数据，自己保管</h2><p>配置、剧目和规则：<code>workspace.json</code><br>素材：<code>projects/项目ID/</code><br>工作流：<code>workflows/</code><br>任务收据：<code>jobs/</code></p><p>Skill 更新只换安装文件，不覆盖工作目录。</p></section></aside></div>`;}
  function settingsData(form){
    const n=clone(state);if(!form)return n;const f=new FormData(form);
    const url=new URL(String(f.get('comfyUrl')));
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw Error('请填写不含密码或密钥参数的 HTTP(S) 地址');
    n.config={...n.config,comfyUrl:String(f.get('comfyUrl')).replace(/\/$/,''),tokenEnv:String(f.get('tokenEnv')).trim(),maxJobsPerRun:Number(f.get('maxJobsPerRun')),maxSecondsPerRun:Number(f.get('maxSecondsPerRun')),maxAttempts:Number(f.get('maxAttempts')),editor:f.get('editor')};
    for(const p of n.profiles){p.workflowName=String(f.get(p.id+'-name'));p.fixedSeconds=Number(f.get(p.id+'-fixed'));p.bindings={prompt:f.get(p.id+'-prompt'),duration:f.get(p.id+'-duration'),seed:f.get(p.id+'-seed')};p.referenceBindings=JSON.parse(String(f.get(p.id+'-refs')));p.overrides=JSON.parse(String(f.get(p.id+'-overrides')));if(!Array.isArray(p.referenceBindings)||Array.isArray(p.overrides)||typeof p.overrides!=='object'||!p.overrides)throw Error('参考槽位必须是数组，参数覆盖必须是对象');}
    return n;
  }
  function route(){const [name,id,tab]=location.hash.replace(/^#/,'').split('/');return {name:name||'projects',id:decodeURIComponent(id||''),tab:tab||'overview'};}
  function render(){chrome();const r=route();$('#main').innerHTML=r.name==='project'?projectPage(r.id,r.tab):r.name==='prompts'?promptsPage():r.name==='rules'?rulesPage():r.name==='settings'?settingsPage():projectHome();bind();}
  function promptDialog(id){const t=bundle.prompts.find(t=>t.id===id);const text=makePrompt(bundle,state,id,selectedProject);showDialog(t.title,`<pre class="code-block">${esc(text)}</pre><button class="button dark" id="dialog-copy">复制完整提示词</button>`);$('#dialog-copy').onclick=()=>copy(text);}
  function handbook(){showDialog('Skill 内置说明',`<select id="handbook-select">${bundle.handbook.map((x,i)=>`<option value="${i}">${esc(x.title)}</option>`).join('')}</select><pre class="code-block" id="handbook-text">${esc(bundle.handbook[0].body)}</pre>`);$('#handbook-select').onchange=e=>$('#handbook-text').textContent=bundle.handbook[Number(e.target.value)].body;}
  function newProject(){showDialog('开启一部新短剧',`<form id="new-project-form"><label class="field"><span>剧名</span><input name="name" placeholder="给这个故事取个名字" required maxlength="80"></label><label class="field"><span>一句话想法</span><textarea name="idea" placeholder="谁想做什么，遇到了什么？" rows="3"></textarea></label><div class="form-actions"><button class="button dark">创建项目</button><span class="hint">先有想法，其他交给 AI 一起打磨。</span></div></form>`);$('#new-project-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const id=uid('p');try{await change(s=>s.projects.push({id,name:String(f.get('name')).trim(),logline:String(f.get('idea')).trim(),story:'',stage:1,createdAt:new Date().toISOString(),assets:[],shots:[],jobs:[],deliveries:[]}));$('#dialog').close();location.hash='project/'+id+'/overview';}catch(err){error(err);}};}
  function ruleDialog(id){const r=state.rules.find(r=>r.id===id)||{id:uid('r'),title:'',content:'',category:'experience',scope:'global',projectId:'',enabled:true};showDialog(id?'编辑规则':'添加一条可复用规则',`<form id="rule-form"><label class="field"><span>一句话标题</span><input name="title" required value="${esc(r.title)}"></label><div class="form-row"><label class="field"><span>类型</span><select name="category"><option value="experience" ${r.category==='experience'?'selected':''}>避坑经验</option><option value="taste" ${r.category==='taste'?'selected':''}>个人品味</option></select></label><label class="field"><span>作用范围</span><select name="projectId"><option value="">所有剧，全局复用</option>${state.projects.map(p=>`<option value="${esc(p.id)}" ${p.id===r.projectId?'selected':''}>${esc(p.name)}</option>`).join('')}</select></label></div><label class="field"><span>具体做法</span><textarea name="content" rows="5" required>${esc(r.content)}</textarea></label><button class="button dark">保存规则</button></form>`);$('#rule-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const updated={...r,title:String(f.get('title')).trim(),content:String(f.get('content')).trim(),category:f.get('category'),projectId:f.get('projectId')||'',scope:f.get('projectId')?'project':'global'};try{await change(s=>{const i=s.rules.findIndex(x=>x.id===r.id);if(i>=0)s.rules[i]=updated;else s.rules.push(updated);});$('#dialog').close();}catch(err){error(err);}};}
  function shotDialog(sid){const p=state.projects.find(p=>p.id===selectedProject);if(!p)return;const s=p.shots.find(s=>s.id===sid)||{id:'S'+String(p.shots.length+1).padStart(2,'0'),title:'',profile:'dialogue',seconds:10,prompt:'',referenceAssets:[],status:'planned'};showDialog(sid?'编辑镜头 '+sid:'添加一个镜头',`<form id="shot-form"><div class="form-row"><label class="field"><span>稳定镜头 ID</span><input name="id" required pattern="[a-zA-Z0-9_-]+" value="${esc(s.id)}" ${sid?'readonly':''}></label><label class="field"><span>镜头标题</span><input name="title" value="${esc(s.title)}" required></label></div><div class="form-row"><label class="field"><span>生成预设</span><select name="profile"><option value="dialogue" ${s.profile==='dialogue'?'selected':''}>文戏</option><option value="action" ${s.profile==='action'?'selected':''}>动作</option></select></label><label class="field"><span>生成秒数</span><input name="seconds" type="number" min="1" max="60" step="0.001" value="${s.seconds}"></label></div><label class="field"><span>完整提示词</span><textarea class="code" name="prompt" rows="9">${esc(s.prompt)}</textarea></label><label class="field"><span>参考资产 ID（用逗号分隔，按输入顺序）</span><input name="refs" value="${esc((s.referenceAssets||[]).join(','))}"><small>${p.assets.map(a=>esc(a.id)+' '+esc(a.title)).join(' · ')||'先添加人物、场景或构图素材'}</small></label><button class="button dark">保存镜头</button></form>`);$('#shot-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const id=String(f.get('id'));const item={...s,id,title:String(f.get('title')),profile:f.get('profile'),seconds:Number(f.get('seconds')),prompt:String(f.get('prompt')),referenceAssets:String(f.get('refs')).split(',').map(x=>x.trim()).filter(Boolean),status:'planned'};delete item.review;try{if(!sid&&p.shots.some(x=>x.id===id))throw Error('这个镜头 ID 已存在');await change(data=>{const cur=data.projects.find(x=>x.id===p.id);const i=cur.shots.findIndex(x=>x.id===id);if(i>=0)cur.shots[i]=item;else cur.shots.push(item);});$('#dialog').close();}catch(err){error(err);}};}
  function bind(){
    document.querySelectorAll('[data-project]').forEach(b=>b.onclick=()=>location.hash='project/'+b.dataset.project+'/overview');
    document.querySelectorAll('[data-new-project]').forEach(b=>b.onclick=newProject);
    document.querySelectorAll('[data-copy-prompt]').forEach(b=>b.onclick=()=>copy(makePrompt(bundle,state,b.dataset.copyPrompt,selectedProject)));
    document.querySelectorAll('[data-show-prompt]').forEach(b=>b.onclick=()=>promptDialog(b.dataset.showPrompt));
    document.querySelectorAll('[data-filter-stage]').forEach(b=>b.onclick=()=>{currentStage=Number(b.dataset.filterStage);render();});
    document.querySelectorAll('[data-stage]').forEach(b=>b.onclick=()=>{currentStage=Number(b.dataset.stage);if(route().name==='project'){change(s=>{s.projects.find(p=>p.id===selectedProject).stage=currentStage;}).catch(error);}location.hash='prompts';});
    document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>location.hash='project/'+selectedProject+'/'+b.dataset.tab);
    if($('#prompt-project'))$('#prompt-project').onchange=e=>{selectedProject=e.target.value;};
    if($('#story-form'))$('#story-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{await change(s=>Object.assign(s.projects.find(p=>p.id===selectedProject),{logline:String(f.get('logline')),story:String(f.get('story'))}));toast(directory?'故事已保存':'故事已更新，请导出或连接目录保存');}catch(err){error(err);}};
    document.querySelectorAll('[data-new-rule]').forEach(b=>b.onclick=()=>ruleDialog());
    document.querySelectorAll('[data-edit-rule]').forEach(b=>b.onclick=()=>ruleDialog(b.dataset.editRule));
    document.querySelectorAll('[data-toggle-rule]').forEach(b=>b.onclick=()=>change(s=>{const r=s.rules.find(x=>x.id===b.dataset.toggleRule);r.enabled=r.enabled===false;}).catch(error));
    document.querySelectorAll('[data-handbook]').forEach(b=>b.onclick=handbook);
    document.querySelectorAll('[data-new-shot]').forEach(b=>b.onclick=()=>shotDialog());
    document.querySelectorAll('[data-edit-shot]').forEach(b=>b.onclick=()=>shotDialog(b.dataset.editShot));
    document.querySelectorAll('[data-copy-shot]').forEach(b=>b.onclick=()=>copy(state.projects.find(p=>p.id===selectedProject).shots.find(s=>s.id===b.dataset.copyShot).prompt));
    document.querySelectorAll('[data-upload-assets]').forEach(b=>b.onclick=()=>{assetProject=selectedProject;$('#asset-input').click();});
    document.querySelectorAll('[data-import-workflow]').forEach(b=>b.onclick=()=>{importProfile=b.dataset.importWorkflow;$('#workflow-input').click();});
    if($('#settings-form'))$('#settings-form').onsubmit=async e=>{e.preventDefault();try{await persist(settingsData(e.target));render();toast(directory?'生成设置已保存到本地':'设置已更新，请导出或连接目录保存');}catch(err){error(err);}};
    document.querySelectorAll('[data-guide-command]').forEach(b=>b.onclick=()=>copy(`使用我的工作流 Skill，读取 ${state.workspaceRoot||'工作目录'}，运行 guide refresh 更新官方 H3 提示词指南和参考文本；失败保留上次成功缓存，并显示来源、时间和哈希。`));
    if($('[data-demo]'))$('[data-demo]').onclick=()=>{const n=clone(state);const id=uid('p');n.projects.push({id,name:'山海修补铺',logline:'一间普通改衣店，藏着上古神话的秘密。',story:'示例大纲：日常生活与神话危机并行。先确认人物关系、冲突和情绪，再拆镜头。',stage:1,isDemo:true,assets:[],jobs:[],deliveries:[],shots:[{id:'S01',title:'改衣店开门',profile:'dialogue',seconds:10,prompt:'',referenceAssets:[],status:'planned'}]});persist(n).then(render).catch(error);};
  }
  $('#connect-button').onclick=()=>connect().catch(e=>{if(e.name!=='AbortError')error(e);});
  $('#new-project').onclick=newProject;$('#skill-button').onclick=handbook;
  $('#refresh-button').onclick=async()=>{try{if(formDirty||dirty){showDialog('先保留当前改动','<p class="dialog-note">页面里有未保存或未导出的改动。先保存或导出，再刷新查看 AI 的更新。</p>');return;}if(directory){state=await readDisk('workspace.json');validateState(state);render();$('#sync-banner').hidden=true;toast('已读取最新的本地数据');}else location.reload();}catch(e){error(e);}};
  $('#export-state').onclick=()=>{const blob=new Blob([JSON.stringify(state,null,2)+'\n'],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='workspace.updated.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);showDialog('把更新交给 AI',`<p class="dialog-note">已发起 JSON 下载。当前页面不会把下载当作磁盘保存成功。</p><pre class="code-block">${esc(`请使用我的工作流 Skill，把我提供的 workspace.updated.json 导入 ${state.workspaceRoot||'工作目录'}，期望磁盘版本为 ${baseRevision}。先核对版本，保留已有素材，导入后刷新工作台。`)}</pre>`);};
  $('#import-state').onclick=()=>$('#json-input').click();
  $('#json-input').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;if(dirty||formDirty)throw Error('请先导出当前改动，再导入其他配置');const s=JSON.parse(await file.text());validateState(s);state=s;directory=null;dirty=false;baseRevision=s.revision;selectedProject=s.projects[0]?.id||'';render();toast('已载入 JSON；后续修改请连接目录或导出');}catch(err){error(err);}e.target.value='';};
  $('#workflow-input').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;const graph=apiGraph(JSON.parse(await file.text()));const n=settingsData($('#settings-form'));const p=n.profiles.find(p=>p.id===importProfile);p.workflowGraph=graph;p.slots=slotsOf(graph);p.workflowName=file.name.replace(/\.json$/i,'');p.workflowPath='';p.bindings={prompt:'',duration:'',seed:''};p.referenceBindings=[];p.overrides={};await persist(n);render();toast('工作流已载入，请选择真实节点槽位并保存');}catch(err){error(err);}e.target.value='';};
  $('#asset-input').onchange=async e=>{try{if(!directory)throw Error('先连接工作目录，再添加素材，确保 AI 能读取同一文件');const files=[...e.target.files];if(!files.length)return;const added=[];for(const file of files){const id=uid('a');const ext=file.name.includes('.')?'.'+file.name.split('.').pop().replace(/[^a-zA-Z0-9]/g,'').toLowerCase():'';const path=`projects/${assetProject}/assets/${id}${ext}`;const h=await fileIn(directory,path,true);const out=await h.createWritable();await out.write(file);await out.close();const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(x=>x.toString(16).padStart(2,'0')).join('');added.push({id,title:file.name,kind:file.type.startsWith('video')?'video':file.type.startsWith('audio')?'voice':'reference',path,sha256:hash});}await change(s=>s.projects.find(p=>p.id===assetProject).assets.push(...added));toast('素材已保存到项目目录');}catch(err){error(err);}e.target.value='';};
  document.addEventListener('input',e=>{if(e.target.closest('form'))formDirty=true;});
  let lastRoute=location.hash;
  window.addEventListener('hashchange',()=>{
    if(formDirty && !confirm('表单还有未保存的内容，确定离开这一页吗？')){
      history.replaceState(null,'',lastRoute || '#projects');return;
    }
    formDirty=false;lastRoute=location.hash;render();
  });
  window.addEventListener('beforeunload',e=>{if(dirty||formDirty){e.preventDefault();e.returnValue='';}});
  setInterval(async()=>{if(!directory || document.hidden || $('#dialog').open)return;try{const disk=await readDisk('workspace.json');if(disk.revision!==state.revision){if(formDirty){$('#sync-banner').textContent='AI 已更新项目。先保存或保留表单内容，再刷新工作台。';$('#sync-banner').hidden=false;}else{state=disk;render();}}}catch(_){}},4000);
  // Public read-only helpers make browser diagnostics possible without a backend.
  window.MyWorkflowApp={getState:()=>clone(state),core};
  render();
})();
