# TG 工作台

本地运行的 Telegram 资源搜索与账号管理网站，使用 Python、SQLite 和原生 JavaScript，支持资源分组、扫码登录、自动加群和消息任务。

## 安装与启动

需要 Python 3.10+。在已登录且具有仓库访问权限的 GitHub 账号下克隆项目：

```powershell
git clone https://github.com/shibaweidu/tg-workspace.git
cd tg-workspace
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe server.py --port 8766
```

访问 http://127.0.0.1:8766 并注册本地用户。macOS/Linux 可使用 `.venv/bin/python` 执行以上 Python 命令。

仓库包含源码、测试和启动脚本。运行数据库、采集数据、账号凭据、Telegram 会话、加密密钥及日志不上传 GitHub；首次启动会创建空的本地数据库，需要重新导入或采集资源，并在页面配置自己的 Telegram 应用和账号。需要本机 Python 后台可以访问 Telegram；若无法直连，在“账号代理”设置对应账号的 SOCKS5/HTTP 代理。

安装依赖后运行隔离测试：

```powershell
python -m unittest test_workspace test_delivery test_operations test_join test_categories test_qr -q
```

入口：http://127.0.0.1:8766

双击“启动网站.cmd”，在浏览器中访问上面的地址。首页现为整站产品入口，群组资源库位于 /group-search，旧版资料库保留在 /library。

## 本阶段可用功能

| 页面 | 能力 |
|---|---|
| / | 产品官网、真实资源统计、功能与使用流程 |
| /register、/login | 本地注册、登录、退出，Cookie 会话 |
| /dashboard | 真实群/频道统计、当前用户账号和草稿数量、活动记录 |
| /group-search | 搜索、AI 类群组分组、类型/状态/成员数筛选、排序、分页、详情、跨页目标选择、导出 |
| /telegram-accounts | 应用配置、扫码/验证码/二步验证登录、会话检查、退出与加密会话管理 |
| /tasks、/broadcasts | 草稿管理、立即/定时发送队列、暂停/继续/取消、逐目标执行结果 |
| /join-groups | 独立自动加群任务、链接/资源库选群、预览确认、暂停/继续/取消、审批与成员身份核对 |
| /message-center | 已有会话同步、消息查看、私信队列、人工接管 |
| /auto-replies | 关键词响应、自动回复、欢迎语、群关键词过滤、监听开关、匹配测试、执行结果与失败重试 |
| /membership、/commissions | 套餐额度、人工审核订单、邀请代码与佣金记账 |
| /proxies、/ai-model | 按账号配置代理；AI 模型配置、调用测试与费用估算 |
| /admin | 订单审核、用户停用、套餐/支付说明、佣金比例、Worker 状态、用量、日志和备份下载 |
| /collection | 采集状态、开始/续采、暂停、定时设置、资源导出 |
| /account-settings | 修改显示名称、修改密码并撤销旧会话 |
| /tg-qunfa | 当前产品范围及后续接入说明 |

首次注册的用户成为所有者，可以管理共享资源采集；其他用户可以保存各自的账号和草稿。资源库是本机用户共享的，账号资料和草稿按用户隔离。密码采用随机盐 PBKDF2 哈希，登录使用 HttpOnly、SameSite Cookie，会话保存在服务端。

**已接入 Telethon 账号登录、发送队列、独立自动加群、消息监听、规则自动化和商业管理后台。** 群发使用已加入的群组/频道，自动加群需单独预览确认创建任务，私信只支持已有会话。Bot 专属接入、AI 自动客服、自动在线收款和提现尚未接入。

## 连接 Telegram 与执行任务

