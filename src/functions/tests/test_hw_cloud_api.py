"""服务端调用收口：路径与报文形态、读写重试口径、响应字段归一。

传输层替身只记录报文并回放响应，不打开套接字；凭据是占位符。
断言重点是三类口径同时成立：

1. 报文形状与华为云接口一致（路径、请求体字段名、查询参数）；
2. 「读可重试、写不盲重试」体现在 ``retries`` 与 ``timeout`` 上；
3. 响应字段缺失/异常时抛语义异常，而不是把 ``None`` 交给上层。
"""

import json
from urllib.parse import quote

import pytest
from handler_errors import ConfigError, ServiceError
from helpers import (PLACEHOLDER_DDB_PASSWORD, PLACEHOLDER_SECRET_KEY, FakeCredentials, REGION,
                     VPC_ID)
from hw_cloud_api import CloudApi
from hw_transport import HttpResponse

PROJECT_ID = "<HW_PROJECT_ID>"
TOPIC_URN = "urn:smn:cn-north-4:demo:cidr-notify"


class FakeTransport(object):
    def __init__(self, responses=None):
        self.requests = []
        self._responses = list(responses or [])

    def request(self, method, url, headers=None, body=None, timeout=None, retries=0):
        self.requests.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "json": json.loads(body.decode("utf-8")) if body else None,
            "timeout": timeout, "retries": retries,
        })
        response = self._responses.pop(0) if self._responses else HttpResponse(200, {}, b"{}")
        if not response.ok():
            raise ServiceError("云服务调用失败", status_code=response.status_code,
                               body=response.text, method=method, url=url)
        return response

    @property
    def last(self):
        return self.requests[-1]


def api(*responses, **kwargs):
    transport = kwargs.pop("transport", None) or FakeTransport(list(responses))
    client = CloudApi(FakeCredentials(**kwargs), REGION, PROJECT_ID, transport=transport)
    return client, transport


def ok(payload):
    return HttpResponse(200, {"x-request-id": "req-1"},
                        json.dumps(payload, ensure_ascii=False).encode("utf-8"))


# ----------------------------------------------------------------------
# 装配与签名
# ----------------------------------------------------------------------
def test_every_request_is_signed_with_a_temporary_token():
    client, transport = api(ok({"vpcs": [], "metadata": {}}))
    client.list_vpcs()
    headers = transport.last["headers"]
    assert headers["Authorization"].startswith("SDK-HMAC-SHA256 ")
    assert "Access=" in headers["Authorization"] and "Signature=" in headers["Authorization"]
    assert "SignedHeaders=accept;host;x-project-id;x-sdk-date;x-security-token" in \
        headers["Authorization"]
    assert headers["X-Security-Token"] == "<HW_SECURITY_TOKEN>"
    assert headers["X-Sdk-Date"]
    assert headers["X-Project-Id"] == PROJECT_ID
    assert headers["Host"] == "vpc.%s.myhuaweicloud.com" % REGION
    # 长期密钥与状态存储口令都不进报文
    assert PLACEHOLDER_SECRET_KEY not in json.dumps(headers)
    assert PLACEHOLDER_DDB_PASSWORD not in json.dumps(headers)


def test_region_and_project_are_required_to_build_the_endpoint_set():
    class NoProjectCredentials(FakeCredentials):
        def __init__(self):
            super(NoProjectCredentials, self).__init__(project_id=None)

    with pytest.raises(ConfigError) as error:
        CloudApi(FakeCredentials(), "", PROJECT_ID, transport=FakeTransport())
    assert error.value.fields["key"] == "HW_REGION"

    with pytest.raises(ConfigError) as error:
        CloudApi(NoProjectCredentials(), REGION, None, transport=FakeTransport())
    assert error.value.fields["key"] == "HW_PROJECT_ID"


def test_project_id_is_taken_from_the_invocation_context_when_not_given():
    transport = FakeTransport([ok({"vpc": {"cidr": "10.0.0.0/16"}})])
    client = CloudApi(FakeCredentials(project_id="<HW_PROJECT_ID>"), REGION, None,
                      transport=transport)
    client.show_vpc(VPC_ID)
    assert client.project_id == "<HW_PROJECT_ID>"
    assert "/v3/%s/" % quote("<HW_PROJECT_ID>", safe="") in transport.last["url"]


