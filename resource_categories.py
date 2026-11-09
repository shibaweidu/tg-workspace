"""Local, explainable topic grouping without altering source resource records."""
import re
import unicodedata

RULE_VERSION='ai-1'
GROUP_TYPES=('group','megagroup','supergroup')
CATEGORIES={'ai':{'id':'ai','name':'AI 类群组','description':'人工智能、大模型、AI 工具、绘画与开发交流群'}}
BATCH_SIZE=500
TOPICS=re.compile(
    r'人工智能|人工智慧|机器学习|機器學習|深度学习|深度學習|大语言模型|大語言模型|大模型|'
    r'智能体|智能體|神经网络|神經網絡|自然语言处理|自然語言處理|计算机视觉|電腦視覺|'
    r'通义千问|通義千問|文心一言|智谱清言|智譜清言|豆包|'
    r'(?<![a-z0-9])(?:ai|aigc|llms?|gpt(?:[ -]?[2-9](?:\.\d+)?)?|chat[ -]?gpt(?:[ -]?[2-9](?:\.\d+)?)?|open[ -]?ai|'
    r'deep[ -]?seek|claude|anthropic|gemini|grok|qwen|llama|ollama|mistral|'
    r'mid[ -]?journey|stable[ -]?diffusion|comfy[ -]?ui|dall[ -]?e|'
    r'lang[ -]?chain|llama[ -]?index|dify|coze|hugging[ -]?face)(?![a-z0-9])',re.I)
AI_CONTEXT=re.compile(r'人工智能|人工智慧|机器学习|機器學習|深度学习|深度學習|大语言模型|大語言模型|大模型|(?<![a-z0-9])(?:ai|chat[ -]?gpt|deepseek)\s*(?:技术|技術|工具|绘画|繪畫|开发|開發|学习|學習|交流|爱好|愛好|模型|创作|創作|编程|編程)|artificial intelligence|machine learning|deep learning',re.I)
LINKS=re.compile(r'https?://\S+|t\.me/\S+|@[a-z0-9_]+',re.I)


def normalize(value):return unicodedata.normalize('NFKC',str(value or ''))


def ai_reason(item):
    if item.get('peer_type') not in GROUP_TYPES:return None
    for key,label in (('title','群名'),('username','用户名')):
        match=TOPICS.search(normalize(item.get(key)))
        if match:return label+'包含「'+match.group(0)+'」'
    # A lone mention in an ad, link or a general-purpose description is weak
    # evidence; require an explicit subject phrase or two distinct topic terms.
    description=LINKS.sub(' ',normalize(item.get('description')))
    explicit=AI_CONTEXT.search(description)
    if explicit:return '简介包含「'+explicit.group(0)+'」'
    matches=list(dict.fromkeys(m.group(0).casefold() for m in TOPICS.finditer(description)))
    if len(matches)>=2:return '简介包含多个 AI 主题词：'+ '、'.join(matches[:3])
    return None


