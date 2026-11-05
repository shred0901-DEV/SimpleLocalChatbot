"""Atomically save chat settings/history in the user's configuration directory."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from core.chat import ChatSettings, ChatConversation
from config.chat_image_store import ChatImageStore


class ChatStore:
    def __init__(self, path):
        self.path = Path(path)
        self.images = ChatImageStore(self.path.with_name(self.path.stem + '_images'))
        self.settings = ChatSettings()
        self.conversations = [ChatConversation()]
        self.active_id = self.conversations[0].id
        self.error = ''
        self.read_failed = False
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or raw.get('version') not in (1, 2, 3):
                raise ValueError('保存形式が不正です。')
            settings = ChatSettings.from_dict(raw['settings'])
            if raw['version'] == 1:
                # Keep the original file untouched until the next successful atomic save.
                conversations = [ChatConversation.from_dict(dict(
                    id=self.active_id, turns=raw.get('turns'), draft=raw.get('draft', '')))]
                active_id = conversations[0].id
            else:
                if not isinstance(raw.get('conversations'), list) or not raw['conversations']:
                    raise ValueError('会話一覧が不正です。')
                conversations = [ChatConversation.from_dict(item) for item in raw['conversations']]
                ids = [item.id for item in conversations]
                active_id = raw.get('active_id')
                if len(set(ids)) != len(ids) or active_id not in ids:
                    raise ValueError('会話の識別情報が不正です。')
            self.settings, self.conversations, self.active_id = settings, conversations, active_id
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            self.read_failed = True
            self.error = f'チャットの保存データを読み込めません。元ファイルは保持しています: {exc}'

    @property
    def active(self):
        return next(item for item in self.conversations if item.id == self.active_id)

    @property
    def turns(self):
        return self.active.turns

    @turns.setter
    def turns(self, value):
        self.active.turns = value

    @property
    def draft(self):
        return self.active.draft

    @draft.setter
    def draft(self, value):
        self.active.draft = value

    def save(self):
        if self.read_failed:
            return False
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(version=3, settings=asdict(self.settings), active_id=self.active_id,
                           conversations=[asdict(item) for item in self.conversations])
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.error = ''
            return True
        except (OSError, UnicodeError) as exc:
            self.error = f'チャットを保存できません: {exc}'
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
