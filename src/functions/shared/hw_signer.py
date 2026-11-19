"""华为云 API 网关 v3 签名（AK/SK + 安全令牌）。

巡检与扩容两个函数都只依赖这一个签名器，因此这里只做「与官方 SDK 行为逐条对齐」
的最小实现，全部走标准库，不引入第三方签名依赖。

规范要点：

1. 算法名 ``SDK-HMAC-SHA256``；签名密钥是密钥原文本身，
   **没有**按日期/区域/服务逐级派生的过程。
2. 待签名串三段：算法名、``X-Sdk-Date``、规范化请求的 SHA-256 十六进制值。
3. 规范化请求 = 方法 + 规范 URI + 规范查询串 + 规范请求头 + 参与签名的头名 + 请求体摘要。
4. **规范 URI 以 ``/`` 结尾，实际发出的 URL 不带这个尾斜杠**；
   写错直接 401。路径先整体解码、再按段重新编码。
5. 参与签名的头 = 键名不含下划线的所有头，小写去重后按字典序；
   ``host``/``x-sdk-date``/``x-security-token``/``x-project-id`` 均参与。
6. 请求体摘要：JSON 请求体取 SHA-256；非 JSON 请求体写
   ``X-Sdk-Content-Sha256: UNSIGNED-PAYLOAD`` 并以该值参与签名；空体用空串摘要。
"""

import hashlib
import hmac
from urllib.parse import quote, unquote

ALGORITHM = "SDK-HMAC-SHA256"
DATE_FORMAT = "%Y%m%dT%H%M%SZ"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

HEADER_HOST = "Host"
HEADER_DATE = "X-Sdk-Date"
HEADER_AUTH = "Authorization"
HEADER_CONTENT_HASH = "X-Sdk-Content-Sha256"
HEADER_SECURITY_TOKEN = "X-Security-Token"
HEADER_PROJECT_ID = "X-Project-Id"

_JSON_CONTENT_TYPES = ("application/json",)


def canonical_uri(path):
    """规范化请求路径：整体解码 → 按段重编码 → 补尾斜杠。"""
    segments = unquote(path or "/").split("/")
    encoded = "/".join(quote(segment, safe="~") for segment in segments)
    if not encoded.endswith("/"):
        encoded += "/"
    return encoded


def canonical_query_string(query_params):
    """规范查询串：按键名排序后逐条编码；多值按键分组展开。"""
    pairs = []
    for key, value in sorted(list(query_params or [])):
        encoded_key = quote(str(key), safe="~")
        if isinstance(value, (list, tuple)):
            for item in sorted(str(v) for v in value):
                pairs.append((encoded_key, quote(item, safe="~")))
        elif isinstance(value, bool):
            pairs.append((encoded_key, quote(str(value).lower(), safe="~")))
        else:
            pairs.append((encoded_key, quote(str(value), safe="~")))
    return "&".join("%s=%s" % pair for pair in pairs)


def signed_header_names(headers):
    """键名不含下划线的头参与签名：小写去重后按字典序。"""
    names = set()
    for key in headers:
        lowered = str(key).lower()
        if "_" not in lowered:
            names.add(lowered)
    return sorted(names)


def _header_lookup(headers, name):
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return value
    return None


def payload_hash(headers, body):
    """按规范求请求体摘要，并按需补写摘要头。"""
    content_type = _header_lookup(headers, "Content-Type") or ""
    if content_type and not content_type.startswith(_JSON_CONTENT_TYPES):
        headers[HEADER_CONTENT_HASH] = UNSIGNED_PAYLOAD
    if not body:
        return EMPTY_PAYLOAD_HASH
    declared = _header_lookup(headers, HEADER_CONTENT_HASH)
    if declared:
        return declared
    return hashlib.sha256(_as_bytes(body)).hexdigest()


def build_request(base_url, method, path, credentials, query_params=None, headers=None,
                  body=None, when=None):
    """返回 ``(请求头, 实际请求 URL)``。

    :param base_url: ``https://<服务域名>``，不含路径
    :param path: 资源路径，路径参数需已按 URL 规范编码
    :param credentials: :class:`invocation.Credentials`
    :param query_params: ``[(key, value), ...]``
    :param when: 固定 UTC 时间字符串（``%Y%m%dT%H%M%SZ``），单测构造可复现签名用
    """
    request_headers = {}
    for key, value in (headers or {}).items():
        if value is not None:
            request_headers[key] = _normalize(value)

    host = base_url.split("://")[-1].rstrip("/")
    request_headers[HEADER_HOST] = host
    request_headers[HEADER_DATE] = when or _utc_stamp()

    if getattr(credentials, "security_token", None):
        request_headers[HEADER_SECURITY_TOKEN] = _normalize(credentials.security_token)
    if getattr(credentials, "project_id", None):
        request_headers[HEADER_PROJECT_ID] = _normalize(credentials.project_id)

    body_bytes = _as_bytes(body) if body else b""
    digest = payload_hash(request_headers, body_bytes if body else None)

    lowered = {str(k).lower(): v for k, v in request_headers.items()}
    names = signed_header_names(request_headers)
    canonical_headers = "\n".join("%s:%s" % (n, lowered[n]) for n in names) + "\n"
    canonical_query = canonical_query_string(query_params)

    canonical_request = "\n".join([
        method.upper(),
        canonical_uri(path),
        canonical_query,
        canonical_headers,
        ";".join(names),
        digest,
    ])
    string_to_sign = "\n".join([
        ALGORITHM,
        request_headers[HEADER_DATE],
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(_as_bytes(credentials.secret_key),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    request_headers[HEADER_AUTH] = (
        "%s Access=%s, SignedHeaders=%s, Signature=%s"
        % (ALGORITHM, credentials.access_key, ";".join(names), signature))

    url = "%s%s" % (base_url.rstrip("/"), path)
    if canonical_query:
        url = "%s?%s" % (url, canonical_query)
    return request_headers, url, body_bytes


def _utc_stamp():
    from datetime import datetime

    return datetime.utcnow().strftime(DATE_FORMAT)


def _as_bytes(value):
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _normalize(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value).strip()