def initialize(db,stamp):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS resource_categories(
        resource_id TEXT PRIMARY KEY,category TEXT NOT NULL,
        source TEXT NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS resource_categories_category ON resource_categories(category,resource_id);
    ''')
    version=db.execute("SELECT value FROM settings WHERE key='resource_category_rules'").fetchone()
    if version and version[0]==RULE_VERSION:return
    try:
        db.execute('BEGIN IMMEDIATE')
        # Another server/collector may have completed the migration meanwhile.
        version=db.execute("SELECT value FROM settings WHERE key='resource_category_rules'").fetchone()
        if not version or version[0]!=RULE_VERSION:
            cursor=db.execute('SELECT id,title,username,description,peer_type FROM resources')
            while rows:=cursor.fetchmany(1000):
                update(db,[dict(row) for row in rows],stamp)
            db.execute("INSERT OR REPLACE INTO settings VALUES('resource_category_rules',?)",(RULE_VERSION,))
        db.commit()
    except Exception:
        db.rollback();raise


def update(db,items,stamp):
    for item in items:
        rid=str(item['id'])
        if item.get('peer_type') not in GROUP_TYPES:
            db.execute('DELETE FROM resource_categories WHERE resource_id=?',(rid,));continue
        row=db.execute('SELECT source FROM resource_categories WHERE resource_id=?',(rid,)).fetchone()
        if row and row['source']=='manual':continue
        reason=ai_reason(item)
        if reason:
            db.execute("INSERT INTO resource_categories VALUES(?,?,'rule',?,?) ON CONFLICT(resource_id) DO UPDATE SET category=excluded.category,source=excluded.source,reason=excluded.reason,updated_at=excluded.updated_at",(rid,'ai',reason,stamp))
        elif row:db.execute("DELETE FROM resource_categories WHERE resource_id=? AND source='rule'",(rid,))


def validate(category):
    if category not in ('all',*CATEGORIES):raise ValueError('分组不存在')
    return category


def validate_batch(category,batch='all'):
    validate(category)
    if batch=='all':return 'all'
    if category!='ai' or not re.fullmatch(r'[1-9][0-9]{0,5}',str(batch)):
        raise ValueError('二级分组不正确，请选择 AI 分类下的有效分组')
    return str(batch)


def where(category,batch='all'):
    validate(category)
    batch=validate_batch(category,batch)
    if category=='all':return ''
    # Select a group's members before applying search, display sorting or page
    # limits. This keeps the same resource from appearing in multiple groups.
    if batch!='all':
        offset=(int(batch)-1)*BATCH_SIZE
        return ("id IN (SELECT r.id FROM resources r JOIN resource_categories c ON c.resource_id=r.id "
                "WHERE c.category='ai' AND r.peer_type IN ('group','megagroup','supergroup') "
                "ORDER BY r.id LIMIT %d OFFSET %d)"%(BATCH_SIZE,offset))
    return "id IN (SELECT resource_id FROM resource_categories WHERE category='ai') AND peer_type IN ('group','megagroup','supergroup')"


def catalog(db):
    result=[]
    for c in CATEGORIES.values():
        count=db.execute('SELECT count(*) FROM resources WHERE '+where(c['id'])).fetchone()[0]
        batches=[{'id':str(i+1),'name':'第 %s 组'%(i+1),'count':min(BATCH_SIZE,count-i*BATCH_SIZE),
                  'start':i*BATCH_SIZE+1,'end':min((i+1)*BATCH_SIZE,count)} for i in range((count+BATCH_SIZE-1)//BATCH_SIZE)]
        result.append(dict(c,count=count,batch_size=BATCH_SIZE,batches=batches))
    return result


def membership(db,rid):
    row=db.execute('SELECT category,source,reason FROM resource_categories WHERE resource_id=?',(str(rid),)).fetchone()
    return dict(row) if row else None


def set_membership(db,rid,action,stamp):
    if not isinstance(rid,str) or action not in ('include','exclude','auto'):raise ValueError('分组操作不正确')
    row=db.execute('SELECT id,title,username,description,peer_type FROM resources WHERE id=?',(rid,)).fetchone()
    if not row:raise ValueError('资源不存在')
    if row['peer_type'] not in GROUP_TYPES:raise ValueError('AI 分组仅支持群组资源')
    if action=='auto':
        db.execute('DELETE FROM resource_categories WHERE resource_id=?',(rid,));update(db,[dict(row)],stamp)
    else:
        db.execute('INSERT INTO resource_categories VALUES(?,?,?,?,?) ON CONFLICT(resource_id) DO UPDATE SET category=excluded.category,source=excluded.source,reason=excluded.reason,updated_at=excluded.updated_at',
            (rid,'ai' if action=='include' else 'none','manual','手动加入 AI 分组' if action=='include' else '手动移出 AI 分组',stamp))
    return {'message':'分组已更新','classification':membership(db,rid)}