# ----------------------------------------------------------------------
# VPC 网段
# ----------------------------------------------------------------------
def test_list_vpcs_follows_the_marker_cursor_and_normalizes_tags():
    page_one = ok({"vpcs": [{"id": "vpc-1", "name": "a", "cidr": "10.0.0.0/16",
                             "status": "AVAILABLE", "tags": {"cidr-expansion": "managed"}},
                            {"id": "vpc-2", "name": "b", "cidr": "10.1.0.0/16",
                             "tags": [{"key": "cidr-expansion", "value": "managed"}]}],
                   "metadata": {"marker": "page-2"}})
    page_two = ok({"vpcs": [{"id": "vpc-3", "name": "c", "tags": None}],
                   "page_info": {"next_marker": "page-3"}})
    page_three = ok({"vpcs": [], "metadata": {}})
    client, transport = api(page_one, page_two, page_three)
    collected = client.list_vpcs()
    assert [item["id"] for item in collected] == ["vpc-1", "vpc-2", "vpc-3"]
    assert collected[0]["tags"] == {"cidr-expansion": "managed"}
    # 两种标签形态都归一成字典，纳管筛选才能在本地完成
    assert collected[1]["tags"] == {"cidr-expansion": "managed"}
    assert collected[2]["tags"] == {}
    assert "limit=2000" in transport.requests[0]["url"]
    assert "marker=page-2" in transport.requests[1]["url"]
    assert "marker=page-3" in transport.requests[2]["url"]
    assert transport.requests[0]["retries"] == 2
    assert [item["json"] for item in transport.requests] == [None, None, None]


def test_list_vpcs_falls_back_to_the_last_object_id_and_stops_on_a_repeat():
    client, transport = api(ok({"vpcs": [{"id": "vpc-a"}]}), ok({"vpcs": [{"id": "vpc-a"}]}))
    assert [item["id"] for item in client.list_vpcs()] == ["vpc-a", "vpc-a"]
    assert "marker=vpc-a" in transport.requests[1]["url"]
    assert len(transport.requests) == 2


def test_list_vpcs_stops_on_an_empty_page_without_an_extra_round():
    client, transport = api(ok({"vpcs": [], "metadata": {"marker": "still-there"}}))
    assert client.list_vpcs() == []
    assert len(transport.requests) == 1


def test_show_vpc_reads_main_and_attached_blocks():
    client, transport = api(ok({"vpc": {"id": VPC_ID, "name": "demo", "cidr": "10.0.0.0/16",
                                        "extend_cidrs": ["10.1.0.0/16"], "status": "AVAILABLE"}}))
    vpc = client.show_vpc(VPC_ID)
    assert vpc == {"id": VPC_ID, "name": "demo", "cidr": "10.0.0.0/16",
                   "extend_cidrs": ["10.1.0.0/16"], "status": "AVAILABLE"}
    assert transport.last["method"] == "GET"
    assert "/v3/%s/vpc/vpcs/%s" % (quote(PROJECT_ID, safe=""), VPC_ID) in transport.last["url"]


def test_show_vpc_rejects_a_response_without_the_vpc_field():
    client, _transport = api(ok({"request_id": "req-1"}))
    with pytest.raises(ServiceError) as error:
        client.show_vpc(VPC_ID)
    assert "vpc" in error.value.message


def test_add_extend_cidr_is_a_single_unretried_write():
    client, transport = api(ok({"vpc": {"id": VPC_ID, "extend_cidrs": ["10.1.0.0/16"]}}))
    client.add_extend_cidr(VPC_ID, "10.1.0.0/16")
    assert transport.last["method"] == "PUT"
    assert transport.last["url"].endswith("/vpc/vpcs/%s/add-extend-cidr" % VPC_ID)
    assert transport.last["json"] == {"vpc": {"extend_cidrs": ["10.1.0.0/16"]}}
    assert transport.last["retries"] == 0
    assert transport.last["timeout"] == 30


def test_wait_for_extend_cidr_polls_until_the_block_shows_up():
    client, transport = api(ok({"vpc": {"extend_cidrs": []}}),
                            ok({"vpc": {"extend_cidrs": ["10.1.0.0/16"]}}))
    waited = []
    outcome = client.wait_for_extend_cidr(VPC_ID, "10.1.0.0/16", 10, 60,
                                          sleeper=waited.append)
    assert outcome["state"] == "associated"
    assert outcome["waited_seconds"] == 10
    assert waited == [10]
    assert outcome["vpc"]["extend_cidrs"] == ["10.1.0.0/16"]


def test_wait_for_extend_cidr_reports_the_deadline_it_reached():
    client, transport = api(*[ok({"vpc": {"extend_cidrs": []}}) for _ in range(4)])
    waited = []
    outcome = client.wait_for_extend_cidr(VPC_ID, "10.9.0.0/16", 20, 60, sleeper=waited.append)
    assert outcome["state"] == "timeout"
    assert outcome["waited_seconds"] == 60
    assert waited == [20, 20, 20]


