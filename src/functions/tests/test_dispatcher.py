"""指令投递：判定分支、静默窗口，以及「运行期让权字段不被回写」的守护。"""

import time

import pytest
from dispatcher import (COMMAND_REASON, DISPATCH_COOLDOWN_SECONDS, CommandDispatcher,
                        Decision, OWNED_FIELDS, decide, seconds_since)
from helpers import VPC_ID, FakeApi, FakeStore, monitor_config

#: 这三个字段属于运维按 VPC 覆盖规划的载体，巡检函数永远不得改写
OPERATIONS_OWNED = ("warning_threshold", "secondary_cidrs", "tiers")


def observation(**overrides):
    item = {"vpc_id": VPC_ID, "vpc_name": "demo-vpc", "utilization": 92.0,
            "total_capacity": 506, "available_ips": 40, "subnet_count": 2,
            "cidr_count": 1, "next_cidr": "10.1.0.0/16", "plan_exhausted": False}
    item.update(overrides)
    return item


def build_dispatcher(store=None, api=None, config=None):
    return CommandDispatcher(api or FakeApi(), store or FakeStore(),
                             config or monitor_config(), logger=None)


# ----------------------------------------------------------------------
# 判定分支
# ----------------------------------------------------------------------
def test_dispatch_when_above_threshold_and_no_recent_dispatch():
    decision = decide(observation(), monitor_config(), seconds_since_dispatch=None)
    assert decision.should_dispatch is True
    assert decision.reason == "above_threshold"


def test_threshold_boundary_is_inclusive():
    config = monitor_config()
    assert config.utilization_threshold == 85.0
    assert decide(observation(utilization=85.0), config, None).should_dispatch is True
    assert decide(observation(utilization=84.99), config, None).should_dispatch is False


def test_observe_failure_does_not_dispatch():
    decision = decide(observation(error="读取超时", utilization=None), monitor_config(), None)
    assert decision.should_dispatch is False
    assert decision.reason == "observe_failed"


def test_unknown_utilization_does_not_dispatch():
    """余量读不到时宁可不扩也不误扩。"""
    decision = decide(observation(utilization=None), monitor_config(), None)
    assert decision.should_dispatch is False
    assert decision.reason == "utilization_unknown"


def test_below_threshold_does_not_dispatch():
    decision = decide(observation(utilization=30.0), monitor_config(), None)
    assert decision.should_dispatch is False
    assert decision.reason == "below_threshold"


def test_exhausted_plan_does_not_dispatch():
    """规划已用尽时不投：投了也只会在扩容侧变成一次失败通知。"""
    decision = decide(observation(plan_exhausted=True, next_cidr=None), monitor_config(), None)
    assert decision.should_dispatch is False
    assert decision.reason == "plan_exhausted"


def test_within_cooldown_does_not_dispatch():
    decision = decide(observation(), monitor_config(), DISPATCH_COOLDOWN_SECONDS - 1)
    assert decision.should_dispatch is False
    assert decision.reason == "within_cooldown"
    # 窗口过后恢复可投
    assert decide(observation(), monitor_config(), DISPATCH_COOLDOWN_SECONDS).should_dispatch


def test_cooldown_window_covers_one_full_expansion():
    """静默窗口必须显著大于扩容函数单次超时上限（900 秒），否则扩容未完就重投。"""
    assert DISPATCH_COOLDOWN_SECONDS >= 1800
    assert DISPATCH_COOLDOWN_SECONDS > 900


def test_decision_order_observation_beats_cooldown():
    """判定顺序固定：观测失败优先于静默窗口。"""
    decision = decide(observation(error="读取失败"), monitor_config(), 0)
    assert decision.reason == "observe_failed"


def test_decision_to_dict_carries_the_verdict():
    payload = Decision(observation(), True, "above_threshold").to_dict()
    assert payload == {"vpc_id": VPC_ID, "vpc_name": "demo-vpc", "utilization": 92.0,
                       "next_cidr": "10.1.0.0/16", "dispatched": True,
                       "reason": "above_threshold"}


