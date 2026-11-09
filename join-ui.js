'use strict';
const J={jobs:[],resources:[],draft:null,revision:null,nonce:null};
const joinJobStates={queued:'等待执行',running:'正在加群',waiting:'等待限流解除',paused:'已暂停',cancelled:'已取消',completed:'处理完成',completed_with_errors:'处理完成，有异常'};
const joinTargetStates={pending:'待处理',joining:'正在加入',joined:'已加入',already_joined:'原已在群',requested:'已提交申请 · 待审批',failed:'失败',uncertain:'结果待核对',cancelled:'已取消'};
function joinMetrics(){return metric('活动任务',fmt(J.jobs.filter(j=>['queued','running','waiting'].includes(j.status)).length),'最近 100 条任务','clock')+metric('已确认在群',fmt(J.jobs.reduce((n,j)=>n+j.joined+j.already_joined,0)),'已加入及原已在群的目标记录','users')+metric('等待审批',fmt(J.jobs.reduce((n,j)=>n+j.requested,0)),'申请已提交，不代表入群成功','file')+metric('需要检查',fmt(J.jobs.reduce((n,j)=>n+j.failed+j.uncertain,0)),'明确失败或结果待核对','info');}
function joinRows(){return J.jobs.length?table(J.jobs.map(j=>'<tr><td><strong>'+esc(j.name)+'</strong><div class="small muted">'+esc(j.account_name||'账号已移除')+' · 间隔 '+j.interval_seconds+' 秒</div><div class="resource-desc">'+esc(j.last_error)+'</div></td><td>'+badge(joinJobStates[j.status],j.status==='completed'?'green':['paused','completed_with_errors'].includes(j.status)?'amber':'purple')+'</td><td><strong>'+fmt(j.joined+j.already_joined)+' / '+j.total+'</strong><div class="small muted">待审批 '+j.requested+' · 未处理 '+j.pending+'<br>失败 '+j.failed+' · 待核对 '+j.uncertain+'</div></td><td><div class="actions">'+ob('join-detail','详情',j.id)+(['queued','running','waiting'].includes(j.status)?ob('join-pause','暂停',j.id):'')+(j.status==='paused'?ob('join-resume','继续',j.id):'')+(j.failed&&['paused','completed_with_errors'].includes(j.status)?ob('join-retry','重试失败',j.id):'')+(['queued','running','waiting','paused'].includes(j.status)?ob('join-cancel','取消',j.id,'sm danger'):'')+'</div></td></tr>').join(''),['任务与账号','状态','结果','操作']):empty('还没有加群任务','粘贴群链接，或从资源库选择群组后预览创建。','','users');}
function joinResourceChips(){return J.resources.length?'<p>已从资源库选择 '+J.resources.length+' 个目标。频道和缺少有效链接的资源会在预览中排除。</p><div class="target-list">'+J.resources.slice(0,12).map(id=>'<span class="target-chip">'+esc(S.selected.get(id)?.title||'ID '+id)+'</span>').join('')+'</div>'+button('join-clear-resources','清空资源选择','sm'):'<p class="small muted">也可在资源库勾选群组，点击“创建加群任务”。</p>';}
async function joinPage(){
 const d=await api('/api/join-jobs');J.jobs=d.items;
 const options=S.workspace.accounts.filter(a=>a.status==='connected').map(a=>[a.id,a.name+' · '+a.phone]);
 return pageHead('自动加群','按账号依次加入你指定的群组，查看每个目标的实际结果。',button('join-refresh',icon('refresh')+' 刷新')+link('/group-search','从资源库选群','button'))+
 '<div id="joinMetrics" class="metrics">'+joinMetrics()+'</div>'+notice('每个任务最多 100 个目标，默认间隔 60 秒。Telegram 要求等待时自动延后；需审批的群仅提交一次申请，可在详情中核对是否获批。加群与消息任务分别创建。')+
 card('创建加群任务',options.length?'<div class="operation-body">'+opForm('joinForm',field('name','任务名称','我的加群任务','required maxlength="100"')+'<div class="op-form-grid">'+select('account_id','使用账号',options,options[0][0])+field('interval_seconds','目标间隔（秒）',60,'type="number" min="30" max="3600" required')+'</div>'+area('text','群链接或 @用户名（每行一个）','','maxlength="52000" rows="6" placeholder="https://t.me/group_name&#10;https://t.me/+邀请代码&#10;@group_name"')+'<div id="joinResources">'+joinResourceChips()+'</div>','预览加群目标')+'</div>':empty('先连接一个 Telegram 账号','连接后即可预览和创建加群任务。',link('/telegram-accounts','连接账号','button primary'),'phone'))+
 card('加群执行记录','<div id="joinRows">'+joinRows()+'</div>');
}
function joinPreview(d){
 J.revision=d.revision;
 const account=S.workspace.accounts.find(a=>a.id===d.account_id);
 modal('确认加群任务',notice(esc(d.name)+' · '+d.targets.length+' 个有效目标 · 每个目标间隔 '+d.interval_seconds+' 秒')+'<p class="small">使用账号：<strong>'+esc(account?.name||'')+'</strong> · '+esc(account?.phone||'')+'</p>'+
 '<div class="join-preview-list">'+d.targets.map(t=>'<div class="delivery-target"><div><strong>'+esc(t.title)+'</strong><small>'+esc(t.link)+'</small></div>'+badge(t.kind==='invite'?'邀请链接':'公开用户名','purple')+'</div>').join('')+'</div>'+
 (d.duplicates?'<p class="small muted">已合并 '+d.duplicates+' 个重复目标。</p>':'')+(d.rejected.length?'<details class="join-rejected"><summary>已排除 '+d.rejected.length+' 个无效目标</summary>'+d.rejected.map(t=>'<p><strong>'+esc(t.title)+'</strong><br>'+esc(t.error)+'</p>').join('')+'</details>':'')+
 opForm('joinConfirmForm',check('confirmed','我已核对账号和目标，确认仅加入以上有效群组。',false),'确认创建加群任务'));
 $('modal').querySelector('[name=confirmed]').required=true;
}
async function joinDetail(id){
 const j=await api('/api/join-jobs/detail?id='+encodeURIComponent(id));
 modal(j.name,'<div class="actions">'+badge(joinJobStates[j.status],'purple')+'<span class="small muted">'+esc(j.account_name||'账号已移除')+' · '+date(j.created_at)+'</span></div>'+notice('核对结果只查询成员身份，不会重复申请。取消任务不会退群，也不会撤销已提交的审批申请。')+'<div class="join-preview-list">'+j.targets.map(t=>'<div class="delivery-target"><div><strong>'+esc(t.title)+'</strong><small>'+esc(t.link)+'</small><small>'+esc(t.error||'')+'</small><small>尝试 '+t.attempts+' 次'+(t.peer_id?' · 群 ID '+esc(t.peer_id):'')+'</small></div><div class="join-target-actions">'+badge(joinTargetStates[t.status],['joined','already_joined'].includes(t.status)?'green':['failed','uncertain'].includes(t.status)?'amber':'purple')+(['requested','uncertain'].includes(t.status)?button('join-check','核对是否已加入','sm','data-id="'+j.id+'" data-target="'+t.id+'"'):'')+'</div></div>').join('')+'</div>',ob('join-detail','刷新详情',id));
}
async function refreshJoinPage(){
 const version=S.routeVersion,d=await api('/api/join-jobs');if(version!==S.routeVersion||!$('joinRows'))return;J.jobs=d.items;$('joinRows').innerHTML=joinRows();$('joinMetrics').innerHTML=joinMetrics();
}
async function joinAction(action,el,id){
 if(action==='join-selected'){
  if(!S.user){toast('请先登录');navigate('/login?next=/join-groups');return true;}
  J.resources=[...S.selected.keys()];navigate('/join-groups');return true;
 }
 if(action==='join-clear-resources'){J.resources=[];$('joinResources').innerHTML=joinResourceChips();return true;}
 if(action==='join-refresh'){el.disabled=true;try{await refreshJoinPage();}finally{el.disabled=false;}return true;}
 if(action==='join-detail'){await joinDetail(id);return true;}
 if(action==='join-check'){el.disabled=true;try{toast((await api('/api/join-jobs/check',{target_id:Number(el.dataset.target)})).message);await joinDetail(id);await refreshJoinPage();}finally{el.disabled=false;}return true;}
 if(action==='join-cancel'){modal('取消加群任务','<p>后续未处理目标会取消，已在途请求会完成。不会自动退群或撤销已提交的入群申请。</p>',button('close-modal','返回')+ob('join-confirm-cancel','确认取消',id,'danger'));return true;}
 if(['join-pause','join-resume','join-retry','join-confirm-cancel'].includes(action)){
  el.disabled=true;try{toast((await api('/api/join-jobs/control',{id,action:{'join-pause':'pause','join-resume':'resume','join-retry':'retry','join-confirm-cancel':'cancel'}[action]})).message);if($('modal').open)$('modal').close();await refreshJoinPage();}finally{el.disabled=false;}return true;
 }
 return false;
}
async function joinSubmit(form,payload){
 if(form.getAttribute('id')==='joinForm'){
  const draft={...payload,resource_ids:[...J.resources]},d=await api('/api/join-jobs/preview',draft);J.draft=draft;J.nonce=crypto.randomUUID();joinPreview(d);return true;
 }
 if(form.getAttribute('id')==='joinConfirmForm'){
  if(!form.elements.confirmed.checked)throw Error('请先核对并确认目标');
  const r=await api('/api/join-jobs/create',{...J.draft,revision:J.revision,nonce:J.nonce,confirm:true});$('modal').close();toast(r.message);J.resources=[];J.draft=null;render();return true;
 }
 return false;
}
setInterval(()=>{if(location.pathname==='/join-groups'&&S.user&&!document.hidden&&!$('modal').open)refreshJoinPage().catch(e=>{if(e.status===401){S.user=null;navigate('/login');}});},7000);
