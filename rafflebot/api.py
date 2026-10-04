import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        super().__init__(description)
        self.code = code
        self.description = description
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        data = json.dumps(params, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base + method, data=data,
                                         headers={"Content-Type": "application/json"})
        # Reads can be retried safely. Sends are never retried here, since a
        # lost HTTP response may still correspond to a successful publication.
        attempts = 3 if method in {"getMe", "getWebhookInfo", "getChat", "getChatMember", "getUpdates", "getCustomEmojiStickers"} else 1
        for attempt in range(attempts):
            try:
                return self._request(request, params.get("timeout", 0) + 30)
            except TelegramError as e:
                if e.code != 0 or attempt == attempts - 1:
                    raise
                time.sleep(attempt + 1)

    def document(self, chat_id, filename, content, caption=""):
        boundary = uuid.uuid4().hex
        chunks = []
        for key, value in {"chat_id": chat_id, "caption": caption}.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        chunks.extend([content, f"\r\n--{boundary}--\r\n".encode()])
        req = urllib.request.Request(self.base + "sendDocument", data=b"".join(chunks),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._request(req, 60)

    @staticmethod
    def _request(req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise TelegramError(error.code, "Ошибка ответа Telegram") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Never log the request URL: it contains the bot token.
            raise TelegramError(0, "Нет соединения с Telegram") from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0), result.get("description", "Ошибка Telegram"),
                                result.get("parameters", {}).get("retry_after", 0))
        return result["result"]
