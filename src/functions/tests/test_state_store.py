"""状态存储客户端：协议报文、值映射、错误翻译与「按需建表」。

传输层由本地替身接管：这里断言的是「发出去的报文长什么样」与
「服务端各种失败被翻译成哪种语义异常」，不打开任何套接字。
替身对非 2xx 的处理与真实传输层一致（抛出带响应体的服务异常），
否则「表不存在就补建」这条分支无法在本地成立。
"""

import json

import pytest
from handler_errors import ServiceError, StateStoreError, TransportError
from helpers import PLACEHOLDER_DDB_PASSWORD, PLACEHOLDER_DDB_USER, REGION
from state_store import (ConditionNotMet, StateStore, attribute_definitions, dump_item,
                         dump_value, key_schema, load_item, load_value)

ENDPOINT = "http://10.0.40.12:8900"
LOCK_TABLE = "demo-locks"
HISTORY_TABLE = "demo-history"


class FakeResponse(object):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = dict(headers or {})

    def json(self):
        return self._payload

    def request_id(self):
        return self.headers.get("x-request-id")


class FakeTransport(object):
    """记录报文并回放响应；``errors`` 里的异常按次序抛出。"""

    def __init__(self, responses=None, errors=None):
        self.requests = []
        self._responses = list(responses or [])
        self._errors = list(errors or [])

    def request(self, method, url, headers=None, body=None, timeout=None, retries=0):
        self.requests.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "body": json.loads(body.decode("utf-8")) if body else None,
            "target": (headers or {}).get("X-Amz-Target"),
            "timeout": timeout, "retries": retries,
        })
        if self._errors:
            raise self._errors.pop(0)
        response = self._responses.pop(0) if self._responses else FakeResponse({})
        if not 200 <= response.status_code < 300:
            raise ServiceError("云服务调用失败", status_code=response.status_code,
                               body=json.dumps(response._payload), method=method, url=url)
        return response

    @property
    def targets(self):
        return [item["target"] for item in self.requests]


def store(transport=None, responses=None, errors=None):
    return StateStore(ENDPOINT, PLACEHOLDER_DDB_USER, PLACEHOLDER_DDB_PASSWORD, REGION,
                      transport=transport or FakeTransport(responses, errors))


# ----------------------------------------------------------------------
# 主键结构与值映射
# ----------------------------------------------------------------------
def test_key_schema_covers_single_key_and_composite_key():
    assert key_schema("vpc_id") == [{"AttributeName": "vpc_id", "KeyType": "HASH"}]
    assert key_schema("vpc_id", "timestamp") == [
        {"AttributeName": "vpc_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"},
    ]
    assert attribute_definitions(["vpc_id", "timestamp"]) == [
        {"AttributeName": "vpc_id", "AttributeType": "S"},
        {"AttributeName": "timestamp", "AttributeType": "S"},
    ]


@pytest.mark.parametrize("value,descriptor", [
    (None, {"NULL": True}),
    (True, {"BOOL": True}),
    (False, {"BOOL": False}),
    (7, {"N": "7"}),
    (7.5, {"N": "7.5"}),
    ("LOCKED", {"S": "LOCKED"}),
    (b"snapshot", {"S": "snapshot"}),
    (["a", 1], {"L": [{"S": "a"}, {"N": "1"}]}),
    ({"k": "v"}, {"M": {"k": {"S": "v"}}}),
])
def test_dump_value_maps_every_supported_kind(value, descriptor):
    assert dump_value(value) == descriptor


def test_dump_value_falls_back_to_the_string_form_for_unknown_kinds():
    class Weird(object):
        def __str__(self):
            return "weird"

    assert dump_value(Weird()) == {"S": "weird"}


@pytest.mark.parametrize("descriptor,expected", [
    ({"S": "LOCKED"}, "LOCKED"),
    ({"N": "1760000000"}, 1760000000),
    ({"N": "not-a-number"}, "not-a-number"),
    ({"BOOL": True}, True),
    ({"NULL": True}, None),
    ({"L": [{"S": "a"}, {"M": {"k": {"N": "2"}}}]}, ["a", {"k": 2}]),
    ({"M": {"ip_set": {"L": [{"S": "10.1.0.0/16"}]}}}, {"ip_set": ["10.1.0.0/16"]}),
    ("raw", "raw"),
    ({}, {}),
])
def test_load_value_round_trips_the_mapping(descriptor, expected):
    assert load_value(descriptor) == expected


def test_item_mapping_keeps_nested_records_readable():
    item = {"vpc_id": "vpc-1", "lock_ttl": 1760000900, "subnets_created": ["subnet-1"],
            "address_group": {"updated": True, "members": ["10.1.0.0/16"]},
            "detail": None}
    assert load_item(dump_item(item)) == item