# ----------------------------------------------------------------------
# 投递
# ----------------------------------------------------------------------
def test_dispatch_publishes_minimal_command():
    api = FakeApi()
    dispatcher = build_dispatcher(api=api)
    result = dispatcher.dispatch(observation(), execution_id="exec-1")
    assert result["dispatched"] is True
    assert len(api.published) == 1
    payload = result["payload"]
    assert payload["vpc_id"] == VPC_ID
    assert payload["reason"] == COMMAND_REASON
    assert payload["utilization"] == 92.0
    assert payload["suggested_cidr"] == "10.1.0.0/16"
    assert payload["request_id"] == "exec-1"


def test_dispatch_without_topic_is_reported_not_raised():
    api = FakeApi()
    dispatcher = build_dispatcher(api=api, config=monitor_config(COMMAND_TOPIC_URN=False))
    result = dispatcher.dispatch(observation())
    assert result["dispatched"] is False
    assert api.published == []


def test_dispatch_swallow_publish_failure():
    from handler_errors import ServiceError

    api = FakeApi(failures={"publish_message": [ServiceError("主题不可用", status_code=503)]})
    result = build_dispatcher(api=api).dispatch(observation())
    assert result["dispatched"] is False
    assert result["vpc_id"] == VPC_ID


def test_record_dispatch_writes_timestamp_and_counter():
    store = FakeStore(tables=["demo-config"])
    dispatcher = build_dispatcher(store=store)
    result = dispatcher.record_dispatch(observation(), "exec-2")
    assert result["written"] is True
    expression = store.updates[-1]["expression"]
    assert "last_dispatch_at = :at" in expression
    assert "if_not_exists(dispatch_count, :zero) + :one" in expression
    assert store.updates[-1]["values"][":exec"] == "exec-2"


def test_record_dispatch_failure_does_not_retract_the_command():
    """已投出去的指令不因记账失败撤回：下一轮靠扩容侧的 VPC 级锁兜底。"""
    from handler_errors import StateStoreError

    store = FakeStore(tables=["demo-config"], raise_on={"update_item": StateStoreError("表不可达")})
    result = build_dispatcher(store=store).record_dispatch(observation())
    assert result["written"] is False


# ----------------------------------------------------------------------
# 让权守护：巡检只写自己维护的观测字段
# ----------------------------------------------------------------------
def test_record_observation_only_touches_owned_fields():
    store = FakeStore(tables=["demo-config"])
    result = build_dispatcher(store=store).record_observation(observation())
    assert result["written"] is True
    expression = store.updates[-1]["expression"]
    assert expression.startswith("SET ")
    assigned = sorted(name.strip().split(" ")[0] for name in expression[4:].split(","))
    assert assigned == sorted(OWNED_FIELDS_WITHOUT_TIMESTAMP)
    for field in OPERATIONS_OWNED:
        assert field not in assigned, "%s 属于运维维护的按 VPC 覆盖规划" % field


#: 除 ``last_inspected_at`` 外，本轮写入的字段集合就是 OWNED_FIELDS 去掉投递侧字段
OWNED_FIELDS_WITHOUT_TIMESTAMP = [
    "available_ips", "cidr_count", "last_inspected_at", "monitored", "next_cidr",
    "plan_exhausted", "subnet_count", "total_capacity", "utilization",
]


def test_owned_field_list_excludes_operations_fields():
    """OWNED_FIELDS 与运维字段两份清单必须互不相交。"""
    assert set(OWNED_FIELDS).isdisjoint(OPERATIONS_OWNED)
    for field in ("last_dispatch_at", "last_dispatch_execution", "dispatch_count"):
        assert field in OWNED_FIELDS


