'use strict';
const $ = id => document.getElementById(id);
const fmt = n => new Intl.NumberFormat('zh-CN').format(n || 0);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = v => v ? new Date(v).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '暂无记录';
const icons = {
 send:'M22 2 9 15 M22 2 15 22 9 15 2 9 22 2',
 grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
 users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 3a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8',
 search:'M21 21l-4.3-4.3 M19 11a8 8 0 1 0-16 0 8 8 0 0 0 16 0',
 phone:'M6 2h12v20H6z M10 18h4',
 tasks:'M9 3H5v18h14V3h-4 M9 2h6v4H9z M8 11h8 M8 16h6',
 settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 2h6l1 4 3 1 3 5-3 5-3 1-1 4H9l-1-4-3-1-3-5 3-5 3-1z',
 refresh:'M20 7v5h-5 M4 17v-5h5 M5.4 7a8 8 0 0 1 13.2-2L20 7 M4 17l1.4 2a8 8 0 0 0 13.2-2',
 arrow:'M5 12h14 M13 6l6 6-6 6',
 arrowLeft:'M19 12H5 M11 6l-6 6 6 6',
 down:'M6 9l6 6 6-6',
 chevron:'M9 5l7 7-7 7',
 plus:'M12 5v14 M5 12h14',
 check:'M5 12l4 4L19 6',
 shield:'M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M8 12l3 3 5-6',
 clock:'M12 8v5l3 2 M22 12a10 10 0 1 0-20 0 10 10 0 0 0 20 0',
 file:'M14 2H4v20h16V8z M14 2v6h6 M8 13h8 M8 17h6',
 database:'M21 5c0 2-4 3-9 3S3 7 3 5s4-3 9-3 9 1 9 3 M3 5v14c0 2 4 3 9 3s9-1 9-3V5 M3 12c0 2 4 3 9 3s9-1 9-3',
 message:'M21 15a4 4 0 0 1-4 4H7l-5 3V6a4 4 0 0 1 4-4h11a4 4 0 0 1 4 4z M7 8h10 M7 13h7',
 bot:'M4 7h16v13H4z M12 3v4 M9 3h6 M8 12h1 M15 12h1 M8 16h8 M1 10v7 M23 10v7',
 logout:'M9 4H3v16h6 M9 12h13 M17 7l5 5-5 5',
 info:'M12 11v6 M12 7h.01 M22 12a10 10 0 1 0-20 0 10 10 0 0 0 20 0',
 close:'M6 6l12 12 M18 6 6 18',
 menu:'M3 6h18 M3 12h18 M3 18h18',
 link:'M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2 M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2',
 download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
 edit:'M16 3l5 5-12 12-6 1 1-6z M14 5l5 5',
 trash:'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7'
};
const icon = (name, cls='') => '<svg class="icon '+cls+'" viewBox="0 0 24 24" aria-hidden="true"><path d="'+(icons[name] || icons.grid)+'"/></svg>';
const link = (path, text, cls='') => '<a href="'+path+'" '+(path.startsWith('/api/')?'':'data-link')+' '+(text.replace(/<[^>]*>/g,'').trim()?'':'aria-label="打开 '+esc(path)+'"')+' class="'+cls+'">'+text+'</a>';
const logo = () => link('/', '<span class="logo-mark">'+icon('send')+'</span><span>TG 工作台<small>TELEGRAM WORKSPACE</small></span>', 'logo');
const badge = (text, tone='') => '<span class="tag '+tone+'">'+text+'</span>';
const button = (action, text, cls='', extra='') => '<button type="button" class="button '+cls+'" data-action="'+action+'" '+extra+'>'+text+'</button>';
const notice = (text, amber=false) => '<div class="notice '+(amber?'amber':'')+'">'+icon('info')+'<p>'+text+'</p></div>';
const S = {user:null, stats:null, workspace:null, routeVersion:0, selected:new Map(), filters:{}, page:1, pages:1, groupController:null, debounce:null, toastTimer:null, draft:null, account:null};
const titles = {'/dashboard':'仪表盘','/group-search':'群组资源库','/telegram-accounts':'Telegram 账号','/tasks':'任务中心','/broadcasts':'群发任务','/account-settings':'账号设置','/collection':'数据采集'};
const protectedRoutes = ['/dashboard','/telegram-accounts','/tasks','/broadcasts','/account-settings','/collection'];
titles['/join-groups']='自动加群';protectedRoutes.push('/join-groups');
Object.assign(titles,{'/message-center':'消息中心','/direct-messages':'消息中心','/auto-replies':'自动化规则','/keyword-outreach':'关键词响应','/group-moderation':'群组管理','/membership':'套餐与订单','/commissions':'邀请与佣金','/proxies':'账号代理','/ai-model':'AI 模型','/admin':'管理后台'});
protectedRoutes.push('/message-center','/direct-messages','/auto-replies','/keyword-outreach','/group-moderation','/membership','/commissions','/proxies','/ai-model','/admin');
const stages = {idle:'尚未开始',running:'正在采集',done:'本轮已完成',error:'采集异常',stopped:'已暂停',partial:'等待继续'};

async function api(path, body, signal){
 const r = await fetch(path, {method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal});
 const data = await r.json();
 if(!r.ok){const e=new Error(data.error || '请求失败，请稍后重试');e.status=r.status;throw e;}
 return data;
}
function toast(message){$('toast').textContent=message;$('toast').classList.add('visible');clearTimeout(S.toastTimer);S.toastTimer=setTimeout(()=>$('toast').classList.remove('visible'),4000);}
function safeURL(value){try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)?u.href:'';}catch{return '';}}
function navigate(path, replace=false){stopTelegramQR();if($('modal').open)$('modal').close();S.groupController?.abort();clearTimeout(S.debounce);history[replace?'replaceState':'pushState']({},'',path);render();window.scrollTo(0,0);}
function syncMenu(){
 const side=document.querySelector('.sidebar'),main=document.querySelector('.app-main');
 if(!side)return;
 const mobile=matchMedia('(max-width:700px)').matches,opened=side.classList.contains('open')&&mobile;
 side.inert=mobile&&!opened;
 if(main)main.inert=opened;
 document.querySelector('.mobile-toggle')?.setAttribute('aria-expanded',String(opened));
}
function pageHead(title, subtitle, actions=''){return '<div class="page-head"><div><span class="page-title-eyebrow">YOUR TELEGRAM WORKSPACE</span><h1>'+title+'</h1><p>'+subtitle+'</p></div><div class="actions">'+actions+'</div></div>';}
function metric(label,value,foot,glyph){return '<article class="card metric"><div class="metric-top">'+label+'<span class="metric-icon">'+icon(glyph)+'</span></div><strong>'+value+'</strong><small>'+foot+'</small></article>';}
function empty(title,copy,action='',glyph='file'){return '<div class="empty"><div class="empty-icon">'+icon(glyph)+'</div><h3>'+title+'</h3><p>'+copy+'</p>'+action+'</div>';}
function publicNav(){return '<nav class="public-nav" aria-label="官网导航">'+logo()+'<div class="public-nav-links"><a href="/#features">产品功能</a>'+link('/tg-qunfa','群发工作流')+link('/group-search','群组资源库')+'</div><div class="actions">'+(S.user?link('/dashboard','进入工作台 '+icon('arrow'),'button primary'):link('/login','登录','text-login')+link('/register','创建工作空间 '+icon('arrow'),'button primary'))+'</div></nav>';}
function footer(){return '<footer class="public-footer"><span>© '+new Date().getFullYear()+' TG 工作台 · 独立开发的本地版本</span><div class="actions">'+link('/group-search','群组资源库')+link('/tg-qunfa','产品说明')+link(S.user?'/dashboard':'/login','进入工作台')+'</div></footer>';}