# ----------------------------------------------------------------------
# 子网
# ----------------------------------------------------------------------
def test_list_subnets_pages_until_the_cursor_repeats():
    client, transport = api(ok({"subnets": [{"id": "subnet-1"}], "metadata": {"marker": "m2"}}),
                            ok({"subnets": [{"id": "subnet-2"}], "metadata": {"marker": "m2"}}))
    collected = client.list_subnets(VPC_ID)
    assert [item["id"] for item in collected] == ["subnet-1", "subnet-2"]
    assert "vpc_id=%s" % VPC_ID in transport.requests[0]["url"]
    assert "limit=2000" in transport.requests[0]["url"]


def test_list_subnets_falls_back_to_the_last_id_as_cursor():
    client, transport = api(ok({"subnets": [{"id": "subnet-1"}]}),
                            ok({"subnets": [], "metadata": {}}))
    assert client.list_subnets(VPC_ID) == [{"id": "subnet-1"}]
    assert "marker=subnet-1" in transport.last["url"]


def test_create_subnet_maps_optional_fields_and_flattens_tags():
    client, transport = api(ok({"subnet": {"id": "subnet-new", "name": "app-public-2"}}))
    created = client.create_subnet(VPC_ID, {
        "name": "app-public-2", "cidr": "10.1.0.0/28", "gateway_ip": "10.1.0.1",
        "availability_zone": "cn-north-4a", "description": "自动扩容子网（层级 public）",
        "dhcp_enable": False, "primary_dns": "100.125.1.25", "secondary_dns": None,
        "dns_list": ["100.125.1.25"], "tags": {"ManagedBy": "sample", "Tier": "public",
                                               "Skipped": None}})
    assert created["id"] == "subnet-new"
    body = transport.last["json"]["subnet"]
    assert transport.last["method"] == "POST"
    assert body["vpc_id"] == VPC_ID
    assert body["dhcp_enable"] is False
    assert body["primary_dns"] == "100.125.1.25"
    assert body["dnsList"] == ["100.125.1.25"]
    # 空值字段不参与创建请求，交给服务端默认
    assert "secondary_dns" not in body
    assert body["tags"] == ["ManagedBy*sample", "Tier*public"]
    assert transport.last["retries"] == 0


def test_create_subnet_caps_tags_at_the_service_limit():
    client, transport = api(ok({"subnet": {"id": "subnet-new"}}))
    client.create_subnet(VPC_ID, {
        "name": "n", "cidr": "10.1.0.0/28", "gateway_ip": "10.1.0.1",
        "availability_zone": "cn-north-4a",
        "tags": {"k%d" % index: str(index) for index in range(20)}})
    assert len(transport.last["json"]["subnet"]["tags"]) == 10


def test_create_subnet_requires_an_id_in_the_response():
    client, _transport = api(ok({"subnet": {"name": "app-public-2"}}))
    with pytest.raises(ServiceError) as error:
        client.create_subnet(VPC_ID, {"name": "app-public-2", "cidr": "10.1.0.0/28",
                                      "gateway_ip": "10.1.0.1",
                                      "availability_zone": "cn-north-4a"})
    assert "subnet.id" in error.value.message


def test_subnet_exists_reads_back_by_name():
    client, _transport = api(ok({"subnets": [{"id": "s-1", "name": "app-public-2"},
                                             {"id": "s-2", "name": "other"}]}))
    assert client.subnet_exists(VPC_ID, "app-public-2")["id"] == "s-1"
    assert client.subnet_exists(VPC_ID, "absent") is None


# ----------------------------------------------------------------------
# 路由（只读）与地址组
# ----------------------------------------------------------------------
def test_show_route_table_reads_only_and_refuses_a_missing_field():
    client, transport = api(ok({"routetable": {"id": "rt-1", "routes": []}}),
                            ok({"error_msg": "not found"}))
    assert client.show_route_table("rt-1") == {"id": "rt-1", "routes": []}
    assert transport.last["method"] == "GET"
    with pytest.raises(ServiceError):
        client.show_route_table("rt-2")