1. 在 `https://my.telegram.org/apps` 创建自己的 Telegram 应用，获取 API ID 和 API Hash。
2. 在“Telegram 账号 → 应用配置”保存配置；每个本地用户的配置独立。
3. 添加账号名称和国际格式手机号，点击“连接账号”，可选择“手机号登录”或“扫码登录”。手机号方式输入 Telegram 官方消息或短信中的验证码；扫码方式使用手机 Telegram 的“设置 → 设备 → 连接桌面设备 / 扫描二维码”扫描并确认。扫描时须切换到账号资料中的手机号，系统会核对，防止绑定错误账号。启用二步验证的账号仍需输入 Telegram 密码。
4. 在资源库选择目标并保存草稿，指定已连接账号。在任务中心点击“创建发送任务”，核对消息、账号、目标和时间，再确认创建。
5. 在执行记录查看逐目标结果。暂停或取消会阻止后续目标，已在途请求可能完成；已发送消息不会撤回。

需要本机网络能够访问 Telegram API；可在“账号代理”配置 SOCKS5/HTTP。没有实际 Telegram 应用配置和账号时，可以管理草稿和规则，但无法真实收发消息。

发送仅使用纯文本，每条最多 4096 个 UTF-16 单位，默认间隔 10 秒（最少 3 秒）。任务创建时固定内容、账号和目标快照，后续编辑草稿不会改变在执行的任务。一个草稿不能同时创建多个未完成任务。

队列和结果持久化在 SQLite，定时任务需保持本地服务运行；停机期间到期的排队任务会在服务恢复后继续执行。同一账号共享发送等待窗口；Telegram 返回 FloodWait/SlowMode 时按要求等待。未加入目标时标记跳过，无权限标记失败。网络中断导致结果无法确认时暂停任务，并标记“结果待核对”；不会自动重发该目标。重启时，在途目标同样标为待核对，原运行任务暂停，确认记录后可继续未发送目标。

API Hash 和 Telegram StringSession 加密保存；Windows 使用当前登录用户的 DPAPI，其他系统使用本机 Fernet 密钥。验证码与二步验证密码仅保留在本次请求/登录流程中，不写入数据库。Windows 数据不能直接复制给另一台机器或另一系统用户解密；其他系统备份需同时保留 `data/telegram-secret.key`，并限制其访问权限。加密不能抵御已控制本机同一用户账户的程序。

扫码二维码使用本地 `qrcode` 生成，不依赖第三方图片服务，授权令牌仅在当前流程内存中使用。页面显示剩余有效时间和授权状态，过期可“重新生成二维码”；关闭弹窗、切换手机号登录或离开页面会取消当前扫码等待。每次重新生成使用独立会话，旧二维码不能绑定新的流程。扫码后的二步验证最长保留 10 分钟，密码连续错误最多 8 次。服务重启后需要重新扫码；已经完成登录并加密保存的会话仍可使用。扫码也需要自己的 API ID/API Hash 和可用的 Telegram 网络连接。

“会话已保存”表示保存了已授权会话；点击“检查连接”核对 Telegram 侧状态。“断开连接”会退出此会话、清除本地会话并暂停关联任务，需要网络可达；也可在 Telegram 官方客户端的设备管理中撤销会话。

本版本继续使用 Python 标准库 HTTP 服务和 SQLite，供本机使用，仅监听 127.0.0.1。当前没有邮箱验证或找回密码服务。若部署为多人公网服务，下一阶段需迁移生产级 API 服务，配置 HTTPS、持久化任务队列和运维监控。

## 开发与验证

不需要 Node 构建步骤。启动：

    python -m pip install -r requirements.txt
    python server.py --port 8766

新页面由 index.html、web.js、telegram-ui.js、operations-ui.js、join-ui.js、web.css 提供；用户数据由 workspace_store.py 管理，delivery_store.py 保存群发队列，join_store.py 和 telegram_join.py 保存并执行加群任务，automation_store.py 保存会话、规则和私信队列，business_store.py 保存套餐、订单、代理、AI 用量、审计和备份，telegram_runtime.py 管理异步连接、消息监听和执行，local_secrets.py 提供本地加密。旧版使用 library.html、app.js、styles.css。

## 自动加群

