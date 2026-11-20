"""两个函数入口：装配、收尾顺序与「什么算失败」。

入口是契约的落点：handler 名、锁的释放、通知的取舍、失败必须原样抛出
（平台只有看到执行失败才会把消息投进死信主题）。
这里用真实执行体 + 替身服务端，不联网、凭据只是占位符。
"""

import json
import logging
import re
import time

import pytest
from helpers import (FULLY_USED, PLACEHOLDER_DDB_PASSWORD, PLACEHOLDER_SECRET_KEY, VPC_ID,
                     FakeApi, FakeContext, FakeStore, expansion_environ, monitor_environ, subnet,
                     vpc_request)
from handler_errors import PartialExpansion, ServiceError

TABLES = ["demo-locks", "demo-config", "demo-history"]
COMMAND_TOPIC = "urn:smn:cn-north-4:demo:cidr-command"


def command_event(vpc_id=VPC_ID, utilization=92.0):
    return {"vpc_id": vpc_id, "reason": "utilization", "utilization": utilization}


def install_expansion(monkeypatch, entry, api, store, **overrides):
    for key, value in expansion_environ(**overrides).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, "CloudApi", lambda *args, **kwargs: api)
    monkeypatch.setattr(entry, "StateStore", lambda *args, **kwargs: store)
    return api, store


def install_monitor(monkeypatch, entry, api, store, **overrides):
    for key, value in monitor_environ(**overrides).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entry, "CloudApi", lambda *args, **kwargs: api)
    monkeypatch.setattr(entry, "StateStore", lambda *args, **kwargs: store)
    return api, store


def crowded_api():
    """主网段铺满、余量为零：一定需要消费规划网段，才会走到附加那一步。"""
    return FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": []},
                   subnets=[subnet(cidr, name, available=0)
                            for cidr, name in FULLY_USED])


# ----------------------------------------------------------------------
# 扩容入口
# ----------------------------------------------------------------------
def test_expansion_entry_runs_the_whole_chain_and_notifies_once(expansion_entry, monkeypatch):
    api, store = install_expansion(monkeypatch, expansion_entry, crowded_api(),
                                   FakeStore(tables=list(TABLES)))
    result = expansion_entry.main_handler(command_event(), FakeContext())
    assert result["status"] == "SUCCESS"
    assert result["new_cidr"] == "10.1.0.0/16"
    assert len(result["subnets_created"]) == 4
    # 收尾：放锁先于通知
    assert result["lock"] == {"released": True, "reason": "released"}
    assert store.rows["demo-locks"][VPC_ID]["status"] == "RELEASED"
    assert result["history_written"] is True and result["history_table"] == "demo-history"
    assert len(api.published) == 1
    published = api.published[0]
    assert published["subject"] == \
        "[SUCCESS] VPC CIDR Expansion - <HW_ACCOUNT_ID> (cn-north-4)"
    # 版式按字符补齐（``%-16s`` 对中文标签留出的空格数不是固定的），只断言标签与取值相邻
    assert re.search(r"新增附加网段\s+是 \(10\.1\.0\.0/16\)", published["message"])
    assert published["message"].count("subnet-") >= 1


def test_expansion_entry_never_puts_credentials_into_the_log(expansion_entry, monkeypatch,
                                                             caplog):
    install_expansion(monkeypatch, expansion_entry, crowded_api(),
                      FakeStore(tables=list(TABLES)))
    with caplog.at_level(logging.INFO):
        expansion_entry.main_handler(command_event(), FakeContext())
    assert "expansion_begin" in caplog.text
    for secret in (PLACEHOLDER_SECRET_KEY, PLACEHOLDER_DDB_PASSWORD):
        assert secret not in caplog.text


