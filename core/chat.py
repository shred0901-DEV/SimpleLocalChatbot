"""Ollama chat data and bounded conversation construction; no Qt/GPU imports."""
from dataclasses import dataclass, field
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit
from core.chat_images import ChatImage, IMAGE_ONLY_PROMPT, image_list, message_cost


SYSTEM_MESSAGE = 'あなたは親切な会話アシスタントです。ユーザーの指定がない限り、自然な日本語で回答してください。'
MAX_TURNS = 100


def server_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')
            or parts.port == 0):
        raise ValueError('接続先は http://127.0.0.1:11434 のように、サーバーのURLを指定してください。')
    return urlunsplit((parts.scheme, parts.netloc, '', '', ''))


def local_model(name: str) -> bool:
    return bool(name.strip()) and not name.lower().endswith((':cloud', '-cloud'))


@dataclass
class ChatSettings:
    endpoint: str = 'http://127.0.0.1:11434'
    model: str = ''
    num_ctx: int = 4096
    web_search: bool = False

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError('チャット設定が不正です。')
        value = cls(**{key: raw[key] for key in ('endpoint', 'model', 'num_ctx', 'web_search') if key in raw})
        if (not isinstance(value.endpoint, str) or not isinstance(value.model, str)
                or type(value.num_ctx) is not int or not 2048 <= value.num_ctx <= 32768
                or type(value.web_search) is not bool):
            raise ValueError('チャット設定が不正です。')
        value.endpoint = server_url(value.endpoint)
        return value


@dataclass
class ChatTurn:
    user: str
    assistant: str = ''
    model: str = ''
    state: str = 'interrupted'
    error: str = ''
    sources: list[dict] = field(default_factory=list)
    images: list[ChatImage] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError('会話履歴が不正です。')
        value = cls(**raw)
        value.images = image_list(raw.get('images', []))
        if (any(not isinstance(getattr(value, key), str) for key in ('user', 'assistant', 'model', 'state', 'error'))
                or value.state not in ('complete', 'interrupted', 'error')):
            raise ValueError('会話履歴が不正です。')
        from core.chat_search import valid_source_url
        if (not isinstance(value.sources, list) or len(value.sources) > 3
                or any(not isinstance(item, dict) or set(item) != {'title', 'url', 'content'}
                       or any(not isinstance(text, str) for text in item.values())
                       or not valid_source_url(item['url']) for item in value.sources)):
            raise ValueError('Web検索の参照元が不正です。')
        return value


@dataclass
class ChatConversation:
    id: str = field(default_factory=lambda: uuid4().hex)
    turns: list[ChatTurn] = field(default_factory=list)
    draft: str = ''
    draft_images: list[ChatImage] = field(default_factory=list)

    @property
    def title(self):
        text = self.turns[0].user if self.turns else self.draft
        return ' '.join(text.split())[:60] or ('画像の会話' if self.draft_images else '新しい会話')

    @classmethod
    def from_dict(cls, raw):
        if (not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id']
                or not isinstance(raw.get('turns'), list) or len(raw['turns']) > MAX_TURNS
                or not isinstance(raw.get('draft', ''), str)):
            raise ValueError('会話一覧の保存形式が不正です。')
        return cls(raw['id'], [ChatTurn.from_dict(turn) for turn in raw['turns']], raw.get('draft', ''),
                   image_list(raw.get('draft_images', [])))


def conversation(turns, text, num_ctx, images=None):
    """Reserve output space and conservatively budget UTF-8 bytes as tokens.

    This is an estimate, not the image model's tokenizer or an exact token count.
    Never cut a message or include a failed/partial assistant reply.
    """
    messages = [dict(role='system', content=SYSTEM_MESSAGE)]
    current = dict(role='user', content=text or (IMAGE_ONLY_PROMPT if images else ''))
    if images:
        current['images'] = [image.id for image in images]
    budget = num_ctx - 1024

    remaining = budget - message_cost(messages[0]) - message_cost(current)
    if remaining < 0:
        raise ValueError('入力が長すぎるか、添付画像を収める余裕がありません。入力・画像を減らすか、会話の長さを増やしてください。')
    selected = []
    for turn in reversed(turns):
        if turn.state != 'complete':
            continue
        user = turn.user
        if turn.sources:
            from core.chat_search import SEARCH_INSTRUCTIONS, source_block
            user += '\n\n' + SEARCH_INSTRUCTIONS + source_block(turn.sources)
        pair = [dict(role='user', content=user), dict(role='assistant', content=turn.assistant)]
        if turn.images:
            pair[0]['images'] = [image.id for image in turn.images]
        size = sum(message_cost(message) for message in pair)
        if size > remaining:
            break
        remaining -= size
        selected[0:0] = pair
    return messages + selected + [current], sum(turn.state == 'complete' for turn in turns) - len(selected) // 2
