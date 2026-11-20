"""状态存储签名器：固定输入向量 + 可重复性 + 关键字段。

签名器的输入全部固定（时间、请求体、口令、端点），期望值是本地一次性算好后固化的
十六进制串：它锚定的是「实现变了就会被发现」，与真实服务端的口径由
状态存储账号/口令这对参数在部署时对齐（见 ``infra/locals.tf``）。
凭据在本文件里一律是占位符。
"""

from datetime import datetime

import ddb_signer

URL = "http://10.0.40.12:8900/"
TARGET = "DynamoDB_20120810.PutItem"
BODY = b'{"TableName":"demo-locks"}'
USERNAME = "<DDB_USER_NAME>"
PASSWORD = "<DDB_PASSWORD>"
REGION = "cn-north-4"
WHEN = datetime(2026, 5, 1, 10, 30, 0)

# 固定输入向量下的期望签名（SHA-256/HMAC 逐级派生的结果）
EXPECTED_SIGNATURE = "339987b21c38a042afdda09e664420152644789410832b20071a1b87561c6018"
EXPECTED_CREDENTIAL = ("%s/20260501/%s/dynamodb/aws4_request" % (USERNAME, REGION))


def sign(**overrides):
    arguments = dict(method="POST", url=URL, target=TARGET, body_bytes=BODY,
                     username=USERNAME, password=PASSWORD, region=REGION, when=WHEN)
    arguments.update(overrides)
    return ddb_signer.sign(**arguments)


def test_authorization_matches_the_fixed_vector():
    headers = sign()
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=%s, "
        "SignedHeaders=content-type;host;x-amz-date;x-amz-target, Signature=%s"
        % (EXPECTED_CREDENTIAL, EXPECTED_SIGNATURE))


def test_signature_is_repeatable_for_the_same_input():
    assert sign()["Authorization"] == sign()["Authorization"]
    assert sign()["X-Amz-Date"] == "20260501T103000Z"


def test_required_headers_are_all_present_and_are_the_signed_set():
    headers = sign()
    assert set(headers) == {"Content-Type", "X-Amz-Date", "X-Amz-Target", "Host",
                            "Authorization"}
    assert headers["Content-Type"] == "application/x-amz-json-1.0"
    assert headers["Host"] == "10.0.40.12:8900"
    assert headers["X-Amz-Target"] == TARGET
    # 参与签名的头就是这四个，顺序按字典序
    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-target" in headers["Authorization"]


def test_signature_uses_only_the_four_signed_headers_in_order():
    assert ddb_signer.SIGNED_HEADERS == ("content-type", "host", "x-amz-date", "x-amz-target")
    assert ddb_signer.SERVICE == "dynamodb"
    assert ddb_signer.TERM == "aws4_request"


def test_password_change_changes_the_signature():
    assert sign(password="<DDB_OTHER_PASSWORD>")["Authorization"] != \
        sign()["Authorization"]


def test_username_is_carried_into_the_credential_field():
    headers = sign(username="<DDB_OTHER_USER>")
    assert "Credential=<DDB_OTHER_USER>/" in headers["Authorization"]
    # 口令本身永远不出现在请求头里
    assert PASSWORD not in str(headers)


def test_empty_body_is_hashed_as_the_empty_string():
    """空体走真实空串摘要，而不是占位符；与有体时的签名必然不同。"""
    assert sign(body_bytes=None)["Authorization"] != sign(body_bytes=b"{}")["Authorization"]
    assert "UNSIGNED-PAYLOAD" not in str(sign(body_bytes=None))


def test_body_change_changes_the_signature_because_payload_is_really_hashed():
    """该接口不接受占位摘要：请求体摘要必须参与签名。"""
    assert sign(body_bytes=b'{"TableName":"demo-history"}')["Authorization"] != \
        sign()["Authorization"]
    assert "UNSIGNED-PAYLOAD" not in str(sign())


def test_target_and_host_are_part_of_the_signature():
    for overrides in ({"target": "DynamoDB_20120810.GetItem"},
                      {"url": "http://10.0.40.13:8900/"}):
        assert sign(**overrides)["Authorization"] != sign()["Authorization"]


def test_date_and_region_feed_the_key_derivation():
    """签名范围按「日期/区域/服务/终止符」逐级派生：日期或区域变了签名就变。"""
    assert sign(when=datetime(2026, 5, 2, 10, 30, 0))["Authorization"] != \
        sign()["Authorization"]
    assert "20260502/" in sign(when=datetime(2026, 5, 2, 10, 30, 0))["Authorization"]
    other_region = sign(region="cn-east-3")["Authorization"]
    assert "cn-east-3" in other_region and other_region != sign()["Authorization"]


def test_missing_region_still_produces_a_usable_scope():
    """区域缺失时退化为占位段，不抛异常（配置缺失由运行期配置的必填校验兜住）。"""
    headers = sign(region=None)
    assert "/region/dynamodb/aws4_request" in headers["Authorization"]
