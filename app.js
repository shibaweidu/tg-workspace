const $ = id => document.getElementById(id);
const nf = new Intl.NumberFormat('zh-CN');
const state = {page: 1, pages: 1, controller: null, timer: null};
const labels = {group:'普通群',megagroup:'超级群',supergroup:'超级群',channel:'频道',unknown:'未分类'};
const stages = {running:'采集中',done:'本轮已完成',partial:'部分完成',stopped:'已暂停',error:'采集异常',idle:'待采集'};
function esc(v){return String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function safeLink(value){try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)?u.href:'';}catch{return '';}}
function toast(s){$('toast').textContent=s;$('toast').classList.add('visible');clearTimeout(state.timer);state.timer=setTimeout(()=>$('toast').classList.remove('visible'),3500);}
async function api(url,body){const r=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data;}
async function load(){
  state.controller?.abort();const controller=state.controller=new AbortController();
  const params=new URLSearchParams({q:$('searchInput').value,type:$('typeFilter').value,visibility:$('visibilityFilter').value,page:state.page,page_size:$('pageSize').value});
  $('resultCount').textContent='正在查询…';
  try{
    const r=await fetch('/api/groups?'+params,{signal:controller.signal});const data=await r.json();if(!r.ok)throw Error(data.error||'查询失败');
    state.page=data.page;state.pages=data.pages;
    $('tableBody').innerHTML=data.items.map(item=>{
      const link=safeLink(item.public_link);
      const visibility=item.is_public===true?'公开':item.is_public===false?'私有':'未知';
      return '<tr><td><div class="group-name" title="'+esc(item.title)+'">'+esc(item.title||'未命名')+'</div><div class="group-handle">'+esc(item.username?'@'+item.username:'ID '+item.id)+'</div></td><td><span class="tag">'+esc(labels[item.peer_type]||item.peer_type||'未知')+'</span></td><td><span class="tag '+(item.is_public===true?'public':item.is_public===false?'private':'')+'">'+visibility+'</span></td><td class="members">'+(item.member_count==null?'—':nf.format(item.member_count))+'</td><td><div class="description" title="'+esc(item.description)+'">'+esc(item.description||'—')+'</div></td><td>'+(link?'<a class="group-link" href="'+esc(link)+'" target="_blank" rel="noopener noreferrer">'+esc(item.public_link)+'</a>':'—')+'</td></tr>';
    }).join('');
    $('emptyState').classList.toggle('hidden',data.items.length>0);
    $('resultCount').textContent='匹配 '+nf.format(data.total)+' 条记录';
    $('showingLabel').textContent=data.total?nf.format((data.page-1)*data.page_size+1)+'–'+nf.format(Math.min(data.page*data.page_size,data.total))+' / '+nf.format(data.total):'0 条记录';
    $('pageLabel').textContent=data.page+' / '+nf.format(data.pages);
    $('prevPage').disabled=data.page<=1;$('nextPage').disabled=data.page>=data.pages;
  }catch(e){if(e.name!=='AbortError'){$('resultCount').textContent='查询失败';toast(e.message);}}
}
async function status(){
  try{
    const data=await api('/api/status');const c=data.collection;
    $('totalStat').textContent=nf.format(data.total||0);$('groupStat').textContent=nf.format(data.groups||0);
    $('importStat').textContent=data.last_import?new Date(data.last_import).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'尚未采集';
    $('collectionTitle').textContent=stages[c.stage]||c.stage;
    $('collectionMessage').textContent=(c.message||'')+(c.pages_total?' · 主采集 '+nf.format(c.pages_done||0)+' / '+nf.format(c.pages_total)+' 页':'')+(c.repair_pages_done?' · 补采 '+c.repair_pages_done+' 页':'');
    $('progressBar').value=c.pages_total?Math.min(100,100*(c.pages_done||0)/c.pages_total):0;
    $('startButton').disabled=c.stage==='running';$('stopButton').disabled=c.stage!=='running';
    $('autoEnabled').checked=data.settings.auto_enabled;
    if(document.activeElement!==$('autoInterval'))$('autoInterval').value=data.settings.interval_hours;
    const scope=c.filters?.peer_type==='group'?'群组':c.filters?.peer_type==='channel'?'频道':'资源';
    $('sourceCount').textContent=c.source_total_latest?'源站最近返回 '+nf.format(c.source_total_latest)+' 条'+scope:'群与频道均采集，默认无筛选';
  }catch(e){$('collectionMessage').textContent='本地服务未连接：'+e.message;}
}
let debounce;
$('searchInput').addEventListener('input',()=>{clearTimeout(debounce);debounce=setTimeout(()=>{state.page=1;load();},250);});
['typeFilter','visibilityFilter','pageSize'].forEach(id=>$(id).addEventListener('change',()=>{state.page=1;load();}));
$('resetButton').onclick=()=>{$('searchInput').value='';$('typeFilter').value='all';$('visibilityFilter').value='all';state.page=1;load();};
$('prevPage').onclick=()=>{state.page--;load();};$('nextPage').onclick=()=>{state.page++;load();};
$('refreshButton').onclick=()=>{load();status();};
$('startButton').onclick=async()=>{try{toast((await api('/api/collect',{})).message);setTimeout(status,1000);}catch(e){toast(e.message);}};
$('stopButton').onclick=async()=>{try{toast((await api('/api/stop',{})).message);}catch(e){toast(e.message);}};
async function saveSettings(){try{await api('/api/settings',{auto_enabled:$('autoEnabled').checked,interval_hours:Number($('autoInterval').value)});toast('自动采集设置已保存');}catch(e){toast(e.message);}}
$('autoEnabled').onchange=saveSettings;$('autoInterval').onchange=saveSettings;
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('searchInput').focus();}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){status();load();}});
status();load();setInterval(()=>{if(!document.hidden)status();},5000);
