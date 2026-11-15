'use strict';
const jobStates={queued:'等待执行',running:'正在发送',waiting:'等待限流解除',paused:'已暂停',cancelled:'已取消',completed:'已完成',completed_with_errors:'完成，有待检查项'};
const targetStates={pending:'待发送',sending:'发送中',sent:'已发送',failed:'失败',skipped:'已跳过',uncertain:'结果待核对',cancelled:'已取消'};
function jobStatusHint(j){
 const now=Date.now()/1000,windowUntil=Number(j.account_window_next_at||0);
 const settings='消息间隔 '+j.interval_seconds+' 秒 · '+(j.repeat_count===1?'单轮发送':'第 '+j.round_number+' / '+(j.repeat_count===0?'不限':j.repeat_count)+' 轮 · 轮间隔 '+j.repeat_interval_seconds+' 秒');
 if(j.status==='queued'&&j.round_number>1&&j.next_at>now+1)return settings+' · 下轮 '+date(j.next_at*1000);
 if(['queued','running','waiting'].includes(j.status)&&windowUntil>now+1)return settings+' · 账号操作冷却至 '+date(windowUntil*1000)+(j.status==='waiting'?'（Telegram 限流）':'');
 if(j.last_error)return j.last_error;
 if(j.status==='queued'&&Number(j.scheduled_at)>now+1)return '计划于 '+date(j.scheduled_at*1000)+' 执行';
 return settings;
}

function broadcastSettingsHTML(){
 return '<fieldset class="broadcast-settings"><legend>发送间隔与循环</legend><label class="field">每条群消息的间隔（秒）<input class="input" name="interval_seconds" type="number" min="3" max="3600" step="1" value="10" required aria-describedby="messageIntervalHint"><span class="hint" id="messageIntervalHint">向下一个群发送前至少等待此时长（3–3600 秒）；Telegram 限流可能延长等待。</span></label><label class="field">循环发送<select name="repeat_mode" id="repeatMode"><option value="once">只发送一轮</option><option value="count">指定总轮数</option><option value="forever">持续循环，直到暂停或取消</option></select></label><div id="repeatSettings" hidden><label class="field" id="repeatCountField">总轮数（包含第一轮）<input class="input" name="repeat_count" type="number" min="2" max="10000" step="1" value="2" disabled></label><label class="field">两轮之间的间隔（秒）<input class="input" name="repeat_interval_seconds" type="number" min="3" max="604800" step="1" value="3600" disabled aria-describedby="repeatIntervalHint"><span class="hint" id="repeatIntervalHint">本轮所有群处理结束后开始计时，再发送下一轮；每轮使用同一份消息及群组快照。可随时暂停或取消。</span></label></div></fieldset>';
}

function telegramAccountsPage(){
 const w=S.workspace, t=w.integration;
 return pageHead('Telegram 账号','连接自己的账号，查看会话状态并管理发送任务。',button('telegram-config',icon('settings')+' 应用配置')+button('new-account',icon('plus')+' 添加账号','primary'))+
 '<div class="connection-summary card"><div>'+icon('shield')+'<strong>'+(t.configured?'Telegram 应用已配置':'先配置 Telegram 应用')+'</strong><p>'+(t.configured?'API ID '+esc(t.api_id)+' · 凭据使用'+esc(t.storage)+'保存':'使用自己的 API ID 与 API Hash，随后通过扫码或验证码连接账号。')+'</p></div>'+badge(t.available?'连接组件已就绪':'连接依赖未安装',t.available?'green':'amber')+'</div>'+
 (w.accounts.length?'<div class="accounts-grid">'+w.accounts.map(a=>'<article class="card account-card"><div class="account-card-top"><span class="avatar">'+esc(a.name.slice(0,1))+'</span><div style="min-width:0"><h3 class="overflow-text">'+esc(a.name)+'</h3><p>'+esc(a.phone)+'</p></div></div>'+badge('<span class="dot"></span> '+(a.status==='connected'?'会话已保存':'尚未连接'),a.status==='connected'?'green':'amber')+'<p class="notes">'+esc(a.notes||'暂无备注')+'</p><p class="small muted">'+(a.status==='connected'?'点击检查连接，核对 Telegram 当前会话。':'点击连接，选择扫码或验证码登录。')+'</p><div class="actions">'+(a.status==='connected'?button('telegram-refresh','检查连接','sm','data-id="'+a.id+'"')+button('telegram-disconnect','断开连接','sm danger','data-id="'+a.id+'"'):button('telegram-connect',icon('link')+' 连接账号','sm primary','data-id="'+a.id+'"'))+button('edit-account','编辑','sm','data-id="'+a.id+'"')+button('remove-account','移除','sm danger','data-id="'+a.id+'"')+'</div></article>').join('')+'</div>':'<section class="card">'+empty('连接第一个 Telegram 账号','添加手机号，配置应用，然后通过扫码或验证码登录。',button('new-account','添加账号','primary'),'phone')+'</section>');
}

