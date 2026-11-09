"""Telegram inbox listeners and durable direct-message / moderation execution."""
import asyncio
import time

import automation_store as auto
import business_store as biz
import workspace_store as ws
from library_store import now


class OperationsMixin:
    def attach_listeners(self, client, account_id):
        if not hasattr(client,'add_event_handler'):return
        from telethon import events

        async def incoming(event):
            try:
                if not event.is_private and not event.is_group:return
                sender=await event.get_sender()
                if getattr(sender,'bot',False) or getattr(sender,'is_self',False):return
                chat=await event.get_chat()
                title=getattr(chat,'title',None) or getattr(chat,'first_name',None) or str(event.chat_id)
                auto.ingest(account_id,event.chat_id,title,'private' if event.is_private else 'group',event.id,event.raw_text or '')
            except Exception:
                self.event_error(account_id)

        async def joined(event):
            try:
                if not (event.user_joined or event.user_added):return
                chat=await event.get_chat()
                users=await event.get_users()
                if not users or all(getattr(u,'is_self',False) or getattr(u,'bot',False) for u in users):return
                mid=getattr(getattr(event,'action_message',None),'id',None)
                if mid is None:return
                auto.ingest(account_id,event.chat_id,getattr(chat,'title','群组'),'group',mid,'新成员加入群组','welcome')
            except Exception:
                self.event_error(account_id)
        client.add_event_handler(incoming,events.NewMessage(incoming=True))
        client.add_event_handler(joined,events.ChatAction())

    def event_error(self,account_id):
        # No message text, credentials, or provider exception bodies in system logs.
        stamp=time.time()
        recent=getattr(self,'event_errors',{})
        if stamp-recent.get(account_id,0)<60:return
        recent[account_id]=stamp;self.event_errors=recent
        with ws.connection() as db:
            db.execute("INSERT INTO audit_events(kind,message,created_at) VALUES('listener',?,?)",('消息监听处理失败，请检查账号连接：'+account_id,now()))

    async def maintenance(self):
        restoring=None;last_restore=0
        while True:
            try:
                biz.heartbeat('running' if biz.dispatch_enabled() else 'paused')
                if time.time()-last_restore>=60 and (not restoring or restoring.done()):
                    restoring=asyncio.create_task(self.restore_listeners())
                    last_restore=time.time()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(10)

    async def sync_dialogs(self,user,payload):
        aid=payload.get('id')
        async with self.lock(aid):
            client,acc=await self.client(user,aid)
            if acc['status']!='connected' or not await client.is_user_authorized():
                raise ws.ApiError('请先连接 Telegram 账号',409)
            dialogs=[]
            async for dialog in client.iter_dialogs():
                entity=dialog.entity
                if getattr(entity,'bot',False) or getattr(entity,'is_self',False) or getattr(entity,'left',False):continue
                if not (getattr(dialog,'is_user',False) or dialog.is_group):continue
                dialogs.append((dialog.id,getattr(dialog,'name',None) or getattr(entity,'title',None) or str(dialog.id),'group' if dialog.is_group else 'private'))
                if len(dialogs)>=100:break
            with ws.connection() as db:
                for peer,title,kind in dialogs:auto.save_conversation(db,user['id'],aid,peer,title,kind)
                ws.add_activity(db,user['id'],'同步 Telegram 已有会话：'+str(len(dialogs)))
            auto.listening(user,{'id':aid,'enabled':True})
        return {'message':'已同步 %s 个已有会话并开启新消息监听；不回填历史消息'%len(dialogs)}

    async def restore_listeners(self):
        with ws.connection() as db:
            rows=db.execute("SELECT a.id,a.user_id FROM accounts a JOIN automation_prefs p ON p.account_id=a.id LEFT JOIN user_access x ON x.user_id=a.user_id WHERE a.status='connected' AND p.enabled=1 AND coalesce(x.enabled,1)=1").fetchall()
            disabled={r[0] for r in db.execute('SELECT a.id FROM accounts a JOIN user_access x ON x.user_id=a.user_id WHERE x.enabled=0')}
        for aid in disabled:
            client=self.clients.pop(aid,None)
            if client:await client.disconnect()
        for row in rows:
            try:
                async with self.lock(row['id']):
                    client,_=await asyncio.wait_for(self.client({'id':row['user_id']},row['id']),timeout=8)
                    if not await asyncio.wait_for(client.is_user_authorized(),timeout=8):await self.invalidate(row['id'])
            except Exception:self.event_error(row['id'])

    async def deliver_outbox(self,item):
        from telegram_runtime import telegram_error
        from telethon.errors import RPCError
        source='outbox:'+item['id'];sending=False;reserved=False
        async with self.lock(item['account_id']):
            try:
                if not auto.executable(item):
                    auto.result(item,'cancelled','账号、监听、规则或人工接管状态已改变');return
                client,_=await self.client({'id':item['user_id']},item['account_id'])
                if not await client.is_user_authorized():
                    await self.invalidate(item['account_id'])
                    auto.result(item,'failed','账号已失效，请重新连接');return
                entity=None
                async for dialog in client.iter_dialogs():
                    if str(dialog.id)==item['peer_id']:
                        if getattr(dialog.entity,'left',False) or getattr(dialog.entity,'bot',False):break
                        if not (dialog.is_group or getattr(dialog,'is_user',False)):break
                        entity=dialog.entity;break
                if entity is None:raise ws.ApiError('该会话已不在账号的已有会话中')
                if item['action']=='delete':
                    if item['chat_kind']!='group':raise ws.ApiError('关键词过滤仅适用于群组')
                    perms=await client.get_permissions(entity,'me')
                    rights=getattr(perms,'admin_rights',None)
                    if not (getattr(perms,'is_creator',False) or (getattr(perms,'is_admin',False) and (getattr(perms,'delete_messages',False) or getattr(rights,'delete_messages',False)))):
                        raise ws.ApiError('此账号没有删除群消息的管理员权限')
                if not auto.executable(item):auto.result(item,'cancelled','执行前状态已改变');return
                if item['action']=='send':
                    biz.reserve(item['user_id'],source);reserved=True
                sending=True
                if item['action']=='delete':
                    deleted=await client.delete_messages(entity,[int(item['target_message_id'])],revoke=True)
                    if not deleted:raise ws.ApiError('Telegram 未确认删除结果，请人工检查')
                    mid=None
                else:
                    message=await client.send_message(entity,item['content'],parse_mode=None,link_preview=False);mid=message.id
                auto.result(item,'sent',message_id=mid)
                if reserved:biz.usage_result(source,'sent')
            except asyncio.CancelledError:
                auto.result(item,'uncertain' if sending else 'failed','执行中断，结果待核对' if sending else '连接中断')
                if reserved:biz.usage_result(source,'uncertain' if sending else 'failed')
                raise
            except Exception as exc:
                name=type(exc).__name__
                if name in ('FloodWaitError','FloodPremiumWaitError','SlowModeWaitError'):
                    auto.result(item,'pending',telegram_error(exc),wait=max(10,int(getattr(exc,'seconds',60))))
                    if reserved:biz.usage_result(source,'failed')
                    return
                if name in ('AuthKeyUnregisteredError','SessionRevokedError','SessionExpiredError','AuthKeyDuplicatedError'):
                    await self.invalidate(item['account_id'])
                uncertain=sending and not isinstance(exc,RPCError)
                auto.result(item,'uncertain' if uncertain else 'failed','结果待核对，不会自动重发' if uncertain else str(exc) if isinstance(exc,ws.ApiError) else telegram_error(exc))
                if reserved:biz.usage_result(source,'uncertain' if uncertain else 'failed')