def test_address_group_read_normalizes_members_and_write_overwrites():
    client, transport = api(ok({"address_group": {"id": "ag-1", "name": "demo",
                                                  "ip_set": "10.0.0.0/16",
                                                  "max_capacity": 200}}),
                            ok({"address_group": {"id": "ag-1"}}))
    group = client.show_address_group("ag-1")
    assert group["ip_set"] == ["10.0.0.0/16"]
    assert group["max_capacity"] == 200
    client.set_address_group_members("ag-1", ["10.0.0.0/16", "10.1.0.0/16"])
    assert transport.last["method"] == "PUT"
    assert transport.last["json"] == {"address_group": {"ip_set": ["10.0.0.0/16",
                                                                   "10.1.0.0/16"]}}
    assert transport.last["retries"] == 0
    assert "/v3/%s/vpc/address-groups/ag-1" % quote(PROJECT_ID, safe="") in \
        transport.requests[0]["url"]


def test_show_vpc_error_keeps_the_target_as_structured_context():
    """缺字段的响应要带上目标对象，日志与失败通知都靠它定位。"""
    client, _transport = api(ok({"request_id": "req-1"}))
    with pytest.raises(ServiceError) as error:
        client.show_vpc(VPC_ID)
    assert error.value.fields["vpc_id"] == VPC_ID
    assert error.value.retryable is True


def test_address_group_read_rejects_an_absent_field():
    client, _transport = api(ok({"request_id": "req-1"}))
    with pytest.raises(ServiceError) as error:
        client.show_address_group("ag-1")
    assert "address_group" in error.value.message


# ----------------------------------------------------------------------
# 指标与通知
# ----------------------------------------------------------------------
def test_metric_batch_is_sent_as_an_array_of_sequences():
    samples = [{
        "namespace": "vpc_cidr_expansion.custom", "metric_name": "UtilizationPercentage",
        "dimensions": [{"name": "VpcId", "value": VPC_ID}], "value": 92.5,
        "unit": "Percent", "timestamp": 1760000000000,
    }, {
        "namespace": "vpc_cidr_expansion.custom", "metric_name": "AvailableIPs",
        "value": 12, "timestamp": 1760000000000,
    }]
    client, transport = api(ok({"metric_data": []}))
    client.report_metric_batch(samples)
    body = transport.last["json"]
    assert isinstance(body, list) and len(body) == 2
    assert body[0]["metricName"] == "UtilizationPercentage"
    assert body[0]["namespace"] == "vpc_cidr_expansion.custom"
    assert body[0]["dimensions"] == [{"name": "VpcId", "value": VPC_ID}]
    assert body[0]["value"] == 92.5 and body[0]["unit"] == "Percent"
    assert body[0]["timestamp"] == 1760000000000
    # 缺字段补齐默认口径，同一时间戳重发只会覆盖同一个点
    assert body[1]["dimensions"] == [] and body[1]["unit"] == "Count"
    assert body[1]["value"] == 12.0
    assert transport.last["url"].startswith("https://ces.%s.myhuaweicloud.com" % REGION)
    assert transport.last["retries"] == 2


def test_metric_batch_skips_an_empty_sample_set():
    client, transport = api()
    assert client.report_metric_batch([]) == {"skipped": "no samples"}
    assert transport.requests == []


def test_publish_message_encodes_the_topic_urn_and_clips_the_subject():
    client, transport = api(ok({"message_id": "mid-1"}))
    client.publish_message(TOPIC_URN, "S" * 300, "扩容完成")
    assert transport.last["method"] == "POST"
    assert quote(TOPIC_URN, safe="") in transport.last["url"]
    assert transport.last["url"].endswith("/publish")
    assert len(transport.last["json"]["subject"]) == 100
    assert transport.last["json"]["message"] == "扩容完成"
    assert transport.last["url"].startswith("https://smn.%s.myhuaweicloud.com" % REGION)


def test_publish_message_skips_when_the_topic_is_not_configured():
    client, transport = api()
    assert client.publish_message(None, "subject", "body") == \
        {"skipped": "notification topic not configured"}
    assert transport.requests == []


# ----------------------------------------------------------------------
# 失败口径
# ----------------------------------------------------------------------
def test_non_2xx_keeps_the_status_code_and_error_code_for_retry_decisions():
    client, transport = api(HttpResponse(429, {"x-request-id": "req-9"},
                                         json.dumps({"error_code": "VPC.0512",
                                                     "error_msg": "请求过多"}).encode("utf-8")))
    with pytest.raises(ServiceError) as error:
        client.show_vpc(VPC_ID)
    assert error.value.status_code == 429
    assert error.value.retryable_by_status() is True
    assert transport.last["retries"] == 2


def test_project_id_is_url_safe_in_every_path():
    client, transport = api(ok({"vpc": {"cidr": "10.0.0.0/16"}}))
    client.show_vpc("vpc with space/and=equals")
    assert "vpc%20with%20space%2Fand%3Dequals" in transport.last["url"]
