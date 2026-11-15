"""Read and validate Telegram source messages; forwarding uses the original message."""
import re
from urllib.parse import urlsplit

import workspace_store as ws


def parse_message_link(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ws.ApiError('请输入有效的 Telegram 消息链接')
    try:
        url = urlsplit(value.strip())
        if url.scheme not in ('https', 'http') or url.netloc.lower() not in ('t.me', 'telegram.me') or url.fragment:
            raise ValueError()
        path = url.path.rstrip('/')
        match = re.fullmatch(r'/c/([1-9][0-9]{0,12})/(?:[1-9][0-9]*/)?([1-9][0-9]*)', path)
        if match:
            channel, mid = map(int, match.groups())
            peer = -(1000000000000 + channel)
            canonical = f'https://t.me/c/{channel}/{mid}'
        else:
            match = re.fullmatch(r'/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/(?:[1-9][0-9]*/)?([1-9][0-9]*)', path)
            if not match:
                raise ValueError()
            peer, raw_mid = match.groups()
            mid = int(raw_mid)
            canonical = f'https://t.me/{peer}/{mid}'
        if not 1 <= mid <= 2147483647:
            raise ValueError()
        return {'link': canonical, 'peer': peer, 'message_id': mid}
    except ValueError:
        raise ws.ApiError('请粘贴消息链接，例如 https://t.me/群用户名/123 或 https://t.me/c/群ID/123')


async def read_source(client, link, expected_peer=None):
    from telethon import types, utils

    ref = parse_message_link(link)
    if isinstance(ref['peer'], int):
        entity = None
        # StringSession does not persist the entity/access-hash cache. Resolve
        # private sources from actual membership instead of joining anything.
        async for dialog in client.iter_dialogs():
            if dialog.id == ref['peer']:
                entity = dialog.entity
                break
        if entity is None:
            raise ws.ApiError('此账号无法访问该私有消息，请确认已加入原群组或频道', 409)
    else:
        try:
            entity = await client.get_entity(ref['peer'])
        except ValueError:
            raise ws.ApiError('无法解析原消息的群组或频道，请检查链接', 409)
        except Exception as exc:
            if type(exc).__name__ in ('ChannelPrivateError', 'UsernameInvalidError', 'UsernameNotOccupiedError'):
                raise ws.ApiError('无法访问原消息来源，请检查链接及账号访问权限', 409)
            raise
    if not isinstance(entity, (types.Channel, types.Chat)):
        raise ws.ApiError('原消息必须来自群组或频道', 409)
    peer_id = str(utils.get_peer_id(entity))
    if expected_peer is not None and peer_id != str(expected_peer):
        raise ws.ApiError('原消息来源已变化，请重新检查消息链接', 409)
    if any(getattr(entity, key, False) for key in ('deactivated', 'kicked')):
        raise ws.ApiError('此账号已无法访问原群组或频道', 409)
    if getattr(entity, 'left', False) and not getattr(entity, 'username', None):
        raise ws.ApiError('此账号已退出私有原群组或频道，无法读取消息历史', 409)
    if getattr(entity, 'noforwards', False):
        raise ws.ApiError('原群组或频道开启了内容保护，禁止转发', 409)
    try:
        message = await client.get_messages(entity, ids=ref['message_id'])
    except Exception as exc:
        if type(exc).__name__ in ('ChannelPrivateError', 'MessageIdInvalidError', 'ChatAdminRequiredError', 'MsgIdInvalidError'):
            raise ws.ApiError('Telegram 未向此账号提供该消息：可能是消息 ID 错误、消息已删除，或账号没有查看该段历史的权限。请用“复制消息链接”重新复制，并确认关联账号可在 Telegram 客户端看到原消息。', 409)
        if type(exc).__name__ in ('RpcMcgetFailError', 'ServerError', 'TimedOutError'):
            raise ws.ApiError('Telegram 暂时无法读取原消息（' + type(exc).__name__ + '），稍后重试；这不表示消息已删除', 503)
        raise
    if not message or isinstance(message, (types.MessageEmpty, types.MessageService)) or getattr(message, 'action', None):
        raise ws.ApiError('Telegram 未返回此消息：请核对链接末尾的消息 ID，确认消息未删除，且关联账号有权查看该段历史。私有群组还需确认账号仍在群内。', 409)
    if getattr(message, 'noforwards', False):
        raise ws.ApiError('原消息禁止转发', 409)
    if not getattr(message, 'message', '') and not getattr(message, 'media', None):
        raise ws.ApiError('原消息没有可转发的内容', 409)
    rows = []
    markup = getattr(message, 'reply_markup', None)
    if isinstance(markup, types.ReplyInlineMarkup):
        rows = [[{'text': button.text, 'is_url': type(button).__name__ == 'KeyboardButtonUrl'
                  or type(getattr(button, 'type', None)).__name__ == 'InlineButtonTypeUrl'}
                 for button in row.buttons] for row in markup.rows]
    preview = {
        'source_link': ref['link'], 'peer_id': peer_id, 'message_id': ref['message_id'],
        'title': getattr(entity, 'title', '') or str(peer_id),
        'text': getattr(message, 'message', '') or '',
        'media': type(message.media).__name__ if getattr(message, 'media', None) else '',
        'buttons': rows, 'album': bool(getattr(message, 'grouped_id', None)),
    }
    return entity, message, preview