连接自己的 Telegram 账号后，进入“自动加群”（`/join-groups`），粘贴公开群用户名或邀请链接，每行一个；也可在资源库勾选资源后点击“创建加群任务”。选择账号、任务名称与间隔，预览去重及无效目标，核对后确认创建。支持 `@username`、`https://t.me/username`、`https://t.me/+邀请代码` 和 `https://t.me/joinchat/邀请代码`。

每个任务最多 100 个输入目标，默认间隔 60 秒，可设 30–3600 秒。执行前检查成员身份，已在群内直接记录“原已在群”；只处理群组，用户、Bot、频道、付费邀请和无效资源会排除或记录失败。创建加群任务不会发送消息，群发任务也不会附带加群。

需要管理员审批的群记录为“已提交申请 · 待审批”，不代表加入成功，也不会重复申请。详情中的“核对是否已加入”只查询成员身份。加群与消息队列共享账号等待窗口，Telegram 要求等待时自动延后；账号达到加群上限或受限时暂停。网络中断、响应无法确认或服务重启中断的在途请求标为“结果待核对”，不会自动重试；只有明确失败的目标可通过“重试失败”重新排队。

任务和逐目标结果持久保存，未处理目标可暂停、继续或取消。取消不会退群，也不撤销已提交的入群申请。服务需保持运行；断开账号会暂停相关任务，重新连接后可手动继续。未完成的任务、待审批和待核对结果会阻止同一账号重复提交相同目标。

## AI 类群组分组

资源库点击“AI 类群组”，或打开 `/group-search?category=ai`。分组只包含群组，支持继续搜索、成员数筛选、分页、勾选并创建加群任务。AI 分类下按资源 ID 升序分成每 500 个一组的二级分类，最后一组保留剩余数量，入口例如 `/group-search?category=ai&batch=1`。搜索、成员数筛选和显示排序在当前二级组内进行，不改变该组范围；重置筛选也保留当前组。

页面提供当前分组 CSV / JSON 导出：选择二级组时只导出该组，选择“全部 AI 群”时导出整个 AI 分类，导出不受搜索词或页码限制。资源采集、手动归类改变 AI 群列表后，按同一 ID 顺序更新二级分组，组成员可能随数据调整。现有 3,113 个 AI 群对应前六组各 500 个、第七组 113 个。

`resource_categories.py` 根据群名、用户名和简介中的明确 AI 主题词分类，包括人工智能、机器学习、大模型、ChatGPT、DeepSeek、Claude、Midjourney、ComfyUI 等。英文词使用边界匹配，避免 Airdrop、Affairs、Paid 等词误命中 AI。简介中的孤立广告提及和链接不作为充分依据。首次打开数据库会为已有资源补充分组，后续导入或采集自动更新。

分类属于关键词初筛，可能有误判或遗漏。在群详情可查看分类依据；所有者可以“加入 AI 分组”“移出 AI 分组”或“恢复自动分类”。手动修正会保留，不被后续采集覆盖。分组元数据存于资源库独立表 `resource_categories`，不改写源站原始记录，也不会触发加群。

测试：`python -m unittest test_categories -v`。覆盖旧库补分组、新数据分类、误命中、手动修正保留、搜索组合、群/频道区分、导出范围和所有者权限。

## 第三、第四阶段

消息中心支持手动同步账号已有会话、查看消息、人工接管和私信发送队列。自动化规则支持私信/群组关键词回复、群欢迎语和关键词过滤；规则默认关闭，过滤动作仅在账号实际具备删除消息的管理员权限时执行。发送结果写入持久队列，明确失败可以人工重试，网络中断或超时会标为待核对且不会自动重发。账号监听和自动化仅处理已连接账号的现有会话，不自动加入群组或批量抓取成员。

商业管理包含基础版/专业版套餐、人工审核订单、邀请代码、佣金记录、账号 SOCKS5/HTTP 代理、管理员用户权限、队列心跳、审计、SQLite 工作空间备份，以及兼容 Chat Completions 的 AI 模型配置和测试用量记录。支付目前是人工核验流程，不伪造在线收款或自动开通；佣金只记录应收，不自动提现；AI 费用按管理员配置报价估算，不自动扣款。备份只包含工作空间数据库，资源库仍保留在原位置。

