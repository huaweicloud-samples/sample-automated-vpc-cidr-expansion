"""留痕：历史表行、历史对象键与「写不进台账也不回滚资源」的口径。

对象存储客户端由替身提供：本地既不联网也不依赖运行时内置模块，
但两种失败（内置模块缺失、写入报错）都要落到「只记日志」这条路上。
"""

import json
import re

import pytest
from handler_errors import StateStoreError
from helpers import VPC_ID, FakeStore, expansion_config, subnet, vpc_request
from history_writer import (HISTORY_KEY_SCHEMA, HistoryWriter, _split_endpoint, _status_of,
                            build_record, object_key, snapshot)
from runtime_conf import Tier

HISTORY_TABLE = "demo-history"


class FakeObjectClient(object):
    def __init__(self, status_code=200, error=None):
        self.calls = []
        self.closed = 0
        self._status_code = status_code
        self._error = error

    def putObject(self, bucketName=None, objectKey=None, data=None, metadata=None):
        self.calls.append({"bucket": bucketName, "key": objectKey, "data": data,
                           "metadata": metadata})
        if self._error is not None:
            raise self._error
        return type("Response", (), {"status": self._status_code})()

    def close(self):
        self.closed += 1


def result(**overrides):
    base = {
        "vpc_id": VPC_ID, "execution_id": "exec-demo-0001", "status": "SUCCESS",
        "utilization": 92.0, "mode": "secondary_cidr", "new_cidr": "10.1.0.0/16",
        "secondary_cidr_added": True, "netmask": 28, "cidr_count_after": 2,
        "subnets_created": [subnet("10.1.0.0/28", "app-public-2", available=12),
                            subnet("10.1.0.16/28", "app-public-3", available=12)],
        "subnets_failed": [], "skipped_plan": ["10.2.0.0/16"],
        "address_group": {"updated": True, "members": ["10.1.0.0/16"]},
        "route_check": {"checked": True, "covered": True},
        "state_before": {"total_capacity": 65534}, "state_after": {"total_capacity": 131070},
        "elapsed_seconds": 7, "function": "vpc-cidr-expansion",
    }
    base.update(overrides)
    return base


def plan():
    config = expansion_config()
    return {"tiers": config.tier_plan, "warning_threshold": config.warning_threshold,
            "source": "environment"}


def record(**overrides):
    values = build_record(result(), vpc_request(), plan(), expansion_config(),
                          "2026-05-01T10:30:00Z")
    values.update(overrides)
    return values


# ----------------------------------------------------------------------
# 表行
# ----------------------------------------------------------------------
def test_history_key_is_a_composite_of_vpc_and_timestamp():
    assert HISTORY_KEY_SCHEMA == [
        {"AttributeName": "vpc_id", "KeyType": "HASH"},
        {"AttributeName": "timestamp", "KeyType": "RANGE"}]


def test_record_carries_the_whole_scene_as_explicit_fields():
    row = record()
    assert row["timestamp"] == "2026-05-01T10:30:00Z"
    assert row["status"] == "SUCCESS" and row["mode"] == "secondary_cidr"
    assert row["new_cidr"] == "10.1.0.0/16"
    assert row["secondary_cidr_added"] is True
    # 子网既留 ID（排障按 ID 查）也留完整视图（通知按它列举）
    assert row["subnets_created"] == ["subnet-app-public-2", "subnet-app-public-3"]
    assert [item["cidr"] for item in row["subnet_details"]] == ["10.1.0.0/28", "10.1.0.16/28"]
    assert row["skipped_plan"] == ["10.2.0.0/16"]
    assert row["trigger_utilization"] == 92.0
    assert row["plan_source"] == "environment"
    assert row["warning_threshold"] == 80.0
    assert [item["tier_name"] for item in row["tiers"]] == ["public", "private"]
    assert row["state_after"]["total_capacity"] == 131070
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", row["recorded_at"])


def test_record_falls_back_to_the_request_for_identity_fields():
    row = build_record({}, vpc_request(vpc_id="vpc-other", execution_id="exec-9"), plan(),
                       expansion_config(), None)
    assert row["vpc_id"] == "vpc-other"
    assert row["execution_id"] == "exec-9"
    assert row["secondary_cidr_added"] is False
    assert row["subnets_created"] == [] and row["subnets_failed"] == []
    assert row["timestamp"].endswith("Z")


def test_record_of_a_skipped_run_keeps_the_reason():
    row = build_record(result(status="SKIPPED_BELOW_THRESHOLD", mode="not_required",
                              new_cidr=None, secondary_cidr_added=None),
                       vpc_request(utilization=50.0), plan(), expansion_config(),
                       "2026-05-01T10:30:00Z")
    assert row["status"] == "SKIPPED_BELOW_THRESHOLD"
    assert row["secondary_cidr_added"] is False
    assert row["reason"] == "utilization"
    assert row["trigger_utilization"] == 50.0


