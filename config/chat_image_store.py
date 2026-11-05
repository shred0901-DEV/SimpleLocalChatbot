"""Content-addressed attachments beside chat.json; no original paths in history."""
import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import tempfile

from core.chat_images import MAX_IMAGE_BYTES, MAX_INPUT_BYTES, normalize_image


class ChatImageStore:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}\.(png|jpg)', identifier):
            raise ValueError('添付画像の識別情報が不正です。')
        path = self.root / identifier
        for item in (self.root, path):
            if item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('添付画像の保存先にリンクは使用できません。')
            if item.is_symlink():
                raise ValueError('添付画像の保存先にリンクは使用できません。')
        return path

    def add_file(self, filename):
        path = Path(filename)
        try:
            with path.open('rb') as stream:
                data = stream.read(MAX_INPUT_BYTES + 1)
        except OSError as exc:
            raise ValueError(f'画像を開けません: {path.name} ({exc.strerror})') from None
        return self.add_data(data, path.name)

    def add_data(self, data, name):
        image, normalized = normalize_image(data, name)
        target = self._path(image.id)
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(normalized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return image
        except OSError as exc:
            raise ValueError(f'添付画像を保存できません: {exc.strerror}') from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # A failed cleanup must not discard a successfully saved attachment.
                    pass

    def read(self, identifier):
        path = self._path(identifier)
        try:
            with path.open('rb') as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
            if (len(data) > MAX_IMAGE_BYTES
                    or hashlib.sha256(data).hexdigest() != identifier.split('.')[0]):
                raise ValueError('保存された添付画像が変更・破損しています。')
            return data
        except OSError:
            raise ValueError('保存された添付画像が見つかりません。新しい会話で画像を添付し直してください。') from None

    def prepare_messages(self, messages):
        prepared = deepcopy(messages)
        for message in prepared:
            if message.get('images'):
                message['images'] = [base64.b64encode(self.read(identifier)).decode('ascii')
                                     for identifier in message['images']]
        return prepared