function deliveryPage(){
 const w=S.workspace, jobs=w.jobs||[], active=jobs.filter(j=>['queued','running','waiting'].includes(j.status));
 return pageHead(location.pathname==='/broadcasts'?'群发任务':'任务中心','保存消息计划，设置发送时间，追踪每个目标的执行结果。',button('jobs-refresh',icon('refresh')+' 刷新')+button('new-draft',icon('plus')+' 新建任务草稿','primary'))+
 '<div class="metrics">'+metric('任务草稿',fmt(w.drafts.length),'保存后可继续编辑','tasks')+metric('待执行 / 执行中',fmt(active.length),'最近 100 条任务中的活动任务','clock')+metric('成功发送',fmt(jobs.reduce((n,j)=>n+j.sent,0)),'最近 100 条任务的确认成功数','send')+metric('需要检查',fmt(jobs.reduce((n,j)=>n+j.failed+j.uncertain,0)),'失败或结果待核对','info')+'</div>'+
 notice('任务固定关联账号和目标。文字群发固定内容；转发群发按原消息发送，后续编辑原消息会影响转发内容。账号须已加入目标群组；未加入会跳过。Telegram 要求等待时自动延后，结果不明确时暂停，避免自动重发。')+
 '<section class="card" style="margin-bottom:24px"><div class="card-head"><div><h2>消息草稿</h2><p>选择关联账号，勾选已加入的群组，再确认内容并创建发送任务</p></div>'+link('/group-search','选择群组目标','button sm')+'</div>'+(w.drafts.length?'<div class="table-scroll"><table class="data-table"><thead><tr><th>名称与消息</th><th>目标</th><th>账号</th><th>操作</th></tr></thead><tbody>'+w.drafts.map(d=>'<tr><td><strong>'+esc(d.name)+'</strong><div class="resource-desc">'+esc(d.send_mode==='forward'?'转发 · '+d.source_link:d.content.slice(0,70))+'</div></td><td>'+d.targets.length+' 个</td><td>'+esc(w.accounts.find(a=>a.id===d.account_id)?.name||'未指定')+'</td><td><div class="actions">'+button('draft-execute',icon('send')+' 创建发送任务','sm primary','data-id="'+d.id+'"')+button('edit-draft','编辑','sm','data-id="'+d.id+'"')+button('remove-draft','删除','sm danger','data-id="'+d.id+'"')+'</div></td></tr>').join('')+'</tbody></table></div>':empty('先保存一份消息草稿','选择已连接账号，加载已加入的群组，再编写消息内容。',button('new-draft','创建草稿','primary'),'tasks'))+'</section>'+
 '<section class="card"><div class="card-head"><div><h2>执行记录</h2><p>最近 100 条任务 · 暂停和取消会在当前在途请求结束后生效</p></div>'+badge('逐目标记录','purple')+'</div>'+(jobs.length?'<div class="table-scroll"><table class="data-table"><thead><tr><th>任务</th><th>状态</th><th>结果</th><th>计划时间</th><th>操作</th></tr></thead><tbody>'+jobs.map(j=>'<tr><td><strong>'+esc(j.name)+'</strong><div class="resource-desc">'+esc(jobStatusHint(j))+'</div></td><td>'+badge(jobStates[j.status]||j.status,['completed'].includes(j.status)?'green':['paused','completed_with_errors'].includes(j.status)?'amber':'purple')+'</td><td><strong>'+j.sent+' / '+j.total+'</strong><div class="small muted">失败 '+j.failed+' · 跳过 '+j.skipped+' · 待核对 '+j.uncertain+'</div></td><td>'+date(j.scheduled_at*1000)+'</td><td><div class="actions">'+button('job-detail','详情','sm','data-id="'+j.id+'"')+(['queued','running','waiting'].includes(j.status)?button('job-pause','暂停','sm','data-id="'+j.id+'"'):'')+(j.status==='paused'?button('job-resume','继续','sm','data-id="'+j.id+'"'):'')+(j.failed&&['paused','completed_with_errors'].includes(j.status)?button('job-retry','重试失败目标','sm','data-id="'+j.id+'"'):'')+(['queued','running','waiting','paused'].includes(j.status)?button('job-cancel','取消','sm danger','data-id="'+j.id+'"'):'')+(['completed','completed_with_errors','cancelled'].includes(j.status)?button('job-delete','删除','sm danger','data-id="'+j.id+'"'):'')+'</div></td></tr>').join('')+'</tbody></table></div>':empty('还没有发送记录','创建发送任务后，这里会显示进度和实际结果。','','clock'))+'</section>';
}