集成测试使用临时数据库，不改动正式账号和资源：

    python -m unittest test_workspace test_delivery -v
    python -m unittest test_operations -v
    python -m unittest test_join -v
    python -m unittest test_qr -v
    python verify_collector.py
    node --check web.js
    node --check telegram-ui.js
    node --check operations-ui.js
    node --check join-ui.js

覆盖注册、登录、退出、密码变更与旧会话撤销、用户隔离、账号/草稿增删改、资源筛选排序、Telegram 登录与二步验证、加密往返、任务快照、调度、暂停/取消、限流、权限错误和重启恢复。加群测试覆盖链接去重、资源预览、重复提交保护、已加入、审批、邀请过期、付费/频道排除、共享限流和结果核对。Telegram 测试使用本地假适配器，不连接 Telegram、加入真实群组或发送真实消息。浏览器流程使用独立临时数据库，不创建正式管理员账号。真实账号联网登录、加群和群组发送仍需使用自己的账号进行验收。

## 数据文件

- data/groups.sqlite3：持续写入的 SQLite 数据库；资源 ID 为唯一键。
- data/groups.csv、data/groups.json：仅普通群、超级群。
- data/all_resources.csv、data/all_resources.json：全部资源，包含频道。
- data/collection-status.json：进度、源站计数和错误记录。
- data/workspace.sqlite3：本地用户、登录会话、账号资料、草稿、加密 Telegram 应用/会话、发送队列、逐目标结果及活动记录。

文件导出在每轮采集结束或暂停时生成。页面导出按钮可随时导出当前已保存数据。JSON 保留源站的全部字段及 null 值，CSV 使用 UTF-8 BOM。

## 采集

依赖：Python 3.10+。安装：

    python -m pip install -r requirements.txt

保持当前支持远程调试的 Chrome 开启，并登录 https://www.tgegg.com/group-search 。采集器通过该浏览器中已有的站点会话访问资源列表，登录材料仅在进程内存中使用，不写入项目。

双击“启动采集.cmd”或运行：

    python -u collect_tgegg.py --workers 6

单独对群组列表做一次补充遍历（不会删除已保存的频道）：

    python -u collect_tgegg.py --workers 8 --fresh --peer-type group --repair-pages 100

换用成员数排序核对并合并遗漏记录：

    python -u collect_tgegg.py --workers 8 --fresh --peer-type group --sort-by member_count --repair-pages 100

同一时间保持一个采集进程；网页会阻止重复启动。中断的换序采集应保留相同的 --peer-type 和 --sort-by 参数以续接断点。

中断后运行相同命令可按页面断点继续。加 --fresh 开始新一轮，已有资源按 ID 更新；--max-pages 3 用于小规模检查。

网页中的“暂停采集”会保存已完成页面，等待在途请求结束后导出文件。data/stop.request 是暂停标记；从页面重新启动时会自动清除，手动启动前可删除该标记。

本地服务默认每 6 小时更新一次，可在页面调整或关闭。只有服务和已登录的 Chrome 保持开启时，自动更新才会运行；不是 Windows 开机任务。

## 数据范围与完整性

采集包括遍历源站分页、按 ID 去重、失败重试，以及最后对近期更新的页面补采。源站总数会实时变化，且接口没有静态快照或稳定游标；完成分页不代表同一时刻的无遗漏快照。状态文件分别记录源站总数、本地去重数量、完成页面和错误。成员数、链接及简介为空时保留源站原值。

数据库保存原始响应字段，不采集群成员、聊天消息或个人联系方式。

数据流：Chrome 当前站点会话 → 资源分页 API → 重试与 ID 去重 → 本地 SQLite → 搜索网站及 CSV / JSON 文件。