function landing(){
 const d=S.stats||{};
 const features=[
  ['search','群组资源库','搜索群名、用户名和简介，按类型、成员数与更新时间找到目标群组。','已可使用','/group-search'],
  ['phone','Telegram 账号','保存账号资料，通过验证码和二步验证连接 Telegram。','支持真实连接','/telegram-accounts'],
  ['send','消息任务','从群库选择目标、保存草稿，确认后立即执行或定时发送。','支持定时执行','/tasks'],
  ['database','持续更新资源','按资源 ID 合并数据，断点续采、失败重试，随时导出 CSV 和 JSON。','已可使用','/collection'],
  ['message','自动回复与客服','集中管理已有会话、关键词回复、群欢迎语和人工接管。','已可配置','/message-center'],
  ['users','自动加群','从资源库选群或粘贴群链接，预览确认后依次加入，逐群查看申请和执行结果。','配置后可用','/join-groups'],
  ['shield','专属工作空间','真实注册与登录，账号资料和任务草稿按用户隔离，登录状态可撤销。','已可使用','/register']
 ];
 return '<div class="landing">'+publicNav()+'<main id="content"><section class="hero"><div class="hero-inner"><div><div class="hero-badge"><span class="dot"></span>Telegram 运营，从这里开始</div><h1>把账号、群组与任务<br>放进一个<em>工作台。</em></h1><p>从发现群组到整理账号，再到规划消息任务。让分散的运营流程有序衔接，在一个清晰的工作空间里完成。</p><div class="actions">'+link(S.user?'/dashboard':'/register','开始使用 '+icon('arrow'),'button primary')+link('/group-search',icon('search')+' 浏览群组资源','button')+'</div><div class="hero-notes"><span>'+icon('check')+' 本地数据存储</span><span>'+icon('check')+' 支持数据导出</span><span>'+icon('check')+' 独立用户空间</span></div></div><div class="preview" aria-label="资源工作台实时数据预览"><div class="preview-bar"><span class="window-dot"></span><span class="window-dot"></span><span class="window-dot"></span><span class="preview-url">TG WORKSPACE / OVERVIEW</span>'+icon('shield')+'</div><div class="preview-content"><span class="label">WORKSPACE OVERVIEW</span><h3>每一步，都井然有序</h3><div class="preview-stats"><div class="preview-stat"><span>已收录群组</span><strong>'+fmt(d.groups)+'</strong></div><div class="preview-stat"><span>全部资源</span><strong>'+fmt(d.total)+'</strong></div></div><div class="preview-item"><span class="mini-icon">'+icon('database')+'</span><span>资源库已接入本地数据</span>'+badge('可搜索','green')+'</div><div class="preview-item"><span class="mini-icon">'+icon('tasks')+'</span><span>创建并保存消息任务</span>'+badge('草稿工作流','purple')+'</div><div class="preview-item"><span class="mini-icon">'+icon('phone')+'</span><span>Telegram 账号执行服务</span>'+badge('配置后可连接','purple')+'</div></div></div></div></section><section class="section" id="features"><div class="section-title"><span class="label">EVERYTHING IN ONE PLACE</span><h2>从资源到执行，连接你的运营流程</h2><p>先把资料、账号与计划组织起来，再逐步接入自动化能力。当前版本的可用范围在每个模块清晰标明。</p></div><div class="feature-grid">'+features.map((f,i)=>'<article class="feature-card"><div class="feature-icon">'+icon(f[0])+'</div><h3>'+f[1]+'</h3><p>'+f[2]+'</p><div class="actions"><span class="feature-status '+(i===4?'planned':'')+'">'+f[3]+'</span>'+link(f[4],icon('arrow'),'button sm')+'</div></article>').join('')+'</div></section><section class="steps" id="workflow"><div class="section"><div class="section-title"><span class="label">A SIMPLE WORKFLOW</span><h2>三个步骤，搭建自己的运营空间</h2></div><div class="step-grid"><article><span class="step-number">01</span><h3>建立你的工作空间</h3><p>注册本地账号，保存 Telegram 账号资料，整理日常使用的账号。</p></article><article><span class="step-number">02</span><h3>找到合适的群组</h3><p>在真实资源库中搜索和筛选，查看群资料，选择需要使用的目标。</p></article><article><span class="step-number">03</span><h3>保存消息任务</h3><p>为目标编写消息内容，保存任务草稿；连接账号并确认后可立即或定时发送。</p></article></div></div></section><section class="section"><div class="cta-card"><div><h2>让下一次运营，从有序开始。</h2><p>已有资源可以直接搜索，创建工作空间即可保存账号和任务。</p></div>'+link(S.user?'/dashboard':'/register','进入工作空间 '+icon('arrow'),'button primary')+'</div></section></main>'+footer()+'</div>';
}
function article(){
 return '<div class="landing">'+publicNav()+'<main id="content" class="article"><span class="label">PRODUCT GUIDE</span><h1>Telegram 消息与群组工作流</h1><p>从账号管理、资源搜索到任务草稿，逐步搭建可追踪的运营流程。</p><section><h2>当前版本已经能做什么</h2><ul><li>注册登录，管理独立工作空间和登录会话。</li><li>搜索已采集的群组和频道，组合成员数、类型及公开状态筛选。</li><li>查看资源详情，选择目标并保存消息任务草稿。</li><li>保存账号资料，编辑任务，导出完整资源文件。</li><li>单独创建自动加群任务，预览群链接或资源库目标，查看入群申请、失败与待核对结果。</li><li>工作空间所有者可以控制资源采集和更新间隔。</li></ul></section><section><h2>Telegram 接入安排</h2><p>当前已接入账号验证码登录、二步验证、会话检查、消息任务队列和逐目标结果。需要使用自己的 Telegram 应用配置并连接账号。保存草稿不会发送，创建发送任务前需确认消息和目标。</p><p>消息任务计划用于已有关系的群组、订阅者或主动联系的用户。账号权限、群管理权限和 Telegram 接口限制会在执行时检查。</p></section><section><h2>消息自动化与商业管理</h2><p>已接入已有会话同步、私信队列、关键词响应、自动回复、欢迎语、群关键词过滤和人工接管。套餐订单通过人工核实收款后开通，后台可管理额度、用户权限、支付说明、模型报价、运行监控及备份。Bot 专属接入与自动在线收款尚未接入。</p></section><div class="actions" style="margin-top:28px">'+link('/group-search','浏览资源库','button primary')+link('/register','创建工作空间','button')+'</div></main>'+footer()+'</div>';
}
function auth(mode){
 const register=mode==='register';
 return '<main id="content" class="auth-page"><section class="auth-brand-panel">'+logo()+'<div><span class="tag">YOUR TELEGRAM WORKSPACE</span><h1>把每一个账号、<br>每一次计划，<br>都安排妥当。</h1><p>一个更清晰的工作空间，让群组资源、账号资料和消息任务保持连接。</p><div class="auth-points"><span>'+icon('database')+' 真实群组资源，随时搜索</span><span>'+icon('shield')+' 独立保存账号与任务资料</span><span>'+icon('tasks')+' 从想法到可编辑的任务草稿</span></div></div><span class="auth-copyright">TG 工作台 · 本地开发版</span></section><section class="auth-form-panel"><div class="auth-form">'+link('/',icon('arrowLeft')+' 返回首页','auth-back')+'<h2>'+(register?'创建你的工作空间':'欢迎回来')+'</h2><p class="subtitle">'+(register?'创建本地账号，开始管理你的资源和任务。':'登录后继续管理你的 Telegram 工作空间。')+'</p><form id="authForm" data-mode="'+mode+'">'+(register?'<label class="field">你的名称<input class="input" name="name" autocomplete="nickname" maxlength="60" required placeholder="例如：我的运营工作室"></label>':'')+'<label class="field">邮箱地址<input class="input" type="email" name="email" autocomplete="username" required maxlength="254" placeholder="name@example.com"></label><label class="field">密码<input class="input" type="password" name="password" autocomplete="'+(register?'new-password':'current-password')+'" minlength="10" maxlength="256" required placeholder="'+(register?'至少 10 个字符':'输入你的密码')+'">'+(register?'<span class="hint">这是本地工作台账号，不是 Telegram 登录信息。</span>':'')+'</label>'+(register?'<label class="field">邀请码（选填）<input class="input" name="invite_code" maxlength="20" value="'+esc(new URLSearchParams(location.search).get('invite')||'')+'" autocomplete="off"></label>':'')+'<div class="form-error" role="alert"></div><button class="button primary wide" type="submit">'+(register?'创建工作空间':'登录工作台')+icon('arrow')+'</button></form><p class="auth-bottom">'+(register?'已有账号？ '+link('/login','立即登录'):'还没有账号？ '+link('/register','创建工作空间'))+'</p><p class="small muted" style="text-align:center;margin-top:28px">账号保存在此本地服务中</p></div></section></main>';
}
function appShell(content){
 const route=location.pathname;
 const nav=[['工作空间'],['/dashboard','grid','仪表盘'],['/telegram-accounts','phone','Telegram 账号'],['资源与任务'],['/group-search','search','群组资源库'],['/tasks','tasks','任务中心'],['/broadcasts','send','群发任务'],['/join-groups','users','自动加群'],['消息自动化'],['/message-center','message','消息中心 / 私信'],['/auto-replies','bot','自动化规则'],['商业与管理'],['/membership','file','套餐与订单'],['/commissions','users','邀请与佣金'],['/proxies','link','账号代理'],['/ai-model','bot','AI 模型'],...(S.user?.role==='owner'?[['/admin','shield','管理后台']]:[]),['/collection','refresh','数据采集'],['/account-settings','settings','账号设置']];
 return '<button class="sidebar-shade" data-action="toggle-menu" aria-label="关闭导航"></button><aside class="sidebar" aria-label="工作台导航">'+logo()+'<nav>'+nav.map(n=>n.length===1?'<div class="nav-group-label">'+n[0]+'</div>':link(n[0],icon(n[1])+n[2]+(n[0]==='/group-search'?'<span class="nav-count">LIVE</span>':''),'nav-link '+(route===n[0]?'active':''))).join('')+'</nav><div class="sidebar-bottom"><div class="sidebar-plan"><strong>你的专属工作空间</strong><p>账号资料与任务草稿独立保存</p></div><div class="user-row"><span class="avatar">'+esc(S.user?.name.slice(0,1)||'访')+'</span><div class="user-info"><strong>'+esc(S.user?.name||'访客浏览')+'</strong><small>'+(S.user?(S.user.role==='owner'?'工作空间所有者':'工作空间成员'):'登录后保存资料')+'</small></div>'+(S.user?button('logout',icon('logout'),'logout','aria-label="退出登录"'):link('/login',icon('arrow'),'button icon-only'))+'</div></div></aside><div class="app-main"><header class="topbar"><div class="breadcrumbs">'+button('toggle-menu',icon('menu'),'mobile-toggle icon-only','aria-label="打开导航"')+'<span class="workspace-crumb">工作空间</span><span>/</span><strong>'+esc(titles[route]||'工作台')+'</strong></div><div class="actions">'+badge('<span class="dot"></span> 本地工作空间','green')+link('/','官网','button sm')+'</div></header><main id="content" class="page">'+content+'<footer class="page-foot"><span>TG 工作台 · 本地开发版</span><span>资源最后写入：'+date(S.stats?.last_import)+' · 北京时间</span></footer></main></div>';
}
function dashboard(){
 const d=S.stats||{}, w=S.workspace||{accounts:[],drafts:[],activity:[]};
 const recent=w.drafts.slice(0,4);
 return pageHead('仪表盘','工作空间的资源、账号与任务，一目了然。',link('/tasks',icon('plus')+' 创建任务草稿','button primary'))+
 '<div class="welcome-banner"><div><h2>'+esc(S.user.name)+'，欢迎回到工作台</h2><p>从资源库选择目标，整理账号资料，保存下一次运营的消息计划。</p></div><span class="welcome-symbol">'+icon('send')+'</span>'+link('/group-search','发现群组 '+icon('arrow'),'button')+'</div>'+
 '<div class="metrics">'+metric('群组资源',fmt(d.groups),'已保存的普通群与超级群','users')+metric('频道资源',fmt(d.channels),'与群组分开归类','database')+metric('我的账号',fmt(w.accounts.length),fmt(w.accounts.filter(a=>a.status==='connected').length)+' 个已连接','phone')+metric('任务草稿',fmt(w.drafts.length),'当前工作空间的消息计划','tasks')+'</div>'+
 '<div class="dashboard-grid"><div><section class="card" style="overflow:hidden;margin-bottom:22px"><div class="card-head"><div><h2>开始下一步</h2><p>把常用操作放在手边</p></div>'+badge('工作流','purple')+'</div><div class="quick-grid">'+[['/group-search','search','发现群组','搜索并选择目标资源'],['/telegram-accounts','phone','管理账号','整理你的账号资料'],['/tasks','tasks','计划任务','编写消息与保存草稿']].map(x=>link(x[0],icon(x[1])+'<strong>'+x[2]+'</strong><p>'+x[3]+'</p>','quick-item')).join('')+'</div></section><section class="card"><div class="card-head"><div><h2>最近的任务草稿</h2><p>继续上一次的消息计划</p></div>'+link('/tasks','查看全部 '+icon('arrow'),'button sm')+'</div>'+(recent.length?'<div class="status-list">'+recent.map(x=>'<div class="status-line"><div><strong>'+esc(x.name)+'</strong><div class="muted small">'+fmt(x.targets.length)+' 个目标 · '+date(x.updated_at)+'</div></div>'+button('edit-draft','编辑','sm','data-id="'+x.id+'"')+'</div>').join('')+'</div>':empty('还没有任务草稿','从群组资源库选择目标，或创建一个空白消息计划。',link('/tasks','前往任务中心','button soft'),'tasks'))+'</section></div><div><section class="card" style="margin-bottom:22px"><div class="card-head"><h2>服务状态</h2>'+icon('shield')+'</div><div class="status-list"><div class="status-line"><span>本地资源库</span>'+badge('已接入','green')+'</div><div class="status-line"><span>资源采集</span>'+badge(stages[d.collection?.stage]||'读取中',d.collection?.stage==='error'?'amber':'purple')+'</div><div class="status-line"><span>Telegram 执行服务</span>'+badge(w.integration?.available?'已就绪':'依赖未安装',w.integration?.available?'green':'amber')+'</div></div></section><section class="card"><div class="card-head"><h2>工作空间动态</h2>'+icon('clock')+'</div><div class="activity">'+w.activity.map(a=>'<div class="activity-row"><span class="dot"></span><div>'+esc(a.message)+'<small>'+date(a.created_at)+'</small></div></div>').join('')+'</div></section></div></div>';
}
function resourcePage(){
 const f=S.filters;
 const ai=S.stats?.categories?.find(c=>c.id==='ai'),isAI=f.category==='ai',batch=f.batch||'all';
 const batchQuery=isAI?'&category=ai'+(batch!=='all'?'&batch='+encodeURIComponent(batch):''):'';
 const batchLabel=batch==='all'?'AI 分组':'第 '+batch+' 组';
 const option=(v,t,current)=>'<option value="'+v+'" '+(v===current?'selected':'')+'>'+t+'</option>';
 return pageHead('群组资源库','搜索真实群组与频道，选择目标并保存到你的任务。',link('/api/export?scope=groups&format=csv'+batchQuery,icon('download')+(isAI?' '+batchLabel+' CSV':' 群组 CSV'),'button')+button('export-menu',icon('file')+' 更多导出',''))+
 '<div class="metrics">'+metric('全部资源',fmt(S.stats?.total),'本地已保存的去重记录','database')+metric('群组',fmt(S.stats?.groups),'普通群 / 超级群','users')+metric('频道',fmt(S.stats?.channels),'独立的频道资源','message')+metric('公开资源',fmt(S.stats?.public),'按源站公开状态统计','shield')+'</div>'+
 '<section class="resource-category-bar" aria-label="资源分组"><div class="actions">'+button('resource-category','全部资源',''+(!isAI?'primary':''),'data-category="all" aria-pressed="'+!isAI+'"')+button('resource-category',icon('bot')+' AI 类群组 <span>'+fmt(ai?.count)+'</span>',isAI?'primary':'','data-category="ai" aria-pressed="'+isAI+'"')+'</div><p>按群名、用户名和简介中的 AI 主题词自动归类，后续采集同步更新。'+(isAI?' '+link('/api/export?scope=groups&format=json'+batchQuery,'导出'+batchLabel+' JSON'):'')+'</p>'+(isAI?resourceBatchChoices(ai,batch):'')+'</section>'+
 '<section class="card"><div class="card-head"><div><h2>'+(isAI?'AI 类群组'+(batch!=='all'?' / 第 '+batch+' 组':''):'全部资源')+'</h2><p id="resultCount">正在查询…</p></div>'+badge(isAI?'仅群组 · 可在详情修正分组':'默认不限类型与状态','purple')+'</div><div class="toolbar"><label class="search-input">'+icon('search')+'<input type="search" id="searchInput" aria-label="搜索群组" value="'+esc(f.q)+'" placeholder="搜索群名、用户名、简介或链接…"><kbd>Ctrl K</kbd></label><select id="typeFilter" aria-label="资源类型">'+[['all','全部类型'],['groups','仅群组'],['group','普通群'],['megagroup','超级群'],['channel','频道']].map(x=>option(...x,f.type)).join('')+'</select><select id="visibilityFilter" aria-label="公开状态">'+[['all','全部状态'],['public','公开'],['private','私有']].map(x=>option(...x,f.visibility)).join('')+'</select><select id="sortFilter" aria-label="排序方式">'+[['recent','最近更新'],['members_desc','成员数最多'],['members_asc','成员数最少'],['name','名称排序']].map(x=>option(...x,f.sort)).join('')+'</select>'+button('reset-filters',icon('refresh'),'icon-only','aria-label="重置筛选"')+'</div><div class="range-fields"><label>成员数从<input class="input" id="minMembers" type="number" min="0" value="'+esc(f.min_members)+'" placeholder="不限"></label><label>到<input class="input" id="maxMembers" type="number" min="0" value="'+esc(f.max_members)+'" placeholder="不限"></label><span>源站未提供成员数的资源，在设置范围后会被排除。</span></div><div id="selectionBar"></div><div class="table-scroll"><table class="data-table"><thead><tr><th style="width:42px"><input id="selectPage" type="checkbox" aria-label="选择本页全部资源"></th><th>群组 / 频道</th><th>类型</th><th>公开状态</th><th>成员数</th><th>简介</th><th>操作</th></tr></thead><tbody id="resourceRows"></tbody></table></div><div id="resourceEmpty" hidden></div><div class="table-bottom"><span id="showingLabel">正在读取数据…</span><div class="pagination">'+button('prev-page',icon('arrowLeft'),'','id="prevPage" aria-label="上一页" disabled')+'<span id="pageLabel">1 / 1</span>'+button('next-page',icon('arrow'),'','id="nextPage" aria-label="下一页" disabled')+'<select id="pageSize" aria-label="每页条数">'+[20,50,100].map(n=>option(String(n),n+' 条',f.page_size)).join('')+'</select></div></div></section>';
}
function accountsPage(){return telegramAccountsPage();}
function resourceBatchChoices(ai,batch){
 const choices=[{id:'all',name:'全部 AI 群',count:ai?.count||0},...(ai?.batches||[])];
 return '<div class="resource-batches"><strong>二级分类 · 每组 '+fmt(ai?.batch_size||500)+' 个</strong><div class="resource-batch-choices" role="group" aria-label="AI 群二级分类">'+choices.map(b=>button('resource-batch',esc(b.name)+' <span>'+fmt(b.count)+' 个</span>',String(b.id)===batch?'primary':'','data-batch="'+esc(b.id)+'" aria-pressed="'+(String(b.id)===batch)+'"')).join('')+'</div><p>按资源 ID 排序分组，最后一组保留剩余群组。搜索和显示排序仅作用于当前组。</p></div>';
}
function tasksPage(){return deliveryPage();}
function collectionPage(){
 const d=S.stats, c=d.collection||{}, owner=S.user.role==='owner';
 const percent=c.pages_total?Math.min(100,100*(c.pages_done||0)/c.pages_total):0;
 return pageHead('数据采集','管理资源更新、断点续采与数据导出。',button('refresh-data',icon('refresh')+' 刷新状态',''))+
 (!owner?notice('采集由工作空间所有者管理。你可以查看状态和搜索共享资源。'):'')+
 '<div class="metrics">'+metric('本地资源',fmt(d.total),'已按资源 ID 去重','database')+metric('已保存群组',fmt(d.groups),'不包含频道','users')+metric('本轮分页',fmt(c.pages_done)+' / '+fmt(c.pages_total),'保存的页面检查点','file')+metric('请求错误',fmt(c.errors?.length),'本轮尚未解决的请求','shield')+'</div>'+
 '<div class="settings-grid"><section class="card pad"><div class="actions" style="justify-content:space-between;margin-bottom:18px"><h2 style="margin:0">采集状态</h2>'+badge(stages[c.stage]||c.stage,c.stage==='error'?'amber':'purple')+'</div><p>'+esc(c.message||'等待采集')+'</p><progress style="width:100%;accent-color:var(--primary);height:8px" value="'+percent+'" max="100" aria-label="采集进度"></progress><div class="detail-grid"><div><dt>开始时间</dt><dd>'+date(c.started_at)+'</dd></div><div><dt>最后状态更新</dt><dd>'+date(c.updated_at||c.finished_at)+'</dd></div><div><dt>近期补采</dt><dd>'+fmt(c.repair_pages_done)+' 页</dd></div><div><dt>源站最新返回</dt><dd>'+fmt(c.source_total_latest)+(c.filters?.peer_type==='group'?' 条群组':' 条资源')+'</dd></div></div><div class="actions">'+button('start-collection',icon('refresh')+' 开始 / 续采','primary',!owner||c.stage==='running'?'disabled':'')+button('stop-collection','暂停采集','',!owner||c.stage!=='running'?'disabled':'')+'</div><hr class="subtle-line"><p class="small">源站为实时分页，无法保证同一时刻的静态快照。已有账号登录和 Chrome 调试连接可用时，采集器才能刷新源站会话。</p>'+(c.errors?.length?'<div class="form-error">'+c.errors.map(e=>'第 '+esc(e.page)+' 页：'+esc(e.error)).join('<br>')+'</div>':'')+'</section><div><section class="card pad" style="margin-bottom:22px"><h2>自动更新</h2><form id="collectionSettings"><label class="check-label" style="margin:20px 0"><input type="checkbox" name="auto_enabled" '+(d.settings.auto_enabled?'checked':'')+' '+(!owner?'disabled':'')+'> 启用定时采集</label><label class="field">更新间隔<select name="interval_hours" '+(!owner?'disabled':'')+'>'+[1,6,12,24].map(n=>'<option value="'+n+'" '+(d.settings.interval_hours===n?'selected':'')+'>'+n+' 小时</option>').join('')+'</select></label><p class="small">保持本地服务和已登录的 Chrome 开启，下一次计划：'+date(d.next_auto_at*1000)+'</p><div class="form-error" role="alert"></div><button class="button primary" type="submit" '+(!owner?'disabled':'')+'>保存更新设置</button></form></section><section class="card pad"><h2>导出本地资源</h2><p class="small">包含当前已保存数据，JSON 保留原始字段。</p><div class="actions"><a class="button" href="/api/export?scope=groups&format=csv">'+icon('download')+' 群组 CSV</a><a class="button" href="/api/export?scope=groups&format=json">群组 JSON</a><a class="button" href="/api/export?scope=all&format=json">全部 JSON</a></div></section></div></div>';
}
function settingsPage(){
 return pageHead('账号设置','管理个人资料与工作空间登录安全。')+
 '<div class="settings-grid"><section class="card pad"><h2>个人资料</h2><p class="small">用于工作空间中的身份展示。</p><form id="profileForm"><label class="field">显示名称<input class="input" name="name" required maxlength="60" value="'+esc(S.user.name)+'"></label><label class="field">邮箱地址<input class="input" type="email" value="'+esc(S.user.email)+'" readonly><span class="hint">当前版本不支持更换登录邮箱。</span></label><div class="detail-grid"><div><dt>角色</dt><dd>'+(S.user.role==='owner'?'工作空间所有者':'成员')+'</dd></div><div><dt>注册时间</dt><dd>'+date(S.user.created_at)+'</dd></div></div><div class="form-error" role="alert"></div><button class="button primary" type="submit">保存资料</button></form></section><section class="card pad"><h2>修改密码</h2><p class="small">修改后会撤销此账号的旧登录会话。</p><form id="passwordForm"><input type="text" name="username" autocomplete="username" value="'+esc(S.user.email)+'" hidden><label class="field">当前密码<input class="input" type="password" name="current_password" autocomplete="current-password" maxlength="256" required></label><label class="field">新密码<input class="input" type="password" name="new_password" autocomplete="new-password" minlength="10" maxlength="256" required><span class="hint">至少 10 个字符。</span></label><label class="field">确认新密码<input class="input" type="password" name="confirm_password" autocomplete="new-password" minlength="10" maxlength="256" required></label><div class="form-error" role="alert"></div><button class="button primary" type="submit">更新密码</button></form></section></div>';
}
async function render(){
 const version=++S.routeVersion, route=location.pathname;
 S.groupController?.abort();
 if(protectedRoutes.includes(route) && !S.user){navigate('/login?next='+encodeURIComponent(route),true);return;}
 if((route==='/login'||route==='/register')&&S.user){navigate('/dashboard',true);return;}
 document.title=(titles[route]||({'/login':'登录','/register':'创建工作空间'}[route])||'Telegram 运营平台')+' · TG 工作台';
 if(route==='/'||route==='/index.html'){$('app').innerHTML=landing();return;}
 if(['/tg-qunfa','/feiji-qunfa','/telegram-qunfa'].includes(route)){$('app').innerHTML=article();return;}
 if(route==='/login'||route==='/register'){$('app').innerHTML=auth(route.slice(1));return;}
 if(route==='/group-search'){
  currentItems=[];
  const p=new URLSearchParams(location.search);
  S.filters={q:p.get('q')||'',type:p.get('type')||'all',category:p.get('category')==='ai'?'ai':'all',batch:p.get('category')==='ai'&&/^[1-9][0-9]{0,5}$/.test(p.get('batch')||'')?p.get('batch'):'all',visibility:p.get('visibility')||'all',sort:p.get('sort')||'recent',min_members:p.get('min_members')||'',max_members:p.get('max_members')||'',page_size:['20','50','100'].includes(p.get('page_size'))?p.get('page_size'):'20'};
  S.page=Math.max(1,Number(p.get('page'))||1);
  $('app').innerHTML=appShell(resourcePage());selection();loadGroups();return;
 }
 $('app').innerHTML=appShell('<div class="boot">正在读取工作空间…</div>');
 try{
  const [w,d]=await Promise.all([api('/api/workspace'),api('/api/status')]);
  if(version!==S.routeVersion)return;
  S.workspace=w;S.stats=d;
  if(route==='/join-groups'){
   const content=await joinPage();if(version!==S.routeVersion)return;
   $('app').innerHTML=appShell(content);syncMenu();return;
  }
  if(operationRoutes.includes(route)){
   const content=await operationsPage(route);
   if(version!==S.routeVersion)return;
   $('app').innerHTML=appShell(content);syncMenu();return;
  }
  const view={'/dashboard':dashboard,'/telegram-accounts':accountsPage,'/tasks':tasksPage,'/broadcasts':tasksPage,'/collection':collectionPage,'/account-settings':settingsPage}[route];
  $('app').innerHTML=appShell(view?view():empty('页面不存在','返回工作台继续。',link('/dashboard','返回工作台','button primary')));
 }catch(e){
  if(version!==S.routeVersion)return;
  if(e.status===401){S.user=null;navigate('/login?next='+encodeURIComponent(route),true);return;}
  $('app').innerHTML=appShell(empty('暂时无法读取工作空间',esc(e.message),button('retry-page','重新加载','primary')));
 }
}

