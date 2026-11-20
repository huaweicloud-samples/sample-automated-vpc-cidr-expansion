"""状态存储（DynamoDB 兼容接口）的请求签名器。

状态存储是 GeminiDB 的 DynamoDB 兼容接口，按数据库账号鉴权：
**账号当 Key ID、口令当 Key Secret**，每个请求都要带签名
（口径见 GeminiDB 用户指南「程序代码连接」章节）。
这套接口的签名与华为云 API 网关的做法完全不同，因此不复用 :mod:`hw_signer`，
单独实现一份只覆盖本实践用到的 ``application/x-amz-json-1.0`` 场景的最小签名器，
仍然只用标准库。

签名要点：

1. 参与签名的头固定为 ``content-type``、``host``、``x-amz-date``、``x-amz-target``；
   小写、按字典序、值折叠空白。
2. 请求体摘要用真实 SHA-256（该接口不接受 ``UNSIGNED-PAYLOAD`` 占位摘要）。
3. 以存储口令为根逐级派生签名密钥：日期 → 区域 → ``dynamodb`` → ``aws4_request``，
   签名范围 ``<date>/<region>/dynamodb/aws4_request``。
4. 该接口在 VPC 内以明文 HTTP 暴露，签名头照发；如需 HTTPS 由实例侧开启 SSL，
   届时只要把 ``DDB_ENDPOINT`` 改成带 ``https://`` 的地址即可，签名逻辑不变。
"""

import hashlib
import hmac
from datetime import datetime

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "dynamodb"
TERM = "aws4_request"
DATE_STAMP = "%Y%m%d"
AMZ_DATE = "%Y%m%dT%H%M%SZ"
SIGNED_HEADERS = ("content-type", "host", "x-amz-date", "x-amz-target")


def sign(method, url, target, body_bytes, username, password, region, when=None):
    """返回可直接发出的请求头（含 ``Authorization``）。

    :param url: 完整请求地址，如 ``http://10.0.40.12:8900/``
    :param target: ``DynamoDB_20120810.PutItem`` 之类的操作名
    :param username: 状态存储账号（充当 Access Key）
    :param password: 状态存储口令（充当 Secret Key）
    :param region: 签名范围里的区域段，取函数下发的 ``HW_REGION``
    :param when: ``datetime`` 实例，单测用它构造可复现签名
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    stamp = when or datetime.utcnow()
    date_stamp = stamp.strftime(DATE_STAMP)
    amz_date = stamp.strftime(AMZ_DATE)

    payload_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    headers = {
        "content-type": "application/x-amz-json-1.0",
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-target": target,
    }
    canonical_headers = "".join("%s:%s\n" % (name, headers[name].strip())
                                for name in SIGNED_HEADERS)
    canonical_request = "\n".join([
        method.upper(),
        path,
        "",                       # 查询串：DynamoDB 协议一律 POST 到根路径
        canonical_headers,
        ";".join(SIGNED_HEADERS),
        payload_hash,
    ])

    scope = "/".join([date_stamp, region or "region", SERVICE, TERM])
    string_to_sign = "\n".join([
        ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _signing_key(password, date_stamp, region or "region")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "Content-Type": headers["content-type"],
        "X-Amz-Date": amz_date,
        "X-Amz-Target": target,
        "Host": host,
        "Authorization": "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
            ALGORITHM, username, scope, ";".join(SIGNED_HEADERS), signature),
    }


def _signing_key(password, date_stamp, region):
    def _hmac(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + str(password)).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, SERVICE)
    return _hmac(k_service, TERM)