# ----------------------------------------------------------------------
# 建表：读一次、缺就补建
# ----------------------------------------------------------------------
def test_ensure_table_returns_early_when_the_table_already_exists():
    transport = FakeTransport([FakeResponse({"Table": {"TableName": LOCK_TABLE}})])
    assert store(transport).ensure_table(LOCK_TABLE, key_schema("vpc_id")) == \
        {"table": LOCK_TABLE, "created": False}
    assert transport.targets == ["DynamoDB_20120810.DescribeTable"]


def test_ensure_table_is_cached_per_instance():
    transport = FakeTransport([FakeResponse({"Table": {}}), FakeResponse({"Table": {}})])
    client = store(transport)
    assert client.ensure_table(LOCK_TABLE, key_schema("vpc_id"))["created"] is False
    assert client.ensure_table(LOCK_TABLE, key_schema("vpc_id")) == \
        {"table": LOCK_TABLE, "created": False, "cached": True}
    assert len(transport.requests) == 1


def test_ensure_table_creates_with_declared_capacity_when_missing():
    transport = FakeTransport([
        FakeResponse({"__type": "ResourceNotFoundException"}, status_code=400),
        FakeResponse({"TableDescription": {"TableName": HISTORY_TABLE}}),
    ])
    result = store(transport).ensure_table(HISTORY_TABLE, key_schema("vpc_id", "timestamp"))
    assert result == {"table": HISTORY_TABLE, "created": True}
    assert transport.targets == ["DynamoDB_20120810.DescribeTable",
                                 "DynamoDB_20120810.CreateTable"]
    body = transport.requests[1]["body"]
    assert body["KeySchema"] == [{"AttributeName": "vpc_id", "KeyType": "HASH"},
                                 {"AttributeName": "timestamp", "KeyType": "RANGE"}]
    assert body["AttributeDefinitions"] == [
        {"AttributeName": "vpc_id", "AttributeType": "S"},
        {"AttributeName": "timestamp", "AttributeType": "S"}]
    # 兼容接口要求显式声明容量项，省略会被服务端拒绝
    assert body["ProvisionedThroughput"] == {"ReadCapacityUnits": 10, "WriteCapacityUnits": 10}


def test_ensure_table_does_not_swallow_other_describe_failures():
    transport = FakeTransport([FakeResponse({"__type": "InternalServerError"}, status_code=500)])
    with pytest.raises(StateStoreError):
        store(transport).ensure_table(LOCK_TABLE, key_schema("vpc_id"))
    assert transport.targets == ["DynamoDB_20120810.DescribeTable"]


def test_ensure_table_rejects_an_empty_endpoint():
    with pytest.raises(StateStoreError) as error:
        StateStore("", PLACEHOLDER_DDB_USER, PLACEHOLDER_DDB_PASSWORD, REGION)
    assert error.value.fields["key"] == "DDB_ENDPOINT"


# ----------------------------------------------------------------------
# 读写报文
# ----------------------------------------------------------------------
def test_get_item_asks_for_a_consistent_read_and_unwraps_the_item():
    transport = FakeTransport([FakeResponse({"Item": {
        "vpc_id": {"S": "vpc-1"}, "lock_ttl": {"N": "1760000900"},
        "tiers": {"L": [{"M": {"netmask": {"N": "28"}}}]}}})])
    row = store(transport).get_item("demo-config", {"vpc_id": "vpc-1"})
    assert transport.targets == ["DynamoDB_20120810.GetItem"]
    assert transport.requests[0]["body"] == {
        "TableName": "demo-config", "Key": {"vpc_id": {"S": "vpc-1"}}, "ConsistentRead": True}
    assert row == {"vpc_id": "vpc-1", "lock_ttl": 1760000900, "tiers": [{"netmask": 28}]}
    # 读允许重试，写不允许
    assert transport.requests[0]["retries"] == 2
    assert transport.requests[0]["timeout"] == 10


def test_get_item_returns_none_when_the_row_is_absent():
    transport = FakeTransport([FakeResponse({})])
    assert store(transport).get_item(LOCK_TABLE, {"vpc_id": "vpc-1"}) is None