let currentItems=[];
async function loadGroups(){
 S.groupController?.abort();const controller=S.groupController=new AbortController();
 const params=new URLSearchParams({...S.filters,page:S.page});
 if(location.pathname!=='/group-search')return;
 history.replaceState({},'',location.pathname+'?'+params);
 $('resultCount').textContent='正在查询…';
 try{
  const d=await api('/api/groups?'+params,undefined,controller.signal);
  if(controller!==S.groupController||!$('resourceRows'))return;
  currentItems=d.items;S.page=d.page;S.pages=d.pages;
  const types={group:'普通群',megagroup:'超级群',supergroup:'超级群',channel:'频道',unknown:'未分类'};
  $('resourceRows').innerHTML=d.items.map(x=>'<tr><td><input type="checkbox" data-select="'+esc(x.id)+'" '+(S.selected.has(String(x.id))?'checked':'')+' aria-label="选择 '+esc(x.title||x.id)+'"></td><td><button class="resource-name" data-action="resource-detail" data-id="'+esc(x.id)+'" title="'+esc(x.title)+'">'+esc(x.title||'未命名资源')+'</button><div class="resource-handle">'+esc(x.username?'@'+x.username:'ID '+x.id)+'</div>'+(x._local_category?.category==='ai'?badge('AI 类群组','purple'):'')+'</td><td>'+badge(esc(types[x.peer_type]||x.peer_type))+'</td><td>'+badge(x.is_public===true?'公开':x.is_public===false?'私有':'未知',x.is_public===true?'green':x.is_public===false?'amber':'')+'</td><td>'+(x.member_count==null?'—':fmt(x.member_count))+'</td><td><span class="resource-desc">'+esc(x.description||'暂无简介')+'</span></td><td>'+button('resource-detail','详情','sm','data-id="'+esc(x.id)+'"')+'</td></tr>').join('');
  $('resourceEmpty').hidden=!!d.items.length;
  $('resourceEmpty').innerHTML=d.items.length?'':empty('没有匹配的资源','试试其他关键词，或重置筛选条件。',button('reset-filters','重置筛选','soft'),'search');
  $('resultCount').textContent='匹配 '+fmt(d.total)+' 条资源';
  $('showingLabel').textContent=d.total?'第 '+fmt((d.page-1)*d.page_size+1)+'–'+fmt(Math.min(d.total,d.page*d.page_size))+' 条，共 '+fmt(d.total)+' 条':'0 条记录';
  $('pageLabel').textContent=d.page+' / '+fmt(d.pages);$('prevPage').disabled=d.page<=1;$('nextPage').disabled=d.page>=d.pages;
  selection();
 }catch(e){if(e.name!=='AbortError'&&controller===S.groupController&&$('resultCount')){$('resultCount').textContent='查询失败：'+e.message;$('resourceRows').innerHTML='';currentItems=[];selection();}}
}
function selection(){
 if(!$('selectionBar'))return;
 $('selectionBar').innerHTML=S.selected.size?'<div class="selection-bar"><span>已选择 '+fmt(S.selected.size)+' 个目标（跨页保留）</span><div class="actions">'+button('clear-selection','清空','sm')+button('join-selected',icon('users')+' 创建加群任务','sm')+button('draft-selected',icon('plus')+' 保存为任务草稿','sm primary')+'</div></div>':'';
 if($('selectPage')){$('selectPage').checked=currentItems.length>0&&currentItems.every(x=>S.selected.has(String(x.id)));$('selectPage').indeterminate=currentItems.some(x=>S.selected.has(String(x.id)))&&!$('selectPage').checked;}
}
function modal(title,body,foot='',keepTelegramQR=false){
 if(!keepTelegramQR)stopTelegramQR();
 $('modal').innerHTML='<div class="modal-head"><h2 id="modalTitle">'+esc(title)+'</h2>'+button('close-modal',icon('close'),'icon-only','aria-label="关闭对话框"')+'</div><div class="modal-body">'+body+'</div>'+(foot?'<div class="modal-foot">'+foot+'</div>':'');
 if(!$('modal').open)$('modal').showModal();
}
async function accountModal(id){
 const w=await api('/api/workspace');S.workspace=w;
 const a=id?w.accounts.find(x=>x.id===id):null;S.account=a;
 modal(a?'编辑账号资料':'添加 Telegram 账号资料',
  notice('先保存手机号和备注，再点击连接账号，选择扫码或验证码登录。')+
  '<form id="accountForm"><label class="field">账号名称<input class="input" name="name" required maxlength="80" placeholder="例如：客服账号" value="'+esc(a?.name)+'"></label><label class="field">手机号<input class="input" name="phone" type="tel" required placeholder="+国家码和手机号" maxlength="24" value="'+esc(a?.phone)+'"><span class="hint">使用国际格式，例如 +8613800000000。</span></label><label class="field">备注<textarea name="notes" maxlength="1000" placeholder="这个账号主要用于…">'+esc(a?.notes)+'</textarea></label><div class="form-error" role="alert"></div><button class="button primary wide" type="submit">保存账号资料</button></form>');
}
async function draftModal(id){
 if(!S.user){toast('请先登录再保存任务');navigate('/login?next=/tasks');return;}
 S.workspace=await api('/api/workspace');
 const d=id?S.workspace.drafts.find(x=>x.id===id):null;
 S.draft=d||{targets:[...S.selected.keys()]};
 const accounts=S.workspace.accounts;
 modal(d?'编辑任务草稿':'创建任务草稿',
  '<form id="draftForm"><label class="field">任务名称<input class="input" name="name" required maxlength="120" placeholder="例如：本周群组公告" value="'+esc(d?.name)+'"></label><label class="field">关联账号<select name="account_id"><option value="">稍后指定账号</option>'+accounts.map(a=>'<option value="'+a.id+'" '+(a.id===d?.account_id?'selected':'')+'>'+esc(a.name)+(a.status==='connected'?'（已连接）':'（尚未连接）')+'</option>').join('')+'</select></label><div class="field">目标资源 <span class="hint">'+S.draft.targets.length+' 个目标 · '+(d?'编辑保留原目标':'来自你在资源库中的选择')+'</span></div><div class="target-list">'+S.draft.targets.slice(0,30).map(id=>'<span class="target-chip">'+esc(S.selected.get(id)?.title||'ID '+id)+'</span>').join('')+(S.draft.targets.length>30?'<span class="small muted">及其他 '+(S.draft.targets.length-30)+' 个目标</span>':'')+'</div><label class="field">消息内容<textarea name="content" required maxlength="10000" placeholder="编写你计划发送的消息…">'+esc(d?.content)+'</textarea><span class="hint">保存草稿不会发送。创建发送任务时会再次确认内容、目标和时间。</span></label><div class="form-error" role="alert"></div><div class="actions"><button class="button primary" type="submit">保存草稿</button>'+button('close-modal','取消','')+'</div></form>');
}
async function resourceDetail(id){
 modal('资源详情','<p>正在读取资源…</p>');
 try{
  const x=await api('/api/resource?id='+encodeURIComponent(id)), url=safeURL(x.public_link);
  modal(x.title||'资源详情','<div class="actions">'+badge(esc({group:'普通群',megagroup:'超级群',channel:'频道'}[x.peer_type]||x.peer_type),'purple')+badge(x.is_public===true?'公开':x.is_public===false?'私有':'未知',x.is_public?'green':'amber')+'</div>'+resourceCategoryInfo(x)+'<dl class="detail-grid"><div><dt>用户名</dt><dd>'+esc(x.username?'@'+x.username:'源站未提供')+'</dd></div><div><dt>成员数</dt><dd>'+(x.member_count==null?'源站未提供':fmt(x.member_count))+'</dd></div><div><dt>最后发现</dt><dd>'+date(x.last_seen_at)+'</dd></div><div><dt>允许发送消息</dt><dd>'+(x.default_can_send_messages===true?'允许':x.default_can_send_messages===false?'不允许':'未知')+'</dd></div></dl><h3>资源简介</h3><div class="detail-description">'+esc(x.description||'暂无简介')+'</div>',
    (url?'<a href="'+esc(url)+'" class="button" target="_blank" rel="noopener noreferrer">'+icon('link')+' 打开 Telegram</a>':'')+button('select-resource','添加到已选目标','primary','data-id="'+esc(x.id)+'" data-title="'+esc(x.title)+'"'));
 }catch(e){modal('无法读取资源','<p>'+esc(e.message)+'</p>');}
}
function resourceCategoryInfo(x){
 if(!['group','megagroup','supergroup'].includes(x.peer_type))return '';
 const c=x._local_category,inAI=c?.category==='ai';
 return '<section class="resource-category-detail"><strong>资源分组：'+(inAI?'AI 类群组':'未加入 AI 分组')+'</strong><p class="small muted">'+esc(c?.reason||'自动分类暂未识别到明确的 AI 主题。')+(c?.source==='rule'?' · 自动分类，可能存在误判。':'')+'</p>'+(S.user?.role==='owner'?'<div class="actions">'+button('resource-category-set',inAI?'移出 AI 分组':'加入 AI 分组','sm','data-id="'+esc(x.id)+'" data-mode="'+(inAI?'exclude':'include')+'"')+(c?.source==='manual'?button('resource-category-set','恢复自动分类','sm','data-id="'+esc(x.id)+'" data-mode="auto"'):'')+'</div>':'')+'</section>';
}
function confirmModal(title,text,action,id){modal(title,'<p>'+esc(text)+'</p>',button('close-modal','取消')+button(action,'确认删除','danger','data-id="'+esc(id)+'"'));}

