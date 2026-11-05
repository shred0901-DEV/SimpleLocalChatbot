"""Bounded image attachments and normalization without Qt/GPU dependencies."""
from dataclasses import dataclass
import hashlib
from io import BytesIO
import re
import warnings


MAX_IMAGES = 3
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_SIDE = 1600
# Vision token counts depend on the model. Reserve space separately from text;
# never treat the much larger base64 representation as text tokens.
IMAGE_TOKEN_RESERVE = 2048
IMAGE_ONLY_PROMPT = 'この画像について日本語で説明してください。'


@dataclass
class ChatImage:
    id: str
    name: str
    width: int
    height: int

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError('添付画像の情報が不正です。')
        value = cls(**raw)
        if (not isinstance(value.id, str) or not re.fullmatch(r'[a-f0-9]{64}\.(png|jpg)', value.id)
                or not isinstance(value.name, str) or not 1 <= len(value.name) <= 200
                or any(type(size) is not int or not 1 <= size <= MAX_SIDE
                       for size in (value.width, value.height))):
            raise ValueError('添付画像の情報が不正です。')
        return value


def image_list(raw):
    if not isinstance(raw, list) or len(raw) > MAX_IMAGES:
        raise ValueError('添付画像は1メッセージにつき3枚までです。')
    return [ChatImage.from_dict(item) for item in raw]


def normalize_image(data, name):
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError('画像ファイルは1枚20MiB以下にしてください。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as opened:
                if opened.format not in ('PNG', 'JPEG', 'WEBP', 'BMP', 'GIF'):
                    raise ValueError('PNG・JPEG・WebP・BMP・GIFの画像を指定してください。')
                if opened.width * opened.height > MAX_PIXELS:
                    raise ValueError('画像は2400万画素以下にしてください。')
                opened.seek(0)
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                mode = 'RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB'
                # Re-create pixels to exclude EXIF/GPS/other original metadata.
                clean = Image.new(mode, image.size)
                clean.paste(image.convert(mode))
                output = BytesIO()
                clean.save(output, format='PNG')
                suffix = 'png'
                if output.tell() > MAX_IMAGE_BYTES:
                    background = Image.new('RGB', clean.size, 'white')
                    background.paste(clean, mask=clean.getchannel('A') if mode == 'RGBA' else None)
                    output = BytesIO()
                    background.save(output, format='JPEG', quality=88)
                    suffix = 'jpg'
                normalized = output.getvalue()
                if len(normalized) > MAX_IMAGE_BYTES:
                    raise ValueError('画像を小さくしてから添付してください。')
                identifier = hashlib.sha256(normalized).hexdigest() + '.' + suffix
                return ChatImage(identifier, str(name)[:200] or '画像', clean.width, clean.height), normalized
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError('画像の画素数が大きすぎます。') from None
    except (OSError, UnidentifiedImageError, SyntaxError):
        raise ValueError('画像を読み込めません。破損や対応形式を確認してください。') from None


def message_cost(message):
    return (len(message['content'].encode('utf-8')) + 64
            + IMAGE_TOKEN_RESERVE * len(message.get('images', [])))
