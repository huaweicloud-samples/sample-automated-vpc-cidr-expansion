"""HTTP 传输层：超时缺省、重试口径、响应体与请求编号的归一。

`_send` 被替身接管：本模块只验证重试计数、状态码判定与异常类型，
不会真的发出任何请求。
"""

import json

import pytest
from handler_errors import ServiceError, TransportError
from hw_transport import (DEFAULT_TIMEOUT_SECONDS, RETRYABLE_STATUS, HttpTransport,
                          HttpResponse)


class RecordingTransport(HttpTransport):
    """按脚本回放响应/异常的传输层；``_send`` 是唯一被替换的环节。"""

    def __init__(self, script, **kwargs):
        super(RecordingTransport, self).__init__(**kwargs)
        self._script = list(script)
        self.sent = []
        self.slept = []

    def _send(self, method, url, headers, body, timeout):
        self.sent.append({"method": method, "url": url, "headers": dict(headers or {}),
                          "body": body, "timeout": timeout})
        item = self._script.pop(0) if self._script else HttpResponse(200, {}, b"{}")
        if isinstance(item, Exception):
            raise item
        return item

    def _sleep(self, base_seconds, attempt):
        self.slept.append((base_seconds, attempt))


def response(status_code=200, payload=None, headers=None, raw=None):
    body = raw if raw is not None else (
        json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"))
    return HttpResponse(status_code, headers or {}, body)


# ----------------------------------------------------------------------
# 响应对象
# ----------------------------------------------------------------------
def test_empty_body_reads_as_an_empty_json_object():
    assert HttpResponse(204, {}, b"").json() == {}
    assert HttpResponse(204, {}, None).text == ""


def test_a_non_json_body_becomes_a_service_error_with_context():
    raw = response(500, raw=b"<html>gateway error</html>")
    with pytest.raises(ServiceError) as error:
        raw.json()
    assert error.value.status_code == 500
    assert "gateway error" in error.value.body


@pytest.mark.parametrize("headers,expected", [
    ({"x-request-id": "req-1"}, "req-1"),
    ({"X-Request-Id": "req-upper"}, "req-upper"),
    ({"x-cff-request-id": "cff-1"}, "cff-1"),
    ({"x-meeting-request-id": "mt-1"}, "mt-1"),
    ({"resp_id": "resp-1"}, "resp-1"),
    ({"x-sdk-date": "20260501T103000Z"}, "20260501T103000Z"),
    ({"other": "x"}, None),
    ({}, None),
])
def test_request_id_prefers_the_first_meaningful_header(headers, expected):
    assert HttpResponse(200, headers, b"{}").request_id() == expected


def test_ok_covers_only_the_two_hundred_band():
    assert HttpResponse(200, {}, b"{}").ok() is True
    assert HttpResponse(299, {}, b"{}").ok() is True
    assert HttpResponse(300, {}, b"{}").ok() is False
    assert HttpResponse(0, {}, b"{}").ok() is False
    assert HttpResponse(None, {}, b"{}").status_code == 0


# ----------------------------------------------------------------------
# 重试口径
# ----------------------------------------------------------------------
def test_a_successful_read_is_sent_once_with_the_declared_timeout():
    transport = RecordingTransport([response(200, {"vpcs": []})])
    result = transport.request("GET", "https://vpc.demo/vpcs", timeout=15, retries=2)
    assert result.status_code == 200
    assert len(transport.sent) == 1
    assert transport.slept == []


def test_timeout_falls_back_to_the_module_default():
    transport = RecordingTransport([response()])
    transport.request("GET", "https://vpc.demo/vpcs")
    assert transport.sent[0]["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_retryable_status_is_retried_then_reported_as_success():
    transport = RecordingTransport([response(503, {"error_msg": "稍后再试"}),
                                    response(200, {"vpcs": []})])
    result = transport.request("GET", "https://vpc.demo/vpcs", retries=2)
    assert result.status_code == 200
    assert len(transport.sent) == 2
    # 退避只作用在重试次数上，等待由 _sleep 记录（测试里不真的睡）
    assert transport.slept == [(1.0, 1)]


def test_writes_are_never_retried_even_for_a_throttled_response():
    transport = RecordingTransport([response(429, {"error_msg": "请求过多"})])
    with pytest.raises(ServiceError) as error:
        transport.request("PUT", "https://vpc.demo/add-extend-cidr", retries=0)
    assert error.value.status_code == 429
    assert error.value.error_code == "请求过多"
    assert error.value.retryable_by_status() is True
    assert len(transport.sent) == 1
    assert transport.slept == []


@pytest.mark.parametrize("status_code", sorted(RETRYABLE_STATUS))
def test_every_declared_retryable_status_is_retried_once(status_code):
    transport = RecordingTransport([response(status_code, {}), response(200, {})])
    assert transport.request("GET", "https://ces.demo/metric-data", retries=1).status_code == 200
    assert len(transport.sent) == 2


@pytest.mark.parametrize("status_code", [400, 403, 404, 405, 408, 409, 501])
def test_client_errors_stop_immediately_except_the_declared_exceptions(status_code):
    transport = RecordingTransport([response(status_code, {"error_msg": "拒绝"})])
    with pytest.raises(ServiceError):
        transport.request("POST", "https://vpc.demo/subnets", retries=2)
    expected = 3 if status_code in RETRYABLE_STATUS else 1
    assert len(transport.sent) == expected


def test_a_deadline_that_never_returns_a_response_stays_a_transport_error():
    transport = RecordingTransport([TransportError("连接超时"), TransportError("连接超时")])
    with pytest.raises(TransportError):
        transport.request("GET", "https://vpc.demo/vpcs", retries=1)
    assert len(transport.sent) == 2
    assert transport.slept == [(1.0, 1)]


def test_negative_retry_counts_degrade_to_a_single_attempt():
    transport = RecordingTransport([response(500, {"error_msg": "服务异常"})])
    with pytest.raises(ServiceError):
        transport.request("GET", "https://vpc.demo/vpcs", retries=-5)
    assert len(transport.sent) == 1


def test_service_error_carries_the_method_url_and_truncated_body():
    payload = {"body": {"error_code": "VPC.0512", "error_msg": "网段冲突"},
               "filler": "x" * 700}
    transport = RecordingTransport([response(400, payload)])
    with pytest.raises(ServiceError) as error:
        transport.request("PUT", "https://vpc.demo/x", body=b"{}", retries=0)
    assert error.value.fields["method"] == "PUT"
    assert error.value.fields["url"] == "https://vpc.demo/x"
    assert error.value.error_code == "VPC.0512"
    assert error.value.body.endswith("...")
    assert len(error.value.body) == 603


def test_top_level_error_fields_win_over_the_nested_body():
    transport = RecordingTransport([response(403, {"error_msg": "无权操作",
                                                   "error_code": "Auth.0001"})])
    with pytest.raises(ServiceError) as error:
        transport.request("POST", "https://vpc.demo/subnets")
    assert error.value.error_code == "无权操作"


def test_error_code_lookup_survives_an_unparseable_body():
    transport = RecordingTransport([response(400, raw=b"not json at all")])
    with pytest.raises(ServiceError) as error:
        transport.request("GET", "https://vpc.demo/vpcs")
    assert error.value.error_code is None
    assert error.value.body == "not json at all"
