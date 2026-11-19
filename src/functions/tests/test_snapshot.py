"""巡检快照：对象键的清洗、正文的配置视图与「写不进快照不影响本轮结论」。

对象存储客户端由替身提供；内置模块缺失这条回退路径也单独断言一次。
"""

import json
import re
from datetime import datetime, timezone

import pytest
from helpers import monitor_config
from snapshot import (SNAPSHOT_PREFIX, SnapshotWriter, _safe, _split_endpoint, _status_of,
                      build_body, object_key)

SUMMARY = {
    "execution_id": "exec-demo-0001",
    "trigger": "TIMER",
    "monitored_count": 2,
    "observed": 2,
    "observe_failed": 0,
    "decision_counts": {"above_threshold": 1, "below_threshold": 1},
    "threshold": 85.0,
    "dispatches": [{"vpc_id": "vpc-example-0001", "dispatched": True, "detail": None}],
}


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


def writer(**overrides):
    config = monitor_config(OBS_BUCKET="demo-bucket",
                            OBS_ENDPOINT="https://obs.example.com", **overrides)
    return SnapshotWriter(config)


class FakeCredentialsStub(object):
    access_key = "<HW_ACCESS_KEY>"
    secret_key = "<HW_SECRET_KEY>"
    security_token = "<HW_SECURITY_TOKEN>"
    project_id = "<HW_PROJECT_ID>"
    source = "agency-context"


# ----------------------------------------------------------------------
# 对象键
# ----------------------------------------------------------------------
def test_key_is_a_daily_directory_with_the_execution_id():
    moment = datetime(2026, 5, 1, 10, 30, 0, tzinfo=timezone.utc)
    assert object_key("exec-demo-0001", moment) == \
        "%s/2026-05-01/exec-demo-0001.json" % SNAPSHOT_PREFIX


def test_key_sanitizes_characters_that_would_change_the_path():
    assert object_key("a/b c:d*e", datetime(2026, 5, 1, tzinfo=timezone.utc)) == \
        "%s/2026-05-01/a-b-c-d-e.json" % SNAPSHOT_PREFIX


def test_key_falls_back_to_a_timestamp_when_the_id_is_missing():
    assert re.match(r"^\d{8}T\d{6}Z$", _safe(None))
    assert re.match(r"^\d{8}T\d{6}Z$", _safe(""))


def test_key_length_is_bounded():
    assert len(_safe("x" * 400)) == 120


@pytest.mark.parametrize("endpoint,expected", [
    ("https://obs.example.com", (True, "obs.example.com")),
    ("https://obs.example.com/", (True, "obs.example.com")),
    ("http://obs.example.com", (False, "obs.example.com")),
    ("obs.example.com", (True, "obs.example.com")),
])
def test_endpoint_scheme_is_restored_for_the_client(endpoint, expected):
    assert _split_endpoint(endpoint) == expected


@pytest.mark.parametrize("response,expected", [
    (type("R", (), {"status": 200})(), 200),
    (type("R", (), {"http_status": 404})(), 404),
    (type("R", (), {"common": {"status_code": 503}})(), 503),
    (type("R", (), {"common": "not-a-dict"})(), None),
    ("anything", None),
])
def test_status_reads_the_first_available_shape(response, expected):
    assert _status_of(response) == expected


# ----------------------------------------------------------------------
# 正文
# ----------------------------------------------------------------------
def test_body_merges_the_round_summary_with_a_credential_free_config_view():
    body = json.loads(build_body(SUMMARY, monitor_config()))
    assert body["monitored_count"] == 2
    assert body["decision_counts"]["above_threshold"] == 1
    assert body["runtime_config"]["tag_filter"] == "cidr-expansion=managed"
    assert body["runtime_config"]["metric_namespace"] == "vpc_cidr_expansion.custom"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", body["generated_at"])
    assert "\\u" not in build_body(SUMMARY, monitor_config())
    for forbidden in ("ddb_password", "ddb_username", "access_key", "secret_key"):
        assert forbidden not in body["runtime_config"]


def test_body_does_not_mutate_the_summary_it_rendered():
    summary = dict(SUMMARY)
    json.loads(build_body(summary, monitor_config()))
    assert "runtime_config" not in summary and "generated_at" not in summary


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------
def test_snapshot_write_uses_the_temporary_credentials_of_this_invocation():
    seen = {}

    class ObsClientStub(object):
        def __init__(self, **kwargs):
            seen.update(kwargs)

    import sys

    sys.modules["obs"] = type("obs", (), {"ObsClient": ObsClientStub})
    try:
        assert writer()._client(FakeCredentialsStub(), "http://obs.example.com") is not None
    finally:
        del sys.modules["obs"]
    assert seen == {"access_key_id": "<HW_ACCESS_KEY>", "secret_access_key": "<HW_SECRET_KEY>",
                    "security_token": "<HW_SECURITY_TOKEN>", "is_secure": False,
                    "server": "obs.example.com"}


def test_a_round_is_written_as_one_object_per_execution():
    client = FakeObjectClient(200)
    subject = writer()
    subject._client = lambda credentials, endpoint: client
    outcome = subject.write(SUMMARY, FakeCredentialsStub())
    assert outcome["written"] is True and outcome["bucket"] == "demo-bucket"
    assert outcome["key"].startswith(SNAPSHOT_PREFIX + "/")
    assert outcome["key"].endswith("/exec-demo-0001.json")
    assert len(outcome["key"].split("/")) == 3
    call = client.calls[0]
    assert call["bucket"] == "demo-bucket"
    assert json.loads(call["data"].decode("utf-8"))["execution_id"] == "exec-demo-0001"
    assert call["metadata"]["Content-Type"] == "application/json; charset=utf-8"
    assert client.closed == 1


def test_execution_id_can_be_overridden_by_the_caller():
    client = FakeObjectClient(200)
    subject = writer()
    subject._client = lambda credentials, endpoint: client
    outcome = subject.write(SUMMARY, FakeCredentialsStub(), execution_id="exec-manual")
    assert outcome["key"].endswith("exec-manual.json")


def test_bucket_or_endpoint_absent_skips_without_any_client():
    subject = SnapshotWriter(monitor_config())
    assert subject.write(SUMMARY, FakeCredentialsStub()) == \
        {"written": False, "detail": "未配置 OBS_BUCKET / OBS_ENDPOINT"}


def test_a_non_2xx_is_not_reported_as_written():
    client = FakeObjectClient(500)
    subject = writer()
    subject._client = lambda credentials, endpoint: client
    outcome = subject.write(SUMMARY, FakeCredentialsStub())
    assert outcome["written"] is False
    assert outcome["detail"] == "HTTP 500"
    assert client.closed == 1


def test_a_client_failure_only_costs_the_snapshot():
    client = FakeObjectClient(error=RuntimeError("内置模块报错"))
    subject = writer()
    subject._client = lambda credentials, endpoint: client
    outcome = subject.write(SUMMARY, FakeCredentialsStub())
    assert outcome["written"] is False and "内置模块报错" in outcome["detail"]


def test_missing_object_library_degrades_to_a_log_line():
    subject = writer()

    def unavailable(credentials, endpoint):
        raise ImportError("No module named 'obs'")

    subject._client = unavailable
    assert subject.write(SUMMARY, FakeCredentialsStub())["detail"] == "运行时缺少 obs 模块"


def test_close_failure_is_ignored():
    client = FakeObjectClient(200)

    def explode():
        raise RuntimeError("关闭失败")

    client.close = explode
    subject = writer()
    subject._client = lambda credentials, endpoint: client
    assert subject.write(SUMMARY, FakeCredentialsStub())["written"] is True
