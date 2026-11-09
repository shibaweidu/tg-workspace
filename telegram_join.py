"""Execute explicit group join requests using the user's authorized Telethon client."""
import asyncio
import time

from telethon.tl import functions, types
from telethon.errors import RPCError

import join_store as joins
import workspace_store as ws

JOIN_ERRORS={
    'InviteHashExpiredError':'邀请链接已过期或已撤销',
    'InviteHashInvalidError':'邀请链接无效',
    'InviteHashEmptyError':'邀请链接为空',
    'UsernameInvalidError':'群用户名无效',
    'UsernameNotOccupiedError':'群用户名不存在',
    'ChannelPrivateError':'此群不可访问或账号受到此群限制',
    'UserBannedInChannelError':'账号已被此群限制',
    'ChannelsTooMuchError':'已达到 Telegram 的加群数量限制，请人工处理后再继续',
    'UserChannelsTooMuchError':'账号已达到 Telegram 的加群数量限制',
    'UsersTooMuchError':'群组人数已达上限',
    'UserRestrictedError':'Telegram 已限制此账号加入群组，请先检查账号',
    'FrozenMethodInvalidError':'Telegram 已限制此账号操作，请先检查账号',
    'StarsPaymentRequiredError':'此邀请需要付费，当前不执行付费入群',
}
AUTH_ERRORS={'AuthKeyUnregisteredError','SessionRevokedError','SessionExpiredError','AuthKeyDuplicatedError','UserDeactivatedBanError'}
LIMIT_ERRORS={'ChannelsTooMuchError','UserChannelsTooMuchError','UserRestrictedError','FrozenMethodInvalidError','PeerFloodError'}


def group_entity(entity):
    return isinstance(entity,types.Chat) or (isinstance(entity,types.Channel) and not entity.broadcast and (entity.megagroup or entity.gigagroup))


async def public_membership(client,entity):
    try:
        result=await client(functions.channels.GetParticipantRequest(entity,'me'))
    except RPCError as exc:
        if type(exc).__name__=='UserNotParticipantError':return False
        raise
    if isinstance(result.participant,types.ChannelParticipantBanned):raise ws.ApiError('账号已被此群限制，无法自动加入')
    return not isinstance(result.participant,types.ChannelParticipantLeft)


async def inspect_target(client,target):
    """Read membership/invite metadata without issuing join or import requests."""
    if target['kind']=='invite':
        result=await client(functions.messages.CheckChatInviteRequest(target['value']))
        if isinstance(result,types.ChatInviteAlready):
            if not group_entity(result.chat):raise ws.ApiError('邀请指向频道，此功能仅加入群组')
            return result.chat,True
        if getattr(result,'subscription_pricing',None) is not None or getattr(result,'subscription_form_id',None) is not None:
            raise ws.ApiError('此邀请需要付费，当前不执行付费入群')
        if isinstance(result,types.ChatInvitePeek):
            if not group_entity(result.chat):raise ws.ApiError('邀请指向频道，此功能仅加入群组')
            return result.chat,False
        if not isinstance(result,types.ChatInvite) or result.broadcast:raise ws.ApiError('邀请未指向可加入的群组')
        return result,False
    entity=await client.get_entity(target['value'])
    if not isinstance(entity,types.Channel) or not group_entity(entity):raise ws.ApiError('此用户名指向用户、Bot 或频道，并非群组')
    if getattr(entity,'deactivated',False):raise ws.ApiError('此群已停用')
    return entity,await public_membership(client,entity)