def test_expansion_entry_skips_a_vpc_it_is_not_bound_to(expansion_entry, monkeypatch):
    api, store = install_expansion(monkeypatch, expansion_entry, crowded_api(),
                                   FakeStore(tables=list(TABLES)),
                                   TARGET_VPC_ID="vpc-some-other")
    result = expansion_entry.main_handler(command_event(), FakeContext())
    assert result["status"] == "SKIPPED_TARGET_MISMATCH"
    assert result["expected_vpc_id"] == "vpc-some-other"
    assert result["lock"] == {"released": False, "reason": "not_acquired"}
    # 护栏生效在取锁之前：一次云端写都没有
    assert api.calls == []
    assert api.published == []
    assert store.puts == []


def test_expansion_entry_turns_a_busy_lock_into_a_skip_not_a_failure(expansion_entry,
                                                                     monkeypatch):
    api, store = install_expansion(
        monkeypatch, expansion_entry, crowded_api(),
        FakeStore(rows={
            "demo-locks": {VPC_ID: {"vpc_id": VPC_ID, "lock_holder": "exec-running",
                                    "status": "LOCKED", "lock_ttl": int(time.time()) + 600}},
            "demo-config": {}, "demo-history": {}}))
    result = expansion_entry.main_handler(command_event(), FakeContext(request_id="exec-second"))
    assert result["status"] == "SKIPPED_LOCK_BUSY"
    assert result["lock_holder"] == "exec-running"
    assert result["execution_id"] == "exec-second"
    # 跳过不发通知：否则每个巡检周期都会寄一封信
    assert api.published == []
    assert api.calls == []
    assert store.rows["demo-locks"][VPC_ID]["lock_holder"] == "exec-running"


def test_two_deliveries_for_the_same_vpc_expand_only_once(expansion_entry, monkeypatch):
    """重复投递的真实幂等：第二次要么让锁要么让规划说话，绝不会附加第二个网段。"""
    api, store = install_expansion(monkeypatch, expansion_entry, crowded_api(),
                                   FakeStore(tables=list(TABLES)))
    first = expansion_entry.main_handler(command_event(), FakeContext(request_id="exec-1"))
    second = expansion_entry.main_handler(command_event(), FakeContext(request_id="exec-2"))
    assert first["secondary_cidr_added"] is True
    assert second["mode"] == "existing_space"
    assert api.vpc["extend_cidrs"] == ["10.1.0.0/16"]
    assert [call[0] for call in api.calls].count("add_extend_cidr") == 1


def test_expansion_entry_rethrows_after_releasing_and_notifying(expansion_entry, monkeypatch):
    api = crowded_api()
    api._failures["add_extend_cidr"] = [ServiceError("无权操作", status_code=403)]
    _store = FakeStore(tables=list(TABLES))
    install_expansion(monkeypatch, expansion_entry, api, _store)
    with pytest.raises(ServiceError):
        expansion_entry.main_handler(command_event(), FakeContext())
    # 再抛出是必需的：只有执行失败，平台才会把消息投进死信主题
    assert api.published[0]["subject"].startswith("[FAILED]")
    assert "无权操作" in api.published[0]["message"]
    assert _store.rows["demo-locks"][VPC_ID]["status"] == "RELEASED"


def test_failure_notification_carries_the_scene_of_a_partial_run(expansion_entry, monkeypatch):
    api = crowded_api()
    api._failures["create_subnet"] = [ServiceError("配额不足", status_code=400)] * 8
    install_expansion(monkeypatch, expansion_entry, api, FakeStore(tables=list(TABLES)))
    with pytest.raises(PartialExpansion):
        expansion_entry.main_handler(command_event(), FakeContext())
    message = api.published[0]["message"]
    assert "错误类型" in message and "PartialExpansion" in message
    assert "执行编号: exec-demo-0001" in message
    assert "锁状态: 已释放" in message


def test_a_broken_notification_topic_does_not_change_the_verdict(expansion_entry, monkeypatch):
    api, _store = install_expansion(monkeypatch, expansion_entry, crowded_api(),
                                    FakeStore(tables=list(TABLES)))
    api._failures["publish_message"] = [ServiceError("主题不可用", status_code=500)]
    result = expansion_entry.main_handler(command_event(), FakeContext())
    assert result["status"] == "SUCCESS"
    assert api.published == []


