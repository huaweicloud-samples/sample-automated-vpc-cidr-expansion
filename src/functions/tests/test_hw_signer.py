"""华为云 API 网关签名的规范要点：规范 URI、查询串、参与签名的头、请求体摘要。"""

import hw_signer
from hw_signer import (ALGORITHM, EMPTY_PAYLOAD_HASH, HEADER_AUTH, HEADER_CONTENT_HASH,
                       HEADER_DATE, UNSIGNED_PAYLOAD, build_request, canonical_query_string,
                       canonical_uri, payload_hash, signed_header_names)
from helpers import (PLACEHOLDER_ACCESS_KEY, PLACEHOLDER_PROJECT_ID, PLACEHOLDER_SECRET_KEY,
                     PLACEHOLDER_SECURITY_TOKEN, FakeCredentials)

BASE = "https://vpc.cn-north-4.myhuaweicloud.com"
WHEN = "20260501T103000Z"


def credentials(**overrides):
    item = FakeCredentials()
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def test_algorithm_and_constants():
    assert ALGORITHM == "SDK-HMAC-SHA256"
    assert UNSIGNED_PAYLOAD == "UNSIGNED-PAYLOAD"
    assert EMPTY_PAYLOAD_HASH == ("e3b0c44298fc1c149afbf4c8996fb924"
                                  "27ae41e4649b934ca495991b7852b855")


def test_canonical_uri_keeps_trailing_slash_and_reencodes_segments():
    """规范 URI 以 / 结尾，实际发出的 URL 不带尾斜杠：写错直接 401。"""
    assert canonical_uri("/v3/p/vpc/vpcs") == "/v3/p/vpc/vpcs/"
    assert canonical_uri("/") == "/"
    assert canonical_uri(None) == "/"
    # 先整体解码、再按段重新编码
    assert canonical_uri("/v3/p/vpc/subnets/a%20b") == "/v3/p/vpc/subnets/a%20b/"
    assert canonical_uri("/v3/p/vpc/subnets/a b") == "/v3/p/vpc/subnets/a%20b/"
    assert canonical_uri("/v3/p/urn%3Aa%2Fb") == "/v3/p/urn%3Aa/b/"


def test_canonical_query_string_sorts_and_encodes():
    assert canonical_query_string([("b", "2"), ("a", "1")]) == "a=1&b=2"
    assert canonical_query_string([("name", "a b")]) == "name=a%20b"
    assert canonical_query_string([("tags", ["b", "a"])]) == "tags=a&tags=b"
    assert canonical_query_string([("dhcp_enable", True)]) == "dhcp_enable=true"
    assert canonical_query_string(None) == ""


def test_signed_header_names_ignore_underscore_keys():
    """键名含下划线的头不参与签名。"""
    headers = {"Host": "h", "X-Sdk-Date": "d", "x_custom": "v", "Accept": "a", "CONTENT-TYPE": "c"}
    assert signed_header_names(headers) == ["accept", "content-type", "host", "x-sdk-date"]


def test_payload_hash_rules():
    headers = {"Content-Type": "application/json"}
    assert payload_hash(dict(headers), b"{}") == hw_signer.hashlib.sha256(b"{}").hexdigest()
    assert "X-Sdk-Content-Sha256" not in headers  # JSON 体不写摘要头
    plain = {"Content-Type": "text/plain"}
    assert payload_hash(plain, b"hello") == UNSIGNED_PAYLOAD
    assert plain[HEADER_CONTENT_HASH] == UNSIGNED_PAYLOAD
    assert payload_hash({"Content-Type": "application/json"}, None) == EMPTY_PAYLOAD_HASH


