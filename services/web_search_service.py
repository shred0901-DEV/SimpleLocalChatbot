"""One explicit, cancellable Ollama search request. No automatic retries or web fetch."""
import json
import os

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


class WebSearchService(QObject):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None, *, endpoint='https://ollama.com', timeout_ms=30000, key_provider=None):
        super().__init__(parent)
        self._endpoint = endpoint
        self._timeout_ms = timeout_ms
        self._key_provider = key_provider or (lambda: os.environ.get('OLLAMA_API_KEY', ''))
        self._manager = QNetworkAccessManager(self)
        self._reply = None
        self._buffer = b''
        self._key = ''
        self._cancelled = False
        self._timed_out = False
        self._too_large = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timeout)

    @property
    def is_running(self):
        return self._reply is not None

    def start(self, query):
        if self.is_running:
            return False
        key = self._key_provider().strip()
        if not key:
            raise ValueError('ユーザー環境変数 OLLAMA_API_KEY を設定し、アプリを起動し直してください。')
        if any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError('OLLAMA_API_KEY の形式を確認してください。空白・改行を含めないでください。')
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError('Web検索の質問は1〜4000文字で入力してください。')
        self._buffer, self._key = b'', key
        self._cancelled = self._timed_out = self._too_large = False
        request = QNetworkRequest(QUrl(self._endpoint.rstrip('/') + '/api/web_search'))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/json')
        request.setRawHeader(b'Authorization', ('Bearer ' + key).encode('ascii'))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        payload = json.dumps(dict(query=query, max_results=3), ensure_ascii=False).encode('utf-8')
        reply = self._manager.post(request, payload)
        self._reply = reply
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply))
        self._timer.start(self._timeout_ms)
        return True

    def _read(self, reply):
        if reply is not self._reply:
            return
        self._buffer += bytes(reply.readAll())
        if len(self._buffer) > 1024 * 1024:
            self._too_large = True
            reply.abort()

    def _finished(self, reply):
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._timer.stop()
        self._read(reply)
        self._reply = None
        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        network_error = reply.error() != QNetworkReply.NetworkError.NoError
        reply.deleteLater()
        try:
            if self._cancelled:
                self.cancelled.emit()
                return
            if self._timed_out:
                raise ValueError('Web検索がタイムアウトしました。自動再試行は行いません。')
            if self._too_large:
                raise ValueError('Web検索の応答が大きすぎます。')
            try:
                data = json.loads(self._buffer)
            except (ValueError, UnicodeError):
                data = None
            detail = str(data.get('error', ''))[:600] if isinstance(data, dict) else ''
            if code is not None and not 200 <= int(code) < 300:
                if int(code) == 401:
                    message = 'APIキーの認証に失敗しました。'
                elif int(code) == 429:
                    message = '利用上限に達しました。時間をおいて再送信してください。'
                else:
                    message = 'Ollamaの利用条件・アカウント設定を確認してください。'
                raise ValueError(f'Web検索 HTTP {code}: {message} {detail}')
            if network_error:
                raise ValueError('Web検索サービスに接続できません。インターネット接続を確認してください。')
            if detail:
                raise ValueError('Web検索に失敗しました: ' + detail)
            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise ValueError('Web検索の応答形式が不正、または空です。')
            self.completed.emit(data['results'])
        except (ValueError, TypeError) as exc:
            # Never persist an API key even if a remote error body echoes it.
            self.failed.emit(str(exc).replace(self._key, '[API key]') if self._key else str(exc))
        finally:
            self._key = ''
            self._buffer = b''

    def _timeout(self):
        if self._reply is not None:
            self._timed_out = True
            self._reply.abort()

    def cancel(self):
        if self._reply is not None:
            self._cancelled = True
            self._reply.abort()

    def shutdown(self):
        self._timer.stop()
        reply, self._reply = self._reply, None
        self._key, self._buffer = '', b''
        if reply is not None:
            reply.abort()