def test_missing_deployment_configuration_is_a_failure_before_any_write(expansion_entry,
                                                                       monkeypatch):
    api, store = install_expansion(monkeypatch, expansion_entry, crowded_api(),
                                   FakeStore(tables=list(TABLES)))
    monkeypatch.delenv("TIER_PLAN")
    with pytest.raises(Exception) as error:
        expansion_entry.main_handler(command_event(), FakeContext())
    assert error.value.__class__.__name__ == "ConfigError"
    assert api.calls == [] and store.puts == [] and api.published == []


def test_history_write_failure_is_reported_in_the_result(expansion_entry, monkeypatch):
    from state_store import StateStoreError

    store = FakeStore(tables=list(TABLES),
                      raise_on={"put_item:demo-history": StateStoreError("台账写不进去")})
    api, store = install_expansion(monkeypatch, expansion_entry, crowded_api(), store)
    result = expansion_entry.main_handler(command_event(), FakeContext())
    # 资源事实已改变：台账写不进去只是少一份留痕，不把成功改判成失败
    assert result["status"] == "SUCCESS"
    assert result["history_written"] is False
    assert result["lock"]["released"] is True


# ----------------------------------------------------------------------
# 巡检入口
# ----------------------------------------------------------------------
def managed_api(available=1):
    return FakeApi(vpc={"cidr": "10.0.0.0/16"}, monitored_tags={"cidr-expansion": "managed"},
                   subnets=[subnet("10.0.0.0/24", "app-public-1", available=available)])


def test_monitor_round_observes_reports_and_dispatches_once(monitor_entry, monkeypatch):
    api, store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                 FakeStore(tables=["demo-config"]))
    summary = monitor_entry.main_handler({"trigger_type": "TIMER"},
                                         FakeContext(name="vpc-cidr-monitor"))
    assert summary["trigger"] == "TIMER"
    assert summary["monitored_count"] == 1 and summary["observed"] == 1
    assert summary["decision_counts"] == {"above_threshold": 1}
    assert summary["metrics_reported"] == {"samples": 4, "batches": 1, "failed_batches": 0}
    assert summary["dispatches"] == [{"vpc_id": VPC_ID, "dispatched": True, "detail": None}]
    assert api.published[0]["topic"] == COMMAND_TOPIC
    payload = json.loads(api.published[0]["message"])
    assert payload["vpc_id"] == VPC_ID and payload["suggested_cidr"] == "10.1.0.0/16"
    assert payload["requested_by"] == "cidr-monitor"
    assert api.reported and {item["metric_name"] for item in api.reported} == \
        {"UtilizationPercentage", "AvailableIPs", "TotalCapacity", "SubnetCount"}


def test_monitor_writes_only_its_own_observation_fields(monitor_entry, monkeypatch):
    api, store = install_monitor(
        monkeypatch, monitor_entry, managed_api(),
        FakeStore(rows={"demo-config": {VPC_ID: {
            "vpc_id": VPC_ID, "warning_threshold": "80", "secondary_cidrs": ["10.1.0.0/16"],
            "tiers": [{"tier_name": "public"}]}}}))
    monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    row = store.rows["demo-config"][VPC_ID]
    # 运行期让权：运维维护的三个字段不被回写
    assert row["warning_threshold"] == "80"
    assert row["secondary_cidrs"] == ["10.1.0.0/16"]
    assert row["tiers"] == [{"tier_name": "public"}]
    assert row["monitored"] is True and row["utilization"] == 99.6


def test_a_second_round_stays_inside_the_dispatch_window(monitor_entry, monkeypatch):
    """连续两轮都看到高利用率：静默窗口让第二条指令不会发出去。"""
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                  FakeStore(tables=["demo-config"]))
    monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor",
                                               request_id="exec-round-1"))
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor",
                                                         request_id="exec-round-2"))
    assert len(api.published) == 1
    assert summary["decision_counts"] == {"within_cooldown": 1}
    assert summary["dispatches"] == []