def test_get_signature_matches_the_fixed_vector():
    headers, url, _body = build_request(
        BASE, "GET", "/v3/p/vpc/vpcs", credentials(),
        query_params=[("limit", "2000"), ("vpc_id", "vpc-1")],
        headers={"Accept": "application/json"}, when=WHEN)
    assert url == BASE + "/v3/p/vpc/vpcs?limit=2000&vpc_id=vpc-1"
    assert headers[HEADER_DATE] == WHEN
    assert headers["Host"] == "vpc.cn-north-4.myhuaweicloud.com"
    assert headers["X-Security-Token"] == PLACEHOLDER_SECURITY_TOKEN
    assert headers["X-Project-Id"] == PLACEHOLDER_PROJECT_ID
    assert headers[HEADER_AUTH] == (
        "SDK-HMAC-SHA256 Access=%s, "
        "SignedHeaders=accept;host;x-project-id;x-sdk-date;x-security-token, "
        "Signature=4e92c68a9f5ba488d269333e2d81ecfc1c5a0b17728d0f5eca32fdfb30c497a8"
        % PLACEHOLDER_ACCESS_KEY)


def test_json_write_signature_matches_the_fixed_vector():
    """附加网段这类写请求：请求体摘要参与签名，且只发一次不重发。"""
    body = b'{"vpc":{"extend_cidrs":["10.1.0.0/16"]}}'
    headers, url, payload = build_request(
        BASE, "PUT", "/v3/p/vpc/vpcs/vpc-1/add-extend-cidr", credentials(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        body=body, when=WHEN)
    assert payload == body
    assert url == BASE + "/v3/p/vpc/vpcs/vpc-1/add-extend-cidr"
    assert HEADER_CONTENT_HASH not in headers
    assert headers[HEADER_AUTH].endswith(
        "Signature=c8e744831f755b6112ee1aaa2f0b8942b5332d7a705afccf38374738c09c9d9a")


def test_non_json_body_uses_the_unsigned_payload_marker():
    headers, _url, _body = build_request(
        "https://smn.cn-north-4.myhuaweicloud.com", "POST",
        "/v2/p/notifications/topics/urn:smn:1/publish", credentials(),
        headers={"Content-Type": "text/plain"}, when=WHEN)
    assert headers[HEADER_CONTENT_HASH] == UNSIGNED_PAYLOAD
    assert "x-sdk-content-sha256" in headers[HEADER_AUTH]
    assert headers[HEADER_AUTH].endswith(
        "Signature=b7b016904a869b979e6148a19b0eeea8597e9ff29e6484f469aa28e0fc924c94")


def test_signature_changes_with_the_secret_key_and_the_timestamp():
    baseline = build_request(BASE, "GET", "/v3/p/vpc/vpcs", credentials(), when=WHEN)[0]
    rotated = build_request(BASE, "GET", "/v3/p/vpc/vpcs",
                            credentials(secret_key="<HW_OTHER_SECRET_KEY>"), when=WHEN)[0]
    later = build_request(BASE, "GET", "/v3/p/vpc/vpcs", credentials(),
                          when="20260501T103001Z")[0]
    assert baseline[HEADER_AUTH] != rotated[HEADER_AUTH]
    assert baseline[HEADER_AUTH] != later[HEADER_AUTH]


def test_signature_is_repeatable_for_identical_input():
    first = build_request(BASE, "GET", "/v3/p/vpc/vpcs", credentials(), when=WHEN)[0]
    second = build_request(BASE, "GET", "/v3/p/vpc/vpcs", credentials(), when=WHEN)[0]
    assert first[HEADER_AUTH] == second[HEADER_AUTH]


def test_access_key_without_security_token_still_signs():
    """委托未签发临时令牌时（本地联调）令牌头缺席，签名照常成立。"""
    headers, _url, _body = build_request(BASE, "GET", "/v3/p/vpc/vpcs",
                                         credentials(security_token=None), when=WHEN)
    assert "X-Security-Token" not in headers
    assert "x-security-token" not in headers[HEADER_AUTH]


def test_none_valued_headers_are_dropped_before_signing():
    headers, _url, _body = build_request(BASE, "GET", "/v3/p/vpc/vpcs", credentials(),
                                         headers={"Accept": None, "X-Trace": " t "}, when=WHEN)
    assert "Accept" not in headers
    assert headers["X-Trace"] == "t"