function telegramConfig(){
 const t=S.workspace.integration;
 modal('Telegram 应用配置','<p>在 <a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">my.telegram.org/apps</a> 创建自己的应用，获取 API ID 和 API Hash。</p><form id="telegramConfigForm"><label class="field">API ID<input class="input" name="api_id" inputmode="numeric" pattern="[0-9]+" value="'+esc(t.api_id||'')+'" required></label><label class="field">API Hash<input class="input" name="api_hash" type="password" autocomplete="off" minlength="32" maxlength="32" required placeholder="输入 32 位 API Hash"><span class="hint">密钥仅用于 Telegram 连接，加密保存后不会回显。</span></label><div class="form-error" role="alert"></div><button class="button primary" type="submit">加密保存应用配置</button></form>');
}

function telegramLogin(id,stage,message){
 const acc=S.workspace.accounts.find(a=>a.id===id);if(!acc)return;
 S.telegramAccount=id;
 const password=stage==='password';
 modal('连接 '+acc.name,telegramLoginTabs(id,'phone')+'<p>'+esc(message||'为 '+acc.phone+' 获取登录验证码。')+'</p>'+notice('验证码和二步验证密码仅用于本次登录，不保存到数据库。登录会话加密保存在本机。')+'<form id="telegramLoginForm" data-stage="'+(stage||'request')+'" data-account="'+esc(id)+'">'+(stage==='code'?'<label class="field">Telegram 验证码<input class="input" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="10" required autofocus></label>':password?'<input type="text" name="username" value="'+esc(acc.phone)+'" autocomplete="username" hidden><label class="field">Telegram 二步验证密码<input class="input" type="password" name="password" autocomplete="current-password" maxlength="256" required autofocus></label>':'')+'<div class="form-error" role="alert"></div><button class="button primary" type="submit">'+(stage==='code'?'验证并连接':password?'完成二步验证':'获取验证码')+'</button></form>');
}