# ----------------------------------------------------------------------
# 对象键与正文
# ----------------------------------------------------------------------
def test_object_key_is_prefixed_by_configuration_and_sanitizes_the_stamp():
    config = expansion_config(HISTORY_OBJECT_PREFIX="/expansion-history/")
    key = object_key(record(), config)
    assert key == "expansion-history/%s/2026-05-01T10-30-00Z-exec-demo-0001.json" % VPC_ID


def test_object_key_falls_back_to_the_default_prefix():
    config = expansion_config(HISTORY_OBJECT_PREFIX=False)
    assert object_key(record(vpc_id="vpc-2"), config).startswith("expansion-history/vpc-2/")


def test_snapshot_body_is_readable_and_free_of_credentials():
    body = json.loads(snapshot(record(), expansion_config()))
    assert body["new_cidr"] == "10.1.0.0/16"
    assert body["runtime_config"]["warning_threshold"] == 80.0
    assert body["runtime_config"]["tables"] == ["demo-locks", "demo-config", "demo-history"]
    # 中文正文不转义，运维直接能在桶里读
    assert "\\u" not in snapshot(record(), expansion_config())
    for forbidden in ("ddb_password", "ddb_username", "access_key", "secret_key",
                      "security_token"):
        assert forbidden not in body["runtime_config"]


def test_snapshot_without_config_is_just_the_row():
    body = json.loads(snapshot(record()))
    assert "runtime_config" not in body
    assert body["vpc_id"] == VPC_ID


@pytest.mark.parametrize("endpoint,expected", [
    ("https://obs.example.com", (True, "obs.example.com")),
    ("https://obs.example.com/", (True, "obs.example.com")),
    ("http://obs.example.com", (False, "obs.example.com")),
    ("  obs.example.com  ", (True, "obs.example.com")),
])
def test_endpoint_is_split_into_scheme_and_host(endpoint, expected):
    assert _split_endpoint(endpoint) == expected


@pytest.mark.parametrize("response,expected", [
    (type("R", (), {"status": 201})(), 201),
    (type("R", (), {"http_status": 204})(), 204),
    (type("R", (), {"common": {"status_code": 500}})(), 500),
    (type("R", (), {})(), None),
    (None, None),
])
def test_status_is_read_from_whichever_shape_the_client_returns(response, expected):
    assert _status_of(response) == expected


# ----------------------------------------------------------------------
# 写入：表与对象
# ----------------------------------------------------------------------
def test_table_row_written_once_with_the_composite_key():
    store = FakeStore(tables=[HISTORY_TABLE])
    writer = HistoryWriter(store, HISTORY_TABLE, expansion_config())
    assert writer.write(record()) is True
    assert store.puts[-1]["table"] == HISTORY_TABLE
    assert store.puts[-1]["item"]["timestamp"] == "2026-05-01T10:30:00Z"
    assert store.rows[HISTORY_TABLE]["%s|2026-05-01T10:30:00Z" % VPC_ID]["new_cidr"] == \
        "10.1.0.0/16"


def test_a_failing_table_write_is_reported_but_not_raised():
    """资源事实已经改变，台账写不进去不能把整次执行判成失败。"""
    store = FakeStore(tables=[HISTORY_TABLE],
                      raise_on={"put_item": StateStoreError("写不进去了")})
    writer = HistoryWriter(store, HISTORY_TABLE, expansion_config())
    assert writer.write(record()) is False


def test_ensure_reports_a_created_table_and_swallows_ddl_failures():
    store = FakeStore()
    writer = HistoryWriter(store, HISTORY_TABLE, expansion_config())
    assert writer.ensure() == {"table": HISTORY_TABLE, "created": True}

    class BrokenStore(FakeStore):
        def ensure_table(self, table_name, key_schema):
            raise StateStoreError("建表被拒", error_code="LimitExceeded")

    outcome = HistoryWriter(BrokenStore(), HISTORY_TABLE, expansion_config()).ensure()
    assert outcome["created"] is False
    assert "建表被拒" in outcome["error"]


def test_object_write_sends_the_body_and_closes_the_client():
    client = FakeObjectClient(status_code=200)
    config = expansion_config(OBS_BUCKET="demo-bucket", OBS_ENDPOINT="https://obs.example.com")
    writer = HistoryWriter(FakeStore(tables=[HISTORY_TABLE]), HISTORY_TABLE, config)
    writer._obs_client = lambda credentials, endpoint: client
    outcome = writer.write_object(record(), FakeCredentialsForTest())
    assert outcome["written"] is True and outcome["bucket"] == "demo-bucket"
    assert outcome["key"].startswith("expansion-history/%s/" % VPC_ID)
    call = client.calls[0]
    assert call["bucket"] == "demo-bucket" and call["key"] == outcome["key"]
    assert json.loads(call["data"].decode("utf-8"))["new_cidr"] == "10.1.0.0/16"
    assert call["metadata"]["Content-Type"] == "application/json; charset=utf-8"
    assert client.closed == 1


def test_object_write_without_a_bucket_is_reported_as_skipped():
    writer = HistoryWriter(FakeStore(tables=[HISTORY_TABLE]), HISTORY_TABLE,
                           expansion_config())
    assert writer.write_object(record(), FakeCredentialsForTest()) == \
        {"written": False, "key": None, "detail": "未配置 OBS_BUCKET / OBS_ENDPOINT"}