def test_record_observation_guard_blocks_unowned_field(monkeypatch):
    """守护分支：写集合里只要冒出非自有字段就拒绝整次写入，一个字段都不落。

    生产代码的写集合固定由 :data:`OWNED_FIELDS` 约束，这里把白名单临时收窄，
    让守护必然触发，验证「越界即整笔拒绝」而不是「越界的照写」。
    """
    import dispatcher as dispatcher_module

    store = FakeStore(tables=["demo-config"])
    monkeypatch.setattr(dispatcher_module, "OWNED_FIELDS",
                        ("utilization", "total_capacity"))
    result = dispatcher_module.CommandDispatcher(
        FakeApi(), store, monitor_config(), None).record_observation(observation())
    assert result["written"] is False
    assert "secondary_cidrs" not in str(result)
    assert set(result["fields"]) & set(OPERATIONS_OWNED) == set()
    assert store.updates == []
    assert store.puts == []


def test_record_observation_requires_vpc_id():
    store = FakeStore(tables=["demo-config"])
    result = build_dispatcher(store=store).record_observation({"vpc_id": None})
    assert result["written"] is False and result["reason"] == "missing vpc_id"


def test_record_observation_without_table_is_noop():
    result = build_dispatcher(config=monitor_config(CONFIG_TABLE_NAME=False)
                              ).record_observation(observation())
    assert result["written"] is False


def test_record_observation_survives_store_failure():
    """写不回观测值不影响指标与指令，本轮判定照常完成。"""
    from handler_errors import StateStoreError

    store = FakeStore(tables=["demo-config"], raise_on={"update_item": StateStoreError("不可达")})
    result = build_dispatcher(store=store).record_observation(observation())
    assert result["written"] is False


def test_ensure_table_creates_config_table_once():
    store = FakeStore()
    result = build_dispatcher(store=store).ensure_table()
    assert result["created"] is True
    assert ("ensure_table", "demo-config") in store.calls


def test_ensure_table_failure_is_downgraded_to_a_warning():
    from handler_errors import StateStoreError

    store = FakeStore(raise_on={"ensure_table": StateStoreError("实例不可达")})
    result = build_dispatcher(store=store).ensure_table()
    assert result["created"] is False


# ----------------------------------------------------------------------
# 节流时间戳解析
# ----------------------------------------------------------------------
def test_seconds_since_handles_epoch_and_iso_forms():
    from datetime import datetime, timezone

    now = int(time.time())
    assert seconds_since(now - 60, now) == pytest.approx(60.0)
    assert seconds_since(str(now - 120), now) == pytest.approx(120.0)
    stamp = datetime(2026, 5, 1, 10, 30, tzinfo=timezone.utc).timestamp()
    assert seconds_since("2026-05-01T10:30:00Z", stamp + 3600) == pytest.approx(3600.0)
    assert seconds_since(None, now) is None
    assert seconds_since("", now) is None
    assert seconds_since("not-a-time", now) is None
    # 未来的时间戳按 0 处理，不能让窗口倒查
    assert seconds_since(now + 9999, now) == 0.0


def test_seconds_since_dispatch_reads_the_row():
    now = int(time.time())
    store = FakeStore(rows={"demo-config": {VPC_ID: {"vpc_id": VPC_ID,
                                                     "last_dispatch_at": now - 10}}})
    dispatcher = build_dispatcher(store=store)
    assert dispatcher.seconds_since_dispatch(VPC_ID) == pytest.approx(10.0, abs=5)
    # 从未投递过：返回 None，视为「不在窗口内」
    assert dispatcher.seconds_since_dispatch("vpc-never") is None


def test_unreadable_row_is_treated_as_never_dispatched():
    """脏数据宁可宽松也不卡死整条自动化。"""
    from handler_errors import StateStoreError

    store = FakeStore(tables=["demo-config"], raise_on={"get_item": StateStoreError("读失败")})
    assert build_dispatcher(store=store).seconds_since_dispatch(VPC_ID) is None
