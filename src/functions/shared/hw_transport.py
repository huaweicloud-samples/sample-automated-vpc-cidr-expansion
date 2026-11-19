"""带超时与重试上限的 HTTP 传输层。

设计口径（与 :mod:`handler_errors` 的异常分类一一对应）：

* **不假设运行环境装了哪个 HTTP 库**：优先用 ``requests``（函数运行环境内置），
  取不到时退回标准库 ``urllib.request``；两条路径的状态码判定与异常类型完全一致。
* 每个调用点都必须显式给超时，避免整条链路挂死在扩展窗口里。
* ``retries`` 只对「幂等的读」和「明确可重试的状态码」开放；
  写操作传 0，网络超时（结果未知）永不自动重发——由上层用轮询确认事实状态。
* 非 2xx 一律抛 :class:`ServiceError` 并带上状态码、请求编号与截断后的响应体。
"""

import json

from handler_errors import ServiceError, TransportError

DEFAULT_TIMEOUT_SECONDS = 15
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
_REQUEST_ID_HEADERS = ("x-request-id", "x-cff-request-id", "x-meeting-request-id", "resp_id")


class HttpResponse(object):
    def __init__(self, status_code, headers, body):
        self.status_code = int(status_code or 0)
        self.headers = dict(headers or {})
        self.body = body or b""

    @property
    def text(self):
        return self.body.decode("utf-8", "replace") if self.body else ""

    def json(self):
        if not self.body:
            return {}
        try:
            return json.loads(self.text)
        except ValueError:
            raise ServiceError(
                "响应体不是合法 JSON",
                status_code=self.status_code,
                request_id=self.request_id(),
                body=_truncate(self.text),
            )

    def request_id(self):
        lowered = {str(k).lower(): v for k, v in self.headers.items()}
        for name in _REQUEST_ID_HEADERS:
            if lowered.get(name):
                return lowered[name]
        return lowered.get("x-sdk-date")

    def ok(self):
        return 200 <= self.status_code < 300


class HttpTransport(object):
    """最小 HTTP 客户端；只做传输与重试，不掺业务语义。"""

    def __init__(self, logger=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
        self.logger = logger
        self.timeout_seconds = timeout_seconds
        self._session = None

    def request(self, method, url, headers=None, body=None, timeout=None, retries=0,
                retry_backoff_seconds=1.0, retry_on_status=RETRYABLE_STATUS):
        """发起请求。

        :param retries: 额外重试次数（0 表示只发一次）
        :raises TransportError: 始终未得到响应
        :raises ServiceError: 得到响应但状态码非 2xx
        """
        timeout = timeout or self.timeout_seconds
        attempts = max(int(retries), 0) + 1
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._send(method, url, headers, body, timeout)
            except TransportError as error:
                last_error = error
                self._log("warning", "请求未得到响应", method=method, url=url,
                          attempt=attempt, detail=str(error))
                if attempt >= attempts:
                    raise
                self._sleep(retry_backoff_seconds, attempt)
                continue

            if response.ok():
                return response

            retriable = response.status_code in retry_on_status and attempt < attempts
            self._log("warning" if retriable else "error", "服务返回错误响应",
                      method=method, url=url, status_code=response.status_code,
                      request_id=response.request_id(), attempt=attempt)
            if retriable:
                last_error = ServiceError(
                    "云服务调用失败", status_code=response.status_code,
                    request_id=response.request_id(), body=_truncate(response.text),
                    method=method, url=url)
                self._sleep(retry_backoff_seconds, attempt)
                continue
            raise ServiceError(
                "云服务调用失败",
                status_code=response.status_code,
                request_id=response.request_id(),
                body=_truncate(response.text),
                method=method,
                url=url,
                error_code=_error_code(response),
            )

        raise last_error or TransportError("请求失败", url=url)

    # ------------------------------------------------------------------
    def _send(self, method, url, headers, body, timeout):
        try:
            import requests
        except ImportError:
            return self._send_with_urllib(method, url, headers, body, timeout)

        try:
            if self._session is None:
                self._session = requests.Session()
            response = self._session.request(
                method=method.upper(), url=url, headers=headers, data=body, timeout=timeout)
        except requests.RequestException as error:
            raise TransportError("请求传输失败", url=url, detail=str(error))
        return HttpResponse(response.status_code, response.headers, response.content)

    def _send_with_urllib(self, method, url, headers, body, timeout):
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        request = Request(url, data=body, method=method.upper())
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=timeout) as raw:
                return HttpResponse(raw.getcode(), dict(raw.headers.items()), raw.read())
        except HTTPError as error:
            # 错误响应也要交给上层判状态码，才能区分「可重试」与「确定失败」
            return HttpResponse(
                error.code,
                dict(error.headers.items()) if error.headers else {},
                error.read() if hasattr(error, "read") else b"")
        except (URLError, OSError, ValueError) as error:
            raise TransportError("请求传输失败", url=url, detail=str(error))

    def _sleep(self, base_seconds, attempt):
        import time

        time.sleep(min(base_seconds * (2 ** (attempt - 1)), 8.0))

    def _log(self, level, message, **fields):
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(
            "%s %s", message, json.dumps(fields, default=str, ensure_ascii=False))


def _error_code(response):
    """从错误响应里尽量取出服务端错误码，便于按码决定要不要重试。"""
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error_msg") or payload.get("error_code")
    if error:
        return str(error)
    body = payload.get("body")
    if isinstance(body, dict):
        return str(body.get("error_code") or "") or None
    return None


def _truncate(text, limit=600):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."
