"""Read-only Ollama account balance/usage, independent of local model inference."""
import json
import math
import os
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


def money(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('金額の形式が不正です。')
    return f'${value:,.6f}'


def balance_text(data):
    if not isinstance(data, dict):
        raise ValueError('残高の形式が不正です。')
    purchased, included = data.get('purchased'), data.get('included')
    lines = []
    if isinstance(purchased, dict) and 'balance_usd' in purchased:
        lines.append('購入分の残量: ' + money(purchased['balance_usd']))
    if isinstance(included, dict):
        if 'balance_usd' in included:
            lines.append('プラン付属分の残量: ' + money(included['balance_usd']))
            if 'allowance_usd' in included:
                allowance, remaining = included['allowance_usd'], included['balance_usd']
                money(allowance)
                lines.append('付属枠の当期消費量: ' + money(max(0, allowance - remaining)))
        else:
            for key, title in (('session', 'セッション'), ('weekly', '週間')):
                limit = included.get(key)
                if isinstance(limit, dict) and 'remaining_percent' in limit:
                    percent = limit['remaining_percent']
                    if type(percent) not in (int, float) or not 0 <= percent <= 100:
                        raise ValueError('利用枠の形式が不正です。')
                    lines.append(f'{title}枠の残量: {percent:g}%')
    if not lines:
        raise ValueError('残高情報が含まれていません。')
    return '\n'.join(lines)


def usage_text(data):
    if not isinstance(data, dict) or not isinstance(data.get('totals'), dict):
        raise ValueError('利用量の形式が不正です。')
    total = data['totals']
    if 'usage_usd' not in total:
        return '消費量（過去30日・本人）: 金額はAPI未提供'
    return '消費量（過去30日・本人）: ' + money(total['usage_usd'])


class OllamaAccountService(QObject):
    updated = Signal(str, str)
    failed = Signal(str, str)
    busyChanged = Signal(bool)

    def __init__(self, parent=None, *, endpoint='https://ollama.com', timeout_ms=15000,
                 key_provider=None):
        super().__init__(parent)
        self._endpoint = endpoint
        self._timeout_ms = timeout_ms
        self._key_provider = key_provider or (lambda: os.environ.get('OLLAMA_API_KEY', ''))
        self._manager = QNetworkAccessManager(self)
        self._requests = {}
        self.next_refresh = 0.0
        self._closed = False

    @property
    def is_running(self):
        return bool(self._requests)

    def refresh(self):
        if self._closed or self.is_running or time.monotonic() < self.next_refresh:
            return False
        key = self._key_provider().strip()
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            for kind in ('balance', 'usage'):
                self.failed.emit(kind, 'OLLAMA_API_KEY を設定してアプリを再起動してください。')
            return False
        # Two separate endpoints, each queried at most once a minute. No retries.
        self.next_refresh = time.monotonic() + 60
        self.busyChanged.emit(True)
        for kind, path in (('balance', '/api/balance'), ('usage', '/api/usage?range=30d')):
            request = QNetworkRequest(QUrl(self._endpoint.rstrip('/') + path))
            request.setRawHeader(b'Authorization', ('Bearer ' + key).encode('ascii'))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
            reply = self._manager.get(request)
            self._requests[reply] = dict(kind=kind, buffer=b'', timed_out=False, too_large=False)
            timer = QTimer(reply)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda reply=reply: self._timeout(reply))
            reply.finished.connect(timer.stop)
            reply.readyRead.connect(lambda reply=reply: self._read(reply))
            reply.finished.connect(lambda reply=reply: self._finished(reply))
            timer.start(self._timeout_ms)
        return True

    def _timeout(self, reply):
        if reply in self._requests:
            self._requests[reply]['timed_out'] = True
            reply.abort()

    def _read(self, reply):
        state = self._requests.get(reply)
        if state is None:
            return
        state['buffer'] += bytes(reply.readAll())
        if len(state['buffer']) > 1024 * 1024:
            state['too_large'] = True
            reply.abort()

    def _finished(self, reply):
        self._read(reply)
        state = self._requests.pop(reply, None)
        if state is None:
            reply.deleteLater()
            return
        try:
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if state['timed_out']:
                raise ValueError('取得がタイムアウトしました。')
            if state['too_large']:
                raise ValueError('応答が大きすぎます。')
            if code is not None and not 200 <= int(code) < 300:
                if int(code) == 429:
                    try:
                        header = next((bytes(value) for name, value in reply.rawHeaderPairs()
                                       if bytes(name).lower() == b'retry-after'), b'60')
                        delay = int(header.decode('ascii'))
                        self.next_refresh = max(self.next_refresh, time.monotonic() + max(60, delay))
                    except (ValueError, UnicodeError):
                        pass
                # Don't display remote bodies, which could contain credentials.
                raise ValueError(f'HTTP {code}: Ollamaの認証・利用条件を確認してください。')
            if reply.error() != QNetworkReply.NetworkError.NoError:
                raise ValueError('OllamaのアカウントAPIに接続できません。')
            data = json.loads(state['buffer'])
            text = balance_text(data) if state['kind'] == 'balance' else usage_text(data)
            self.updated.emit(state['kind'], text)
        except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
            self.failed.emit(state['kind'], str(exc))
        finally:
            reply.deleteLater()
            if not self._requests:
                self.busyChanged.emit(False)

    def shutdown(self):
        self._closed = True
        replies = list(self._requests)
        self._requests.clear()
        for reply in replies:
            reply.abort()