def test_a_non_success_status_is_not_reported_as_written():
    client = FakeObjectClient(status_code=500)
    config = expansion_config(OBS_BUCKET="demo-bucket", OBS_ENDPOINT="obs.example.com")
    writer = HistoryWriter(FakeStore(), HISTORY_TABLE, config)
    writer._obs_client = lambda credentials, endpoint: client
    outcome = writer.write_object(record(), FakeCredentialsForTest())
    assert outcome["written"] is False
    assert outcome["detail"] == "HTTP 500"
    assert client.closed == 1


def test_a_put_failure_keeps_the_key_for_follow_up():
    client = FakeObjectClient(error=RuntimeError("内置模块报错"))
    config = expansion_config(OBS_BUCKET="demo-bucket", OBS_ENDPOINT="https://obs.example.com")
    writer = HistoryWriter(FakeStore(), HISTORY_TABLE, config)
    writer._obs_client = lambda credentials, endpoint: client
    outcome = writer.write_object(record(), FakeCredentialsForTest())
    assert outcome["written"] is False
    assert "内置模块报错" in outcome["detail"]
    assert outcome["key"].endswith(".json")


def test_a_missing_object_library_only_costs_the_object():
    config = expansion_config(OBS_BUCKET="demo-bucket", OBS_ENDPOINT="https://obs.example.com")
    writer = HistoryWriter(FakeStore(), HISTORY_TABLE, config)

    def unavailable(credentials, endpoint):
        raise ImportError("No module named 'obs'")

    writer._obs_client = unavailable
    assert writer.write_object(record(), FakeCredentialsForTest()) == \
        {"written": False, "key": object_key(record(), config), "detail": "运行时缺少 obs 模块"}


def test_client_construction_failure_is_reported_without_raising():
    config = expansion_config(OBS_BUCKET="demo-bucket", OBS_ENDPOINT="https://obs.example.com")
    writer = HistoryWriter(FakeStore(), HISTORY_TABLE, config)

    def unavailable(credentials, endpoint):
        raise ValueError("endpoint 不合法")

    writer._obs_client = unavailable
    outcome = writer.write_object(record(), FakeCredentialsForTest())
    assert outcome["written"] is False and "不合法" in outcome["detail"]


class FakeCredentialsForTest(object):
    access_key = "<HW_ACCESS_KEY>"
    secret_key = "<HW_SECRET_KEY>"
    security_token = "<HW_SECURITY_TOKEN>"
    project_id = "<HW_PROJECT_ID>"
    source = "agency-context"


def test_client_built_from_this_invocations_temporary_credentials_only():
    """客户端用的是本次调用的临时三元组：这里只断言构造入参不含长期密钥。"""
    import sys

    seen = {}

    class ObsClientStub(object):
        def __init__(self, access_key_id=None, secret_access_key=None, security_token=None,
                     is_secure=None, server=None):
            seen.update({"access_key_id": access_key_id, "secret_access_key": secret_access_key,
                         "security_token": security_token, "is_secure": is_secure,
                         "server": server})

    sys.modules["obs"] = type("obs", (), {"ObsClient": ObsClientStub})
    writer = HistoryWriter(FakeStore(), HISTORY_TABLE,
                           expansion_config(OBS_BUCKET="demo-bucket",
                                            OBS_ENDPOINT="http://obs.example.com"))
    try:
        assert writer._obs_client(FakeCredentialsForTest(), "http://obs.example.com") is not None
    finally:
        del sys.modules["obs"]
    assert seen == {"access_key_id": "<HW_ACCESS_KEY>", "secret_access_key": "<HW_SECRET_KEY>",
                    "security_token": "<HW_SECURITY_TOKEN>", "is_secure": False,
                    "server": "obs.example.com"}


def test_tier_view_in_the_record_is_plain_json_friendly():
    tiers = [Tier("public", "app-public-1", ["cn-north-4a"], 28, "sg-demo")]
    row = build_record(result(), vpc_request(), {"tiers": tiers, "warning_threshold": 80.0,
                                                 "source": "environment"},
                       expansion_config(), "2026-05-01T10:30:00Z")
    assert row["tiers"] == [{"tier_name": "public", "sample_name": "app-public-1",
                             "availability_zones": ["cn-north-4a"], "netmask": 28,
                             "security_group_id": "sg-demo"}]
    assert json.dumps(row, default=str)


def test_history_table_name_comes_from_the_deployed_configuration():
    store = FakeStore(tables=["other-history"])
    config = expansion_config(HISTORY_TABLE_NAME="other-history")
    writer = HistoryWriter(store, config.history_table, config)
    assert writer.write(record()) is True
    assert store.puts[-1]["table"] == "other-history"
    assert store.calls[0] == ("put_item", "other-history", "%s|2026-05-01T10:30:00Z" % VPC_ID)


def test_snapshot_uses_the_shared_row_without_mutating_it():
    row = record()
    before = dict(row)
    json.loads(snapshot(row, expansion_config()))
    assert row == before