document.addEventListener('click',async e=>{
 const anchor=e.target.closest('a[data-link]');
 if(anchor && !e.ctrlKey&&!e.metaKey&&!e.shiftKey&&e.button===0){e.preventDefault();navigate(anchor.getAttribute('href'));return;}
 const el=e.target.closest('[data-action]');
 if(!el||el.disabled)return;
 const action=el.dataset.action,id=el.dataset.id;
 try{
  if(action==='close-modal'){stopTelegramQR();$('modal').close();}
  else if(action==='toggle-menu'){document.querySelector('.sidebar')?.classList.toggle('open');document.querySelector('.sidebar-shade')?.classList.toggle('open');syncMenu();}
  else if(action==='logout'){await stopTelegramQR();await api('/api/auth/logout',{});S.user=null;S.workspace=null;S.selected.clear();Object.assign(J,{jobs:[],resources:[],draft:null,revision:null,nonce:null});navigate('/');toast('已退出登录');}
  else if(action==='retry-page')init();
  else if(await telegramAction(action,el,id)){}
  else if(await operationsAction(action,el,id)){}
  else if(await joinAction(action,el,id)){}
  else if(action==='new-account')await accountModal();
  else if(action==='edit-account')await accountModal(id);
  else if(action==='remove-account')confirmModal('移除账号资料','移除后，关联任务草稿会保留，但不再关联这个账号。','confirm-remove-account',id);
  else if(action==='confirm-remove-account'){await api('/api/accounts/delete',{id});$('modal').close();toast('账号资料已移除');render();}
  else if(action==='new-draft'||action==='draft-selected')await draftModal();
  else if(action==='edit-draft')await draftModal(id);
  else if(action==='remove-draft')confirmModal('删除任务草稿','删除后无法从任务中心恢复这份草稿。','confirm-remove-draft',id);
  else if(action==='confirm-remove-draft'){await api('/api/drafts/delete',{id});$('modal').close();toast('草稿已删除');render();}
  else if(action==='resource-detail')await resourceDetail(id);
  else if(action==='resource-category')navigate('/group-search?'+new URLSearchParams({...S.filters,category:el.dataset.category,batch:'all',page:1}));
  else if(action==='resource-batch')navigate('/group-search?'+new URLSearchParams({...S.filters,category:'ai',batch:el.dataset.batch,page:1}));
  else if(action==='resource-category-set'){
   el.disabled=true;const r=await api('/api/resource-categories/update',{id,action:el.dataset.mode});
   S.stats=await api('/api/status');await render();await resourceDetail(id);toast(r.message);
  }
  else if(action==='select-resource'){if(S.selected.size>=500&&!S.selected.has(id))throw Error('最多选择 500 个目标');S.selected.set(id,{id,title:el.dataset.title});$('modal').close();if($('resourceRows')){document.querySelectorAll('[data-select]').forEach(x=>x.checked=S.selected.has(x.dataset.select));selection();}toast('已加入目标选择');}
  else if(action==='clear-selection'){S.selected.clear();document.querySelectorAll('[data-select]').forEach(x=>x.checked=false);selection();}
  else if(action==='prev-page'||action==='next-page'){S.page+=action==='prev-page'?-1:1;loadGroups();}
  else if(action==='reset-filters'){navigate('/group-search?'+new URLSearchParams({category:S.filters.category||'all',batch:S.filters.batch||'all'}));}
  else if(action==='export-menu')modal('导出资源','<p>导出当前数据库中已保存的资源，JSON 保留原始字段。筛选条件不会限制这些完整文件。</p><div class="actions"><a href="/api/export?scope=groups&format=csv" class="button">群组 CSV</a><a href="/api/export?scope=groups&format=json" class="button">群组 JSON</a><a href="/api/export?scope=all&format=csv" class="button">全部 CSV</a><a href="/api/export?scope=all&format=json" class="button">全部 JSON</a></div>');
  else if(action==='refresh-data'){S.stats=await api('/api/status');render();toast('状态已刷新');}
  else if(action==='start-collection'||action==='stop-collection'){el.disabled=true;const r=await api(action==='start-collection'?'/api/collect':'/api/stop',{});toast(r.message);setTimeout(render,1200);}
 }catch(err){toast(err.message);el.disabled=false;}
});
document.addEventListener('input',e=>{
 const fields={searchInput:'q',minMembers:'min_members',maxMembers:'max_members'};
 if(fields[e.target.id]){
  S.filters[fields[e.target.id]]=e.target.value;S.page=1;
  clearTimeout(S.debounce);S.debounce=setTimeout(loadGroups,300);
 }
});
document.addEventListener('change',e=>{
 const ids={typeFilter:'type',visibilityFilter:'visibility',sortFilter:'sort',pageSize:'page_size'};
 if(ids[e.target.id]){S.filters[ids[e.target.id]]=e.target.value;S.page=1;loadGroups();}
 if(e.target.matches('[data-select]')){
  const id=e.target.dataset.select,x=currentItems.find(x=>String(x.id)===id);
  if(e.target.checked){if(S.selected.size>=500){e.target.checked=false;toast('最多选择 500 个目标');return;}S.selected.set(id,x);}else S.selected.delete(id);
  selection();
 }
 if(e.target.id==='selectPage'){
  if(e.target.checked && new Set([...S.selected.keys(),...currentItems.map(x=>String(x.id))]).size>500){e.target.checked=false;toast('最多选择 500 个目标');return;}
  currentItems.forEach(x=>e.target.checked?S.selected.set(String(x.id),x):S.selected.delete(String(x.id)));
  document.querySelectorAll('[data-select]').forEach(x=>x.checked=S.selected.has(x.dataset.select));selection();
 }
});
document.addEventListener('submit',async e=>{
 const form=e.target;if(!form.getAttribute('id'))return;e.preventDefault();
 const error=form.querySelector('.form-error'),submit=form.querySelector('button[type=submit]');if(error)error.textContent='';
 const payload=Object.fromEntries(new FormData(form)),submitText=submit?.textContent;if(submit){submit.disabled=true;submit.textContent='处理中…';}
 try{
  if(await telegramSubmit(form,payload)){}
  else if(await operationsSubmit(form,payload)){}
  else if(await joinSubmit(form,payload)){}
  else if(form.getAttribute('id')==='authForm'){
   const r=await api('/api/auth/'+form.dataset.mode,payload);S.user=r.user;
   const next=new URLSearchParams(location.search).get('next');
   navigate(protectedRoutes.includes(next)?next:'/dashboard',true);toast('工作空间已打开');
  }else if(form.getAttribute('id')==='accountForm'){
   const r=await api('/api/accounts/save',{...payload,id:S.account?.id});$('modal').close();toast(r.message);render();
  }else if(form.getAttribute('id')==='draftForm'){
   const r=await api('/api/drafts/save',{...payload,id:S.draft?.id,targets:S.draft.targets,kind:'broadcast'});$('modal').close();toast(r.message);navigate('/tasks');
  }else if(form.getAttribute('id')==='profileForm'){
   const r=await api('/api/profile',payload);S.user=r.user;toast(r.message);render();
  }else if(form.getAttribute('id')==='passwordForm'){
   if(payload.new_password!==payload.confirm_password)throw Error('两次输入的新密码不一致');
   const r=await api('/api/password',payload);form.reset();toast(r.message);
  }else if(form.getAttribute('id')==='collectionSettings'){
   const r=await api('/api/settings',{auto_enabled:form.elements.auto_enabled.checked,interval_hours:Number(payload.interval_hours)});S.stats.settings=r;toast('自动更新设置已保存');
  }
 }catch(err){if(error)error.textContent=err.message;else toast(err.message);}
 finally{if(submit){submit.disabled=false;submit.textContent=submitText;}}
});
document.addEventListener('keydown',e=>{
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'&&$('searchInput')){e.preventDefault();$('searchInput').focus();}
 if(e.key==='Escape'&&document.querySelector('.sidebar.open')&&!$('modal').open){
  document.querySelector('.sidebar')?.classList.remove('open');document.querySelector('.sidebar-shade')?.classList.remove('open');syncMenu();document.querySelector('.mobile-toggle')?.focus();
 }
});
new MutationObserver(syncMenu).observe($('app'),{childList:true,subtree:true});
window.addEventListener('resize',syncMenu);
window.addEventListener('popstate',()=>{if($('modal').open)$('modal').close();render();});
setInterval(async()=>{
 if(document.hidden||!['/dashboard','/collection'].includes(location.pathname))return;
 try{
  const previous=S.stats?.collection?.stage;
  const data=await api('/api/status');S.stats=data;
  // Preserve forms and focus while they are being edited; user can explicitly refresh.
  if(location.pathname==='/collection'&&(data.collection.stage==='running'||previous!==data.collection.stage)&&!$('collectionSettings')?.contains(document.activeElement)&&!$('modal').open)render();
 }catch{}
},15000);
async function init(){
 const results=await Promise.allSettled([api('/api/auth/me'),api('/api/status')]);
 if(results[0].status==='fulfilled')S.user=results[0].value.user;
 if(results[1].status==='fulfilled')S.stats=results[1].value;
 else { $('app').innerHTML=empty('本地服务暂时不可用','请启动网站服务，再刷新此页面。',button('retry-page','重试','primary'));return; }
 render();
}
init();