class JoinMixin:
    async def join_group(self,job,target):
        from telegram_runtime import telegram_error
        attempted=False;response_received=False;entity=None
        async with self.lock(job['account_id']):
            try:
                if not joins.executable(job):joins.finish(job,target,'pending');return
                client,_=await self.client({'id':job['user_id']},job['account_id'])
                if not await client.is_user_authorized():
                    await self.invalidate(job['account_id']);joins.finish(job,target,'pending','账号登录已失效，请重新连接',pause=True);return
                entity,member=await inspect_target(client,target)
                if member:
                    joins.finish(job,target,'already_joined',peer_id=entity.id,title=entity.title);return
                if not joins.executable(job):joins.finish(job,target,'pending');return
                request=functions.messages.ImportChatInviteRequest(target['value']) if target['kind']=='invite' else functions.channels.JoinChannelRequest(entity)
                attempted=True
                await client(request)
                response_received=True
                confirmed,member=await inspect_target(client,target)
                if not member:
                    joins.finish(job,target,'uncertain','Telegram 尚未确认成员身份，请核对结果；不会自动重复申请');return
                joins.finish(job,target,'joined',peer_id=confirmed.id,title=confirmed.title)
            except asyncio.CancelledError:
                joins.finish(job,target,'uncertain' if attempted else 'pending','执行中断，入群结果待核对' if attempted else '连接检查中断，任务已暂停',pause=True)
                raise
            except Exception as exc:
                name=type(exc).__name__
                # Once Telegram accepted the mutation, a failed follow-up read must
                # never turn the target into something eligible for resubmission.
                if response_received:
                    if name in AUTH_ERRORS:await self.invalidate(job['account_id'])
                    wait=max(1,int(getattr(exc,'seconds',60))) if name in ('FloodWaitError','FloodPremiumWaitError','SlowModeWaitError') else None
                    joins.finish(job,target,'uncertain','加入请求已返回，但成员身份尚未确认；请核对结果，不会自动重发',wait=wait,pause=True)
                    return
                if name=='InviteRequestSentError':
                    joins.finish(job,target,'requested','已提交入群申请，等待群管理员审批，不会自动重复申请',peer_id=getattr(entity,'id',None),title=getattr(entity,'title',None));return
                if name=='UserAlreadyParticipantError':
                    joins.finish(job,target,'already_joined',peer_id=getattr(entity,'id',None),title=getattr(entity,'title',None));return
                if name in ('FloodWaitError','FloodPremiumWaitError','SlowModeWaitError'):
                    joins.finish(job,target,'pending',telegram_error(exc),wait=max(1,int(getattr(exc,'seconds',60))));return
                if name in AUTH_ERRORS:
                    await self.invalidate(job['account_id']);joins.finish(job,target,'pending',telegram_error(exc),pause=True);return
                uncertain=attempted and (not isinstance(exc,RPCError) or getattr(exc,'code',500)>=500)
                error='入群结果待核对，不会自动重试' if uncertain else str(exc) if isinstance(exc,ws.ApiError) else JOIN_ERRORS.get(name,telegram_error(exc))
                joins.finish(job,target,'uncertain' if uncertain else 'failed',error,pause=name in LIMIT_ERRORS)
                if name in LIMIT_ERRORS:
                    with ws.connection() as db:
                        db.execute("UPDATE join_jobs SET status='paused',last_error=? WHERE account_id=? AND status IN ('queued','running','waiting')",(error,job['account_id']))

    async def check_join(self,user,payload):
        from delivery_store import account
        from telegram_runtime import telegram_error
        item=joins.reconcile_target(user,payload.get('target_id'))
        acc=account(user,item['account_id'])
        if acc['status']!='connected' or not acc.get('secret'):raise ws.ApiError('请先连接此任务账号',409)
        async with self.lock(item['account_id']):
            item=joins.reconcile_target(user,payload.get('target_id'))
            with ws.connection() as db:
                window=db.execute('SELECT next_at FROM telegram_windows WHERE account_id=?',(item['account_id'],)).fetchone()
            if window and window[0]>time.time():raise ws.ApiError('此账号正在等待，请约 %s 秒后再核对'%max(1,int(window[0]-time.time())+1),429)
            try:
                client,_=await self.client(user,item['account_id'])
                if not await client.is_user_authorized():
                    await self.invalidate(item['account_id']);raise ws.ApiError('账号登录已失效，请重新连接',409)
                entity,member=await inspect_target(client,item)
            except RPCError as exc:
                name=type(exc).__name__
                if name in AUTH_ERRORS:await self.invalidate(item['account_id'])
                if name in ('FloodWaitError','FloodPremiumWaitError'):
                    with ws.connection() as db:
                        db.execute('INSERT INTO telegram_windows VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)',(item['account_id'],time.time()+max(1,int(getattr(exc,'seconds',60)))))
                raise ws.ApiError(JOIN_ERRORS.get(name,telegram_error(exc)),409)
            if member:
                joins.reconciled(item,entity.id,entity.title)
                return {'message':'已确认加入此群','joined':True}
            return {'message':'尚未确认已加入，保留当前状态；本次核对没有发出入群申请','joined':False}