let telegramQR=null;
function telegramLoginTabs(id,mode){
 return '<div class="telegram-login-tabs" role="group" aria-label="登录方式">'+button('telegram-phone','手机号登录',mode==='phone'?'primary':'','data-id="'+esc(id)+'" aria-pressed="'+(mode==='phone')+'"')+button('telegram-qr','扫码登录',mode==='qr'?'primary':'','data-id="'+esc(id)+'" aria-pressed="'+(mode==='qr')+'"')+'</div>';
}
function cancelTelegramQRRequest(state){
 if(!state?.login_id)return Promise.resolve();
 return fetch('/api/telegram/qr-cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:state.id,login_id:state.login_id}),keepalive:true}).catch(()=>{});
}
function stopTelegramQR(){
 const state=telegramQR;telegramQR=null;if(!state)return;
 clearTimeout(state.timer);clearInterval(state.clock);
 return cancelTelegramQRRequest(state);
}
function qrIsCurrent(state){return telegramQR===state&&$('modal').open;}
function telegramQRView(state,result){
 if(!qrIsCurrent(state))return;
 state.stage=result.stage;clearInterval(state.clock);
 const acc=S.workspace.accounts.find(a=>a.id===state.id);if(!acc)return;
 const password=result.stage==='password',waiting=result.stage==='waiting';
 const image=waiting&&state.image?'<img class="telegram-qr-image" src="'+esc(state.image)+'" width="240" height="240" alt="Telegram 登录二维码，请使用对应账号扫描">':'';
 const body=telegramLoginTabs(state.id,'qr')+'<p class="small muted">登录账号：'+esc(acc.phone)+'</p>'+(password?'<form id="telegramQRPasswordForm"><p>'+esc(result.message)+'</p><input type="text" name="username" value="'+esc(acc.phone)+'" autocomplete="username" hidden><label class="field">Telegram 二步验证密码<input class="input" name="password" type="password" autocomplete="current-password" maxlength="256" required autofocus></label><div class="form-error" role="alert"></div><button class="button primary" type="submit">完成二步验证</button></form>':'<div class="telegram-qr-layout"><div class="telegram-qr-panel">'+image+'<p id="telegramQRStatus" role="status" aria-live="polite">'+esc(result.message)+'</p><p id="telegramQRExpiry" class="small muted"></p></div><div class="telegram-qr-guide"><ol class="telegram-qr-steps"><li>在手机上打开 Telegram，切换到上述手机号的账号。</li><li>进入「设置 → 设备 → 连接桌面设备 / 扫描二维码」。</li><li>扫描二维码，并在手机上确认登录。</li></ol>'+button('telegram-qr-refresh','重新生成二维码','','data-id="'+esc(state.id)+'"')+'</div></div>');
 modal('扫码连接 '+acc.name,body,'',true);
 if(waiting){
  const tick=()=>{if(!qrIsCurrent(state))return;const seconds=Math.max(0,Math.ceil(result.expires_at-Date.now()/1000));if($('telegramQRExpiry'))$('telegramQRExpiry').textContent=seconds?'二维码剩余 '+seconds+' 秒有效':'正在确认二维码是否已过期…';if(!seconds)document.querySelector('.telegram-qr-image')?.remove();};
  tick();state.clock=setInterval(tick,1000);
 }
}
async function telegramQRStart(id){
 await stopTelegramQR();
 const acc=S.workspace.accounts.find(a=>a.id===id);if(!acc)return;
 const state={id,stage:'starting'};telegramQR=state;
 // Render the loading state synchronously, so closing it also invalidates late responses.
 modal('扫码连接 '+acc.name,telegramLoginTabs(id,'qr')+'<div class="telegram-qr-panel"><p role="status">正在连接 Telegram 并生成二维码…</p></div>','',true);
 try{
  const r=await api('/api/telegram/qr-start',{id});state.login_id=r.login_id;
  if(!qrIsCurrent(state)){await cancelTelegramQRRequest(state);return;}
  state.image=r.image;telegramQRView(state,r);telegramQRPoll(state);
 }catch(e){if(qrIsCurrent(state))telegramQRView(state,{stage:'error',message:e.message});}
}
async function telegramQRPoll(state){
 if(!qrIsCurrent(state)||state.stage!=='waiting')return;
 state.timer=setTimeout(async()=>{
  try{
   const r=await api('/api/telegram/qr-status',{id:state.id,login_id:state.login_id});
   if(!qrIsCurrent(state))return;
   if(r.stage==='connected'){telegramQRComplete(state,r);return;}
   if(r.stage!==state.stage)telegramQRView(state,r);
   telegramQRPoll(state);
  }catch(e){if(qrIsCurrent(state))telegramQRView(state,{stage:'error',message:e.message});}
 },1500);
}
function telegramQRComplete(state,result){
 if(!qrIsCurrent(state))return;
 clearTimeout(state.timer);clearInterval(state.clock);telegramQR=null;
 cancelTelegramQRRequest(state);$('modal').close();toast(result.message);render();
}
document.addEventListener('DOMContentLoaded',()=>{
 $('modal').addEventListener('cancel',()=>stopTelegramQR());
 $('modal').addEventListener('close',()=>{if(!$('modal').open)stopTelegramQR();});
});
window.addEventListener('pagehide',()=>stopTelegramQR());
window.addEventListener('popstate',()=>stopTelegramQR());

function updateDraftSendMode(){
 const form=$('draftForm');if(!form)return;
 const forward=form.elements.send_mode.value==='forward';
 $('draftTextFields').hidden=forward;$('draftForwardFields').hidden=!forward;
 form.elements.content.disabled=forward;form.elements.content.required=!forward;
 form.elements.source_link.disabled=!forward;form.elements.source_link.required=forward;
 invalidateForwardPreview();
}
function invalidateForwardPreview(){
 S.forwardPreviewRequest=Symbol();
 if($('forwardSourcePreview'))$('forwardSourcePreview').innerHTML='';
}
function forwardPreviewHTML(p){
 const mediaNames={MessageMediaPhoto:'图片',MessageMediaDocument:'文件 / 视频 / 动画',MessageMediaWebPage:'网页预览',MessageMediaPoll:'投票',MessageMediaGeo:'位置',MessageMediaContact:'联系人'};
 return '<div class="forward-preview"><p class="small muted">来源：'+esc(p.title)+' · 消息 '+p.message_id+'</p><div class="message-preview">'+esc(p.text||'（无文字内容）')+'</div>'+(p.media?'<p class="small muted">附带媒体：'+esc(mediaNames[p.media]||'媒体消息')+'</p>':'')+(p.buttons?.length?'<div class="forward-button-rows" aria-label="原消息按钮预览">'+p.buttons.map(row=>'<div class="forward-button-row">'+row.map(b=>'<span class="forward-button">'+esc(b.text)+(b.is_url?' ↗':'')+'</span>').join('')+'</div>').join('')+'</div>':'')+'<p class="small muted">这是原消息转发预览，不是发送消息链接：任务执行时调用 Telegram 转发接口，保留来源、文字、媒体和可保留的按钮。交互按钮取决于原机器人，建议先转发到测试群。'+(p.album?'此链接属于相册，本次仅转发链接指向的单条消息。':'')+'原消息后续编辑会影响实际转发内容。</p></div>';
}
async function checkDraftForwardSource(el){
 const form=$('draftForm'),aid=form?.elements.account_id.value,link=form?.elements.source_link.value;
 if(!aid)throw Error('请先选择已连接账号');
 if(!link)throw Error('请填写原消息链接');
 const request=Symbol();S.forwardPreviewRequest=request;el.disabled=true;
 $('forwardSourcePreview').innerHTML='<p role="status">正在读取原消息…</p>';
 try{
  const p=await api('/api/telegram/forward-preview',{id:aid,source_link:link});
  if(S.forwardPreviewRequest===request&&form.isConnected&&$('modal').open)$('forwardSourcePreview').innerHTML=forwardPreviewHTML(p);
 }catch(e){if(S.forwardPreviewRequest===request&&form.isConnected)$('forwardSourcePreview').textContent=e.message;}
 finally{if(el.isConnected)el.disabled=false;}
}
async function executeModal(id){
 const request=Symbol();S.executeRequest=request;
 const d=await api('/api/drafts/preview?id='+encodeURIComponent(id)), a=S.workspace.accounts.find(x=>x.id===d?.account_id);
 if(!d)return;
 if(!a||a.status!=='connected'){toast('请先连接草稿关联的 Telegram 账号');return;}
 let source=null;
 if(d.send_mode==='forward'){
  source=await api('/api/telegram/forward-preview',{id:d.account_id,source_link:d.source_link});
 }
 if(S.executeRequest!==request)return;
 S.executeDraft=id;S.executeRevision=d.updated_at;S.executeSourcePeer=source?.peer_id||null;
 modal('确认发送计划','<form id="executeForm"><dl class="detail-grid"><div><dt>任务名称</dt><dd>'+esc(d.name)+'</dd></div><div><dt>关联账号</dt><dd>'+esc(a.name)+'</dd></div><div><dt>目标数量</dt><dd>'+d.targets.length+' 个</dd></div><div><dt>发送方式</dt><dd>'+(source?'转发原消息':'纯文本')+' · 仅已加入的群组</dd></div></dl><div class="target-list">'+d.targets.map(t=>'<span class="target-chip">'+esc(t.title||t.id)+'</span>').join('')+'</div>'+(source?'<p class="small muted">'+esc(d.source_link)+'</p>'+forwardPreviewHTML(source):'<div class="message-preview">'+esc(d.content)+'</div>')+'<label class="field">发送时间<select name="timing" id="sendTiming"><option value="now">立即加入队列</option><option value="later">指定时间</option></select></label><label class="field" id="scheduleField" hidden>定时发送（当前设备时区）<input class="input" name="scheduled_at" type="datetime-local"></label>'+broadcastSettingsHTML()+'<label class="check-label"><input type="checkbox" name="confirmed" required> 我已核对消息内容、关联账号与目标，确认创建发送任务。</label><div class="form-error" role="alert"></div><button type="submit" class="button primary" style="margin-top:18px">确认并创建任务</button></form>');
}

function deliveryRoundsHTML(j){
 const numbers=[...new Set([j.round_number,...j.rounds.map(r=>r.round_number)])].sort((a,b)=>b-a);
 return '<label class="field">查看轮次（最近 100 轮）<select id="deliveryRound" data-id="'+esc(j.id)+'">'+numbers.map(n=>'<option value="'+n+'"'+(n===j.selected_round?' selected':'')+'>第 '+n+' 轮'+(n===j.round_number?' · 当前':' · 已结束')+'</option>').join('')+'</select></label>';
}

async function jobDetail(id,round){
 modal('执行详情','<p>正在读取逐目标记录…</p>');
 try{
  const j=await api('/api/jobs/detail?id='+encodeURIComponent(id)+(round?'&round='+encodeURIComponent(round):''));
  modal(j.name,'<div class="actions">'+badge(jobStates[j.status]||j.status,'purple')+'<span class="small muted">'+date(j.created_at)+(j.send_mode==='forward'?' · 创建时内容预览':' · 消息快照')+'</span></div>'+(j.send_mode==='forward'?'<p class="small muted">转发原消息 · '+esc(j.source_link)+'</p>':'')+'<p class="small muted">'+esc(jobStatusHint(j))+' · 已完成 '+j.completed_rounds+' 轮</p>'+deliveryRoundsHTML(j)+'<div class="message-preview">'+esc(j.content||'（媒体消息）')+'</div><div class="delivery-targets">'+j.targets.map(t=>'<div class="delivery-target"><div><strong>'+esc(t.title)+'</strong><small>'+esc(t.error||(t.message_id?'Telegram 消息 ID '+t.message_id:'等待执行'))+'</small></div>'+badge(targetStates[t.status]||t.status,t.status==='sent'?'green':t.status==='pending'?'purple':'amber')+'</div>').join('')+'</div>',button('job-detail','刷新详情','','data-id="'+j.id+'" data-round="'+j.selected_round+'"'));
 }catch(e){modal('读取失败','<p>'+esc(e.message)+'</p>');}
}

async function telegramAction(action,el,id){
 if(action==='telegram-phone')telegramLogin(id);
 else if(action==='telegram-qr'||action==='telegram-qr-refresh'){el.disabled=true;await telegramQRStart(id);}
 else if(action==='telegram-config')telegramConfig();
 else if(action==='telegram-connect'){
  if(!S.workspace.integration.configured){telegramConfig();toast('先保存应用配置，再连接账号');}
  else telegramLogin(id);
 }else if(action==='telegram-refresh'){
  el.disabled=true;const r=await api('/api/telegram/refresh',{id});toast(r.message);render();
 }else if(action==='telegram-disconnect'){
  modal('断开 Telegram 连接','<p>将退出此 Telegram 会话并清除本地会话。关联发送任务会暂停，已在途的请求会完成。</p>',button('close-modal','返回')+button('telegram-confirm-disconnect','退出并清除会话','danger','data-id="'+id+'"'));
 }else if(action==='telegram-confirm-disconnect'){
  el.disabled=true;const r=await api('/api/telegram/disconnect',{id});$('modal').close();toast(r.message);render();
 }else if(action==='forward-source-check')await checkDraftForwardSource(el);
 else if(action==='draft-execute'){el.disabled=true;try{await executeModal(id);}finally{el.disabled=false;}}
 else if(action==='jobs-refresh'){render();}
 else if(action==='job-detail')await jobDetail(id,el.dataset.round);
 else if(action==='job-cancel')modal('取消发送任务','<p>尚未发送的目标会取消。已发送的消息不会撤回，正在处理的请求会完成。</p>',button('close-modal','返回')+button('job-confirm-cancel','确认取消任务','danger','data-id="'+id+'"'));
 else if(action==='job-delete'){
  const job=S.workspace.jobs.find(j=>j.id===id);if(!job)return true;
  modal('删除执行记录','<p>确认删除「'+esc(job.name)+'」的执行记录？</p><p>将永久删除此任务的逐群结果和全部历史轮次。消息草稿保留，已发送的 Telegram 消息不会撤回。</p>',button('close-modal','返回')+button('job-confirm-delete','确认删除记录','danger','data-id="'+esc(id)+'"'));
 }else if(action==='job-confirm-delete'){
  el.disabled=true;
  try{const r=await api('/api/jobs/delete',{id});$('modal').close();toast(r.message);await render();}
  finally{el.disabled=false;}
 }else if(['job-pause','job-resume','job-confirm-cancel','job-retry'].includes(action)){
  el.disabled=true;const r=await api('/api/jobs/control',{id,action:{'job-retry':'retry','job-pause':'pause','job-resume':'resume','job-confirm-cancel':'cancel'}[action]});if($('modal').open)$('modal').close();toast(r.message);render();
 }else return false;
 return true;
}

async function telegramSubmit(form,payload){
 if(form.getAttribute('id')==='telegramConfigForm'){
  const r=await api('/api/telegram/settings',payload);form.reset();$('modal').close();toast(r.message);render();
 }else if(form.getAttribute('id')==='telegramLoginForm'){
  const id=form.dataset.account;
  const r=await api(form.dataset.stage==='request'?'/api/telegram/request-code':'/api/telegram/sign-in',{...payload,id});form.reset();
  if(!form.isConnected)return true;
  if(r.stage==='connected'){$('modal').close();toast(r.message);render();}
  else telegramLogin(id,r.stage,r.message);
 }else if(form.getAttribute('id')==='telegramQRPasswordForm'){
  const state=telegramQR;if(!state)throw Error('扫码流程已结束，请重新扫码');
  const r=await api('/api/telegram/qr-password',{id:state.id,login_id:state.login_id,password:payload.password});form.reset();
  if(!qrIsCurrent(state))return true;
  if(r.stage==='connected')telegramQRComplete(state,r);else telegramQRView(state,r);
 }else if(form.getAttribute('id')==='executeForm'){
  if(payload.confirmed!=='on')throw Error('请先核对并确认发送计划');
  if(payload.timing==='later'&&!payload.scheduled_at)throw Error('请选择发送时间');
  const r=await api('/api/drafts/execute',{id:S.executeDraft,revision:S.executeRevision,source_peer_id:S.executeSourcePeer,confirm:true,interval_seconds:Number(payload.interval_seconds),repeat_count:payload.repeat_mode==='forever'?0:payload.repeat_mode==='count'?Number(payload.repeat_count):1,repeat_interval_seconds:payload.repeat_mode==='once'?3600:Number(payload.repeat_interval_seconds),scheduled_at:payload.timing==='later'?new Date(payload.scheduled_at).toISOString():null});$('modal').close();toast(r.message);render();
 }else return false;
 return true;
}

document.addEventListener('change',e=>{
 if(e.target.id==='deliveryRound')jobDetail(e.target.dataset.id,e.target.value).catch(err=>toast(err.message));
 if(e.target.id==='repeatMode'){
  const mode=e.target.value,form=e.target.form,repeat=mode!=='once';
  $('repeatSettings').hidden=!repeat;$('repeatCountField').hidden=mode!=='count';
  form.elements.repeat_count.disabled=mode!=='count';form.elements.repeat_count.required=mode==='count';
  form.elements.repeat_interval_seconds.disabled=!repeat;form.elements.repeat_interval_seconds.required=repeat;
 }
 if(e.target.id==='sendTiming'){
  $('scheduleField').hidden=e.target.value!=='later';
  const input=$('scheduleField').querySelector('input');input.required=e.target.value==='later';
 }
});
setInterval(async()=>{
 if(document.hidden||!S.user||!['/tasks','/broadcasts','/dashboard'].includes(location.pathname)||$('modal').open)return;
 try{
  const data=await api('/api/jobs');
  if(JSON.stringify(data.items)!==JSON.stringify(S.workspace?.jobs))render();
 }catch{}
},7000);