def test_monitor_never_touches_the_network_writes(monitor_entry, monkeypatch):
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                 FakeStore(tables=["demo-config"]))
    monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    methods = [call[0] for call in api.calls]
    for forbidden in ("add_extend_cidr", "create_subnet", "set_address_group_members"):
        assert forbidden not in methods


def test_a_single_unreadable_vpc_does_not_stop_the_round(monitor_entry, monkeypatch):
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                 FakeStore(tables=["demo-config"]))
    api._failures["show_vpc"] = [ServiceError("读不到", status_code=500)]
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    assert summary["observe_failed"] == 1 and summary["observed"] == 0
    assert summary["decision_counts"] == {"observe_failed": 1}
    assert summary["dispatches"] == [] and api.published == []
    assert summary["metrics_reported"] == {"samples": 0, "batches": 0, "failed_batches": 0}


def test_discovery_failure_is_rethrown_for_the_dead_letter_path(monitor_entry, monkeypatch):
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                 FakeStore(tables=["demo-config"]))
    api._failures["list_vpcs"] = [ServiceError("列表读不到", status_code=500)]
    with pytest.raises(ServiceError):
        monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))


def test_an_unmanaged_account_produces_an_empty_round(monitor_entry, monkeypatch):
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, monitored_tags={},
                  subnets=[subnet("10.0.0.0/24", "app-public-1", available=1)])
    store = FakeStore(tables=["demo-config"])
    install_monitor(monkeypatch, monitor_entry, api, store)
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    assert summary["monitored_count"] == 0
    assert "没有命中纳管标签" in summary["detail"]
    assert summary["dispatches"] == []
    # 一轮空转不建表也不写表
    assert store.calls == []


def test_metric_report_failure_keeps_the_command_dispatched(monitor_entry, monkeypatch):
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                 FakeStore(tables=["demo-config"]))
    api._failures["report_metric_batch"] = [ServiceError("大盘写不进", status_code=500)]
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    assert summary["metrics_reported"]["failed_batches"] == 1
    assert summary["dispatches"][0]["dispatched"] is True


def test_exhausted_plan_is_not_dispatched(monitor_entry, monkeypatch):
    api, _store = install_monitor(
        monkeypatch, monitor_entry, managed_api(), FakeStore(tables=["demo-config"]),
        SECONDARY_CIDR_PLAN='["10.2.0.0/16"]')
    api.vpc["extend_cidrs"] = ["10.2.0.0/16"]
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    assert summary["decision_counts"] == {"plan_exhausted": 1}
    assert api.published == []


def test_a_below_threshold_round_reports_metrics_without_a_command(monitor_entry, monkeypatch):
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(available=200),
                                 FakeStore(tables=["demo-config"]))
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    assert summary["decision_counts"] == {"below_threshold": 1}
    assert api.published == []
    assert summary["metrics_reported"]["samples"] == 4


def test_snapshot_is_written_as_a_side_channel(monitor_entry, monkeypatch):
    api, _store = install_monitor(monkeypatch, monitor_entry, managed_api(),
                                 FakeStore(tables=["demo-config"]))
    summary = monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    # 本地没有配置留痕桶：快照写不进只是少一份留痕，本轮结论不变
    assert summary["snapshot"] == {"written": False,
                                   "detail": "未配置 OBS_BUCKET / OBS_ENDPOINT"}


def test_monitor_logs_are_credential_free(monitor_entry, monkeypatch, caplog):
    install_monitor(monkeypatch, monitor_entry, managed_api(), FakeStore(tables=["demo-config"]))
    with caplog.at_level(logging.INFO):
        monitor_entry.main_handler({}, FakeContext(name="vpc-cidr-monitor"))
    assert "monitor_round" in caplog.text
    for secret in (PLACEHOLDER_SECRET_KEY, PLACEHOLDER_DDB_PASSWORD):
        assert secret not in caplog.text
