"""Nonblocking Ollama HTTP client. Owns no subprocess, model files, or CUDA state."""
from copy import deepcopy
import json
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from core.chat import local_model, server_url
from core.chat_search import search_conversation
from services.web_search_service import WebSearchService


class ChatService(QObject):
    modelsReady = Signal(object)
    connectionFailed = Signal(str)
    connectingChanged = Signal(bool)
    textReceived = Signal(str)
    sourcesReady = Signal(object)
    result = Signal(str, str, str)  # state, assistant text, error
    status = Signal(str)
    busyChanged = Signal(bool)  # remains True if model release needs retry

    def __init__(self, parent=None, timeout_ms=120000, release_timeout_ms=30000, search_service=None):
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)
        self.timeout_ms = timeout_ms
        self.release_timeout_ms = release_timeout_ms
        self._probe = None
        self._reply = None
        self._phase = 'idle'
        self._release_target = None
        self._buffer = b''
        self._text = ''
        self._received_bytes = 0
        self._done = False
        self._stream_error = ''
        self._outcome = None
        self._closed = False
        self._poll = QTimer(self)
        self._poll.setSingleShot(True)
        self._poll.timeout.connect(self._check_released)
        self._search = search_service or WebSearchService(self)
        self._search.completed.connect(self._search_ready)
        self._search.failed.connect(self._search_failed)
        self._search.cancelled.connect(self._search_cancelled)

    @property
    def is_running(self):
        return self._phase != 'idle'

    @property
    def is_active(self):
        return self._phase not in ('idle', 'release_failed')

    @property
    def release_failed(self):
        return self._phase == 'release_failed'

    @property
    def can_cancel(self):
        return self._phase in ('search', 'show', 'chat')

    def _network_request(self, endpoint, path, payload=None, timeout_ms=None):
        request = QNetworkRequest(QUrl(server_url(endpoint) + path))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/json')
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        if payload is None:
            reply = self._manager.get(request)
        else:
            reply = self._manager.post(request, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        timer = QTimer(reply)
        timer.setSingleShot(True)

        def timeout():
            reply.setProperty('timed_out', True)
            reply.abort()

        timer.timeout.connect(timeout)
        reply.finished.connect(timer.stop)
        timer.start(timeout_ms or self.timeout_ms)
        return reply

    @staticmethod
    def _error(reply, body):
        if reply.property('timed_out'):
            return 'Ollamaの応答がタイムアウトしました。'
        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if code is not None and not 200 <= int(code) < 300:
            try:
                detail = json.loads(body).get('error', '')
            except (ValueError, AttributeError):
                detail = ''
            return f'Ollama HTTP {code}: {str(detail)[:500] or "リクエストに失敗しました。"}'
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return 'Ollamaに接続できません。起動状態と接続先を確認してください。' + reply.errorString()
        return ''

    def refresh_models(self, endpoint):
        if self._closed or self.is_running:
            return False
        endpoint = server_url(endpoint)
        if self._probe is not None:
            previous, self._probe = self._probe, None
            previous.abort()
        reply = self._network_request(endpoint, '/api/tags', timeout_ms=5000)
        self._probe = reply
        self.connectingChanged.emit(True)
        reply.finished.connect(lambda: self._models_finished(reply))
        return True

    def _models_finished(self, reply):
        if reply is not self._probe:
            reply.deleteLater()
            return
        self._probe = None
        body = bytes(reply.readAll())
        error = self._error(reply, body)
        reply.deleteLater()
        self.connectingChanged.emit(False)
        try:
            if error:
                raise ValueError(error)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError('モデル一覧が大きすぎます。')
            raw = json.loads(body)
            if not isinstance(raw, dict) or not isinstance(raw.get('models'), list):
                raise ValueError('Ollamaのモデル一覧の形式が不正です。')
            names = []
            for model in raw['models']:
                if not isinstance(model, dict) or not isinstance(model.get('name'), str):
                    continue
                if local_model(model['name']) and not model.get('remote_host'):
                    names.append(model['name'])
            self.modelsReady.emit(sorted(set(names)))
        except (ValueError, TypeError) as exc:
            self.connectionFailed.emit(str(exc))

    def start(self, settings, messages):
        if self._closed or self.is_running:
            return False
        if not local_model(settings.model):
            raise ValueError('ダウンロード済みのローカルモデルを選択してください。')
        self._settings = deepcopy(settings)
        self._settings.endpoint = server_url(settings.endpoint)
        self._messages = deepcopy(messages)
        self._model = settings.model
        self._text, self._outcome = '', None
        self._phase = 'show'
        self.busyChanged.emit(True)
        self._check_model()
        return True

    def _check_model(self):
        self.status.emit('モデルを確認しています…')
        self._request('show', '/api/show', dict(model=self._model))

    def _start_search(self):
        self._phase = 'search'
        self.status.emit('Web検索中…（検索は1回、最大3件）')
        try:
            query = self._messages[-1]['content'] if self._messages else ''
            self._search.start(query)
        except (ValueError, TypeError, KeyError) as exc:
            self._search_failed(str(exc))

    def _start_chat(self):
        data = self._model_info
        payload = dict(model=self._model, messages=self._messages, stream=True, keep_alive=0,
                       options=dict(num_ctx=self._settings.num_ctx, num_predict=1024))
        thinking = data.get('thinking', {})
        if isinstance(thinking, dict) and False in thinking.get('values', []):
            payload['think'] = False
        elif 'thinking' in data.get('capabilities', []) and self._model.startswith(('qwen3:', 'qwen3.5:')) and not thinking:
            payload['think'] = False
        self._release_target = (self._settings.endpoint, self._model)
        self.status.emit('応答を生成しています…（初回はモデルの読み込みに時間がかかります）')
        self._request('chat', '/api/chat', payload)

    def _search_ready(self, results):
        if self._closed or self._phase != 'search':
            return
        try:
            self._messages, sources = search_conversation(self._messages, results, self._settings.num_ctx)
        except (ValueError, TypeError) as exc:
            self._search_failed(str(exc))
            return
        self.sourcesReady.emit(sources)
        try:
            self._start_chat()
        except (ValueError, TypeError) as exc:
            self._outcome = 'error'
            self._terminal(str(exc))

    def _search_failed(self, message):
        if not self._closed and self._phase == 'search':
            self._outcome = 'error'
            self._terminal(message)

    def _search_cancelled(self):
        if not self._closed and self._phase == 'search':
            self._outcome = 'cancelled'
            self._terminal('')

    def _request(self, phase, path, payload=None):
        self._phase = phase
        self._buffer, self._stream_error = b'', ''
        self._received_bytes, self._done = 0, False
        endpoint = self._release_target[0] if self._release_target else self._settings.endpoint
        timeout = self.release_timeout_ms if phase in ('unload', 'ps') else self.timeout_ms
        reply = self._network_request(endpoint, path, payload, timeout_ms=timeout)
        self._reply = reply
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply, phase))

    def _read(self, reply):
        if reply is not self._reply:
            return
        data = bytes(reply.readAll())
        self._received_bytes += len(data)
        if self._received_bytes > 4 * 1024 * 1024:
            self._stream_error = 'Ollamaの応答が大きすぎます。'
            reply.abort()
            return
        self._buffer += data
        if self._phase != 'chat':
            return
        # Decode only complete lines; a UTF-8 character may span network reads.
        while b'\n' in self._buffer:
            line, self._buffer = self._buffer.split(b'\n', 1)
            if not self._chunk(line):
                reply.abort()
                return

    def _chunk(self, line):
        if not line.strip():
            return True
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError('応答がJSON objectではありません。')
            if data.get('error'):
                raise ValueError(str(data['error'])[:500])
            message = data.get('message', {})
            if not isinstance(message, dict) or not isinstance(message.get('content', ''), str):
                raise ValueError('応答メッセージの形式が不正です。')
            text = message.get('content', '')
            if text:
                self._text += text
                self.textReceived.emit(text)
            self._done = data.get('done') is True
            return True
        except (ValueError, UnicodeError, TypeError) as exc:
            self._stream_error = f'Ollamaの応答を処理できません: {exc}'
            return False

    def _finished(self, reply, phase):
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._read(reply)
        self._reply = None
        body = self._buffer
        error = self._stream_error or self._error(reply, body)
        reply.deleteLater()
        if phase == 'chat':
            if not error and body.strip():
                self._chunk(body)
                error = self._stream_error
            if self._outcome != 'cancelled':
                if not error and not self._done:
                    error = '応答が完了する前に接続が終了しました。'
                if not error and not self._text.strip():
                    error = 'モデルから回答本文が返されませんでした。'
                self._outcome = 'error' if error else 'complete'
            self._terminal(error)
            return
        if self._outcome == 'cancelled' and phase == 'show':
            self._terminal('')
            return
        try:
            if error:
                raise ValueError(error)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('Ollamaの応答形式が不正です。')
            if data.get('error'):
                raise ValueError(str(data['error'])[:500])
            if phase == 'show':
                if data.get('remote_host') or data.get('remote_model'):
                    raise ValueError('ローカルにダウンロードしたモデルを選択してください。')
                capabilities = data.get('capabilities', [])
                if capabilities and 'completion' not in capabilities:
                    raise ValueError('このモデルはチャットに対応していません。別のモデルを選択してください。')
                if any(message.get('images') for message in self._messages) and 'vision' not in capabilities:
                    raise ValueError('選択したモデルの画像入力対応を確認できません。Gemma 4など画像対応モデルと最新のOllamaを使うか、新しい会話で画像を使わず送信してください。')
                self._model_info = data
                if self._settings.web_search:
                    self._start_search()
                else:
                    self._start_chat()
            elif phase == 'unload':
                if data.get('done') is not True:
                    raise ValueError('モデル解放の完了を確認できません。')
                self._check_released()
            elif phase == 'ps':
                if not isinstance(data.get('models'), list):
                    raise ValueError('ロード済みモデルの一覧を確認できません。')
                target = self._model if ':' in self._model else self._model + ':latest'
                loaded = any(isinstance(item, dict) and target in (item.get('name'), item.get('model'))
                             for item in data['models'])
                if not loaded:
                    self._release_target = None
                    self._idle()
                elif time.monotonic() >= self._release_deadline:
                    raise ValueError('モデルがまだメモリに残っています。')
                else:
                    self._phase = 'poll'
                    self._poll.start(300)
        except (ValueError, TypeError, UnicodeError) as exc:
            if phase in ('unload', 'ps'):
                self._phase = 'release_failed'
                self.status.emit(f'モデル解放を確認できません。「モデル解放を再試行」を押してください。{exc}')
                self.busyChanged.emit(True)
            else:
                self._outcome = 'error'
                self._terminal(str(exc))

    def _terminal(self, error):
        state = 'interrupted' if self._outcome == 'cancelled' else self._outcome
        self.result.emit(state, self._text, '' if state == 'interrupted' else error)
        self._final_status = (error if state == 'error' else
                              '中止しました。' if state == 'interrupted' else '応答が完了しました。')
        if self._release_target is None:
            self._idle()
        else:
            self.retry_release()

    def retry_release(self):
        if self._release_target is None or self._reply is not None or self._closed:
            return
        self._release_deadline = time.monotonic() + self.release_timeout_ms / 1000
        self.status.emit('モデルをメモリから解放しています…')
        self._request('unload', '/api/generate', dict(model=self._model, keep_alive=0, stream=False))
        self.busyChanged.emit(True)

    def _check_released(self):
        if not self._closed:
            self._request('ps', '/api/ps')

    def _idle(self):
        self._phase = 'idle'
        self.status.emit(self._final_status)
        self.busyChanged.emit(False)

    def cancel(self):
        if self._phase == 'search':
            self._search.cancel()
        elif self._phase in ('show', 'chat') and self._reply is not None:
            self._outcome = 'cancelled'
            self._reply.abort()

    def shutdown(self):
        self._closed = True
        self._poll.stop()
        self._search.shutdown()
        for name in ('_probe', '_reply'):
            reply = getattr(self, name)
            setattr(self, name, None)
            if reply is not None:
                reply.abort()