def test_put_item_attaches_the_condition_expression_only_when_given():
    transport = FakeTransport()
    store(transport).put_item(
        LOCK_TABLE, {"vpc_id": "vpc-1", "status": "LOCKED", "lock_ttl": 1760000900},
        condition_expression="attribute_not_exists(vpc_id) OR #status <> :locked",
        expression_attribute_names={"#status": "status"},
        expression_attribute_values={":locked": "LOCKED", ":now": 1760000000})
    body = transport.requests[0]["body"]
    assert body["Item"] == {"vpc_id": {"S": "vpc-1"}, "status": {"S": "LOCKED"},
                            "lock_ttl": {"N": "1760000900"}}
    assert body["ConditionExpression"].startswith("attribute_not_exists(vpc_id)")
    assert body["ExpressionAttributeNames"] == {"#status": "status"}
    assert body["ExpressionAttributeValues"] == {":locked": {"S": "LOCKED"},
                                                 ":now": {"N": "1760000000"}}
    assert transport.requests[0]["retries"] == 0

    transport = FakeTransport()
    store(transport).put_item(LOCK_TABLE, {"vpc_id": "vpc-1"})
    assert "ConditionExpression" not in transport.requests[0]["body"]
    assert "ExpressionAttributeValues" not in transport.requests[0]["body"]


def test_update_item_sends_both_expressions_and_never_retries_writes():
    transport = FakeTransport()
    store(transport).update_item(
        LOCK_TABLE, {"vpc_id": "vpc-1"},
        update_expression="SET #status = :released, released_at = :now",
        condition_expression="lock_holder = :holder",
        expression_attribute_names={"#status": "status"},
        expression_attribute_values={":released": "RELEASED", ":holder": "exec-1",
                                     ":now": "2026-05-01T10:30:00Z"})
    body = transport.requests[0]["body"]
    assert body["UpdateExpression"] == "SET #status = :released, released_at = :now"
    assert body["ConditionExpression"] == "lock_holder = :holder"
    assert body["Key"] == {"vpc_id": {"S": "vpc-1"}}
    assert transport.requests[0]["retries"] == 0
    assert transport.requests[0]["method"] == "POST"


def test_every_request_is_signed_and_posted_to_the_interface_root():
    transport = FakeTransport()
    store(transport).get_item(LOCK_TABLE, {"vpc_id": "vpc-1"})
    record = transport.requests[0]
    assert record["url"] == ENDPOINT + "/"
    assert record["headers"]["Content-Type"] == "application/x-amz-json-1.0"
    assert record["headers"]["X-Amz-Target"].startswith("DynamoDB_20120810.")
    authorization = record["headers"]["Authorization"]
    assert "Credential=" in authorization and "Signature=" in authorization
    # 数据库口令只参与派生密钥，绝不出现在报文头里
    assert PLACEHOLDER_DDB_PASSWORD not in authorization


# ----------------------------------------------------------------------
# 错误翻译
# ----------------------------------------------------------------------
def test_conditional_check_failure_becomes_condition_not_met():
    transport = FakeTransport(errors=[ServiceError(
        "条件写失败", status_code=400,
        body=json.dumps({"__type": "InvalidRequestException#ConditionalCheckFailedException"}))])
    with pytest.raises(ConditionNotMet) as error:
        store(transport).put_item(LOCK_TABLE, {"vpc_id": "vpc-1"}, condition_expression="x")
    assert error.value.table == LOCK_TABLE
    assert error.value.error_code == "ConditionalCheckFailedException"
    # 条件未命中属于「有人在用」而不是链路故障，因此不重试写
    assert transport.requests[0]["retries"] == 0


def test_transport_failure_is_reported_as_an_unreachable_store():
    transport = FakeTransport(errors=[TransportError("连接超时", url=ENDPOINT)])
    with pytest.raises(StateStoreError) as error:
        store(transport).get_item(LOCK_TABLE, {"vpc_id": "vpc-1"})
    assert error.value.error_code == "transport"
    assert "不可达" in error.value.message


def test_service_failure_keeps_status_code_and_clips_a_long_body():
    transport = FakeTransport(errors=[ServiceError("服务端异常", status_code=503,
                                                   body="E" * 900)])
    with pytest.raises(StateStoreError) as error:
        store(transport).put_item(LOCK_TABLE, {"vpc_id": "vpc-1"})
    assert error.value.status_code == 503
    assert error.value.body.endswith("...")
    assert len(error.value.body) == 303


def test_missing_table_marker_recognition_covers_the_message_spelling():
    transport = FakeTransport([
        FakeResponse({"Message": "Requested resource not found: table not found"}, 400),
        FakeResponse({}),
    ])
    assert store(transport).ensure_table("demo-config", key_schema("vpc_id"))["created"] is True


def test_non_dict_response_payload_is_treated_as_empty():
    transport = FakeTransport([FakeResponse(["unexpected", "list"])])
    assert store(transport).get_item(LOCK_TABLE, {"vpc_id": "vpc-1"}) is None
