"""异常契约：分类、可重试标记与上下文字段的落地方式。

入口把「一行日志 + 一条失败通知 + 一个可重试的失败结果」都建立在
:class:`ExpansionError` 的上下文字段上，因此这两件事必须可断言：
哪些错误值得让平台重投一次，以及排障时要拿到的字段真的在异常上。
"""

import json

import pytest
from expansion_flow import _is_conflict
from handler_errors import (AssociationTimeout, ConfigError, ExpansionError, LockUnavailable,
                            PartialExpansion, PlanExhausted, ServiceError, StateStoreError,
                            TableNotFound, TransportError)
from state_store import ConditionNotMet

RETRYABLE = (ServiceError, TransportError, StateStoreError, TableNotFound, AssociationTimeout)
NON_RETRYABLE = (ConfigError, LockUnavailable, PlanExhausted, PartialExpansion)


@pytest.mark.parametrize("error_class", RETRYABLE)
def test_classes_worth_a_replatform_retry(error_class):
    assert error_class.retryable is True


@pytest.mark.parametrize("error_class", NON_RETRYABLE)
def test_classes_that_retries_cannot_fix(error_class):
    assert error_class.retryable is False


def test_context_fields_are_rendered_for_logging_and_notification():
    error = PlanExhausted("规划已用尽", vpc_id="vpc-1", cidr_count=5, max_count=5,
                          plan=["10.1.0.0/16"])
    assert error.as_fields() == {
        "error": "PlanExhausted", "reason": "规划已用尽", "vpc_id": "vpc-1",
        "cidr_count": 5, "max_count": 5, "plan": ["10.1.0.0/16"]}
    rendered = str(error)
    # 上下文字段按字典序展开：同一条故障每次产生的日志行完全一致
    assert rendered.index("cidr_count=") < rendered.index("max_count=") < \
        rendered.index("plan=") < rendered.index("vpc_id=")


def test_service_error_keeps_the_response_body_reachable_for_semantic_decisions():
    """冲突/条件未命中这类判定读的是响应体，它必须挂在异常上而不只在文本里。"""
    body = json.dumps({"__type": "Conflict#VPC.0512", "error_msg": "网段已存在"})
    error = ServiceError("调用失败", status_code=409, request_id="req-1", body=body)
    assert error.body == body
    assert error.fields["body"] == body
    assert error.status_code == 409 and error.request_id == "req-1"


def test_state_store_error_accepts_extra_context_without_losing_the_code():
    error = StateStoreError("状态存储地址为空", key="DDB_ENDPOINT", error_code="transport")
    assert error.fields["key"] == "DDB_ENDPOINT"
    assert error.error_code == "transport"
    assert error.as_fields()["status_code"] is None


def test_condition_not_met_carries_both_the_table_and_the_protocol_code():
    error = ConditionNotMet("条件写未命中", table="demo-locks")
    assert isinstance(error, StateStoreError)
    assert error.table == "demo-locks"
    assert error.error_code == "ConditionalCheckFailedException"


@pytest.mark.parametrize("status_code,expected", [
    (None, True), (429, True), (500, True), (502, True), (503, True), (504, True),
    (400, False), (403, False), (404, False), (409, False), (501, False)])
def test_retry_by_status_follows_the_declared_set(status_code, expected):
    assert ServiceError("x", status_code=status_code).retryable_by_status() is expected


def test_partial_expansion_carries_both_lists_for_the_failure_notice():
    failed = [{"name": "app-public-2", "attempts": 2}]
    created = [{"id": "subnet-1"}]
    error = PartialExpansion("层级未铺开", failed=failed, created=created)
    assert error.failed == failed and error.created == created
    assert error.as_fields()["failed"] == failed
    # 缺失的参数落到空列表而不是 None，通知模板可以直接遍历
    blank = PartialExpansion("层级未铺开")
    assert blank.failed == [] and blank.created == []


@pytest.mark.parametrize("marker", ["409", "Conflict", "already", "exist", "duplicate"])
def test_conflict_markers_are_read_from_every_part_of_the_error(marker):
    error = ServiceError("服务端拒绝", status_code=400, error_code=None,
                         body=json.dumps({"error_msg": marker}))
    assert _is_conflict(error) is True


def test_a_plain_server_error_is_not_read_as_a_conflict():
    assert _is_conflict(ServiceError("配额不足", status_code=400, body="quota")) is False


def test_transport_error_keeps_the_url_it_failed_on():
    error = TransportError("请求未得到响应", url="https://vpc.demo/x", detail="timeout")
    assert error.fields == {"url": "https://vpc.demo/x", "detail": "timeout"}
    assert isinstance(error, ExpansionError)
