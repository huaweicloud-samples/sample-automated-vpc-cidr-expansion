"""监控指标：名称/命名空间/维度常量，以及上报的分块与失败口径。"""

import pytest
from helpers import VPC_ID, FakeApi, monitor_config, monitor_environ
from metrics import (BATCH_SIZE, METRIC_AVAILABLE, METRIC_CAPACITY, METRIC_SUBNETS,
                     METRIC_UTILIZATION, UNIT_COUNT, UNIT_PERCENT, build_samples, report)
from monitor_conf import MonitorConfig

NAMESPACE = "vpc_cidr_expansion.custom"
DIMENSION = "VpcId"


def observation(**overrides):
    item = {"vpc_id": VPC_ID, "utilization": 87.3, "available_ips": 120,
            "total_capacity": 506, "subnet_count": 2}
    item.update(overrides)
    return item


def _env_with(**overrides):
    env = monitor_environ()
    env.update(overrides)
    return MonitorConfig(env)


def test_metric_name_constants_are_the_dashboard_contract():
    """四个指标名逐字固定：改名等于把大盘取数打空。"""
    assert METRIC_UTILIZATION == "UtilizationPercentage"
    assert METRIC_AVAILABLE == "AvailableIPs"
    assert METRIC_CAPACITY == "TotalCapacity"
    assert METRIC_SUBNETS == "SubnetCount"
    assert UNIT_PERCENT == "Percent"
    assert UNIT_COUNT == "Count"


def test_namespace_and_dimension_constants_match_infra(infra_variables, infra_locals):
    """代码常量与部署栈下发值、大盘图件用的是同一套取值。"""
    config = monitor_config()
    assert config.metric_namespace == NAMESPACE
    assert config.metric_dimension == DIMENSION
    assert 'default     = "%s"' % NAMESPACE in infra_variables
    assert 'default     = "%s"' % DIMENSION in infra_variables
    # 自定义命名空间禁止连字符：默认值必须可安全下发
    assert "-" not in NAMESPACE
    for name in (METRIC_UTILIZATION, METRIC_AVAILABLE, METRIC_CAPACITY, METRIC_SUBNETS):
        assert '"%s"' % name in infra_locals


def test_namespace_is_taken_from_configuration_verbatim():
    """下发什么命名空间就上报什么，代码不擅自改写。"""
    samples = build_samples(observation(), _env_with(
        CES_NAMESPACE="other_namespace.custom", CES_DIMENSION_NAME="VpcId"))
    assert {item["namespace"] for item in samples} == {"other_namespace.custom"}


def test_build_samples_produces_four_sequences_with_single_dimension():
    samples = build_samples(observation(), monitor_config(), timestamp_ms=1735689600000)
    assert len(samples) == 4
    assert sorted(item["metric_name"] for item in samples) == sorted([
        METRIC_UTILIZATION, METRIC_AVAILABLE, METRIC_CAPACITY, METRIC_SUBNETS])
    for item in samples:
        assert item["namespace"] == NAMESPACE
        assert item["timestamp"] == 1735689600000
        assert item["dimensions"] == [{"name": DIMENSION, "value": VPC_ID}]
    by_name = {item["metric_name"]: item for item in samples}
    assert by_name[METRIC_UTILIZATION]["unit"] == UNIT_PERCENT
    assert by_name[METRIC_UTILIZATION]["value"] == pytest.approx(87.3)
    assert by_name[METRIC_SUBNETS]["unit"] == UNIT_COUNT


def test_build_samples_skips_missing_values_only():
    """利用率算不出来时只跳过那一条序列：大盘上「无数据」与「0」是两件事。"""
    samples = build_samples(observation(utilization=None), monitor_config(), timestamp_ms=1)
    assert [item["metric_name"] for item in samples] == [
        METRIC_AVAILABLE, METRIC_CAPACITY, METRIC_SUBNETS]
    assert all(item["value"] is not None for item in samples)


def test_build_samples_needs_vpc_id():
    assert build_samples({"utilization": 90.0}, monitor_config()) == []
    assert build_samples({"vpc_id": None}, monitor_config()) == []


def test_report_splits_into_batches_and_counts_samples():
    """每 VPC 四条序列，累计超过单批上限就分块发送。"""
    api = FakeApi()
    result = report(api, [observation(vpc_id="vpc-%04d" % i) for i in range(10)],
                    monitor_config(), timestamp_ms=1)
    assert result == {"samples": 40, "batches": 1, "failed_batches": 0}

    api = FakeApi()
    result = report(api, [observation(vpc_id="vpc-%04d" % i) for i in range(15)],
                    monitor_config(), timestamp_ms=1)
    sizes = [call[1] for call in api.calls if call[0] == "report_metric_batch"]
    assert result["samples"] == 60
    assert result["batches"] == 2
    assert sizes == [BATCH_SIZE, 10]


def test_metric_write_failure_does_not_break_the_round():
    """指标是大盘的输入：写失败只计数，不能抛异常打断已完成的判定。"""
    from handler_errors import ServiceError

    api = FakeApi(failures={"report_metric_batch": [ServiceError("拒绝写入", status_code=400)]})
    result = report(api, [observation()], monitor_config(), timestamp_ms=1)
    assert result == {"samples": 4, "batches": 1, "failed_batches": 1}
    assert api.calls  # 确实尝试过上报


def test_report_with_nothing_to_send():
    api = FakeApi()
    assert report(api, [], monitor_config()) == {"samples": 0, "batches": 0,
                                                "failed_batches": 0}
    assert api.calls == []
