"""巡检观测：纳管标签筛选、地址现状计算与「下一个可消费网段」的前置判断。

全部只读，替身不联网。重点断言两件事：
单个 VPC 读失败只影响它自己；观测口径与扩容函数用的是同一个模块，
同一条链路里不会出现两个「利用率」。
"""

import pytest
from handler_errors import ServiceError
from helpers import VPC_ID, FakeApi, monitor_config, subnet
from inspector import (cidrs_of, discover, is_actionable, matches_tag, observe, plan_view)

TAG_KEY = "cidr-expansion"
TAG_VALUE = "managed"


class DiscoveryApi(object):
    """只负责 VPC 列表的替身：标签形态按用例给。"""

    def __init__(self, vpcs, error=None):
        self._vpcs = vpcs
        self._error = error
        self.calls = 0

    def list_vpcs(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._vpcs)


def managed(vpc_id, **extra):
    item = {"id": vpc_id, "name": vpc_id, "cidr": "10.0.0.0/16", "status": "AVAILABLE",
            "tags": {TAG_KEY: TAG_VALUE}}
    item.update(extra)
    return item


# ----------------------------------------------------------------------
# 标签筛选
# ----------------------------------------------------------------------
@pytest.mark.parametrize("tags,expected", [
    ({TAG_KEY: TAG_VALUE}, True),
    ({TAG_KEY: "Managed"}, False),
    ({TAG_KEY: "other"}, False),
    ({"other": TAG_VALUE}, False),
    ({}, False),
    (None, False),
    ([{TAG_KEY: TAG_VALUE}], False),
])
def test_tag_matching_is_key_presence_plus_exact_value(tags, expected):
    assert matches_tag({"tags": tags}, TAG_KEY, TAG_VALUE) is expected


def test_matches_tag_tolerates_non_dict_records():
    assert matches_tag(None, TAG_KEY, TAG_VALUE) is False
    assert matches_tag("vpc-1", TAG_KEY, TAG_VALUE) is False
    assert matches_tag({}, TAG_KEY, TAG_VALUE) is False


def test_discover_keeps_only_managed_vpcs_and_the_number_is_logged():
    api = DiscoveryApi([managed("vpc-1"), {"id": "vpc-2", "tags": {}}, managed("vpc-3")])
    collected = discover(api, monitor_config())
    assert [item["id"] for item in collected] == ["vpc-1", "vpc-3"]
    assert api.calls == 1


def test_discover_raises_when_the_list_itself_is_unreadable():
    """列表读不到就没有任何结论：原样抛出，由平台的投递机制对外发声。"""
    api = DiscoveryApi([], error=ServiceError("列表读不到", status_code=500))
    with pytest.raises(ServiceError):
        discover(api, monitor_config())


# ----------------------------------------------------------------------
# 现状观测
# ----------------------------------------------------------------------
def test_observe_computes_the_same_numbers_the_expansion_side_uses():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": ["10.1.0.0/16"]},
                  subnets=[subnet("192.168.0.0/25", "app-public-1", available=2),
                           subnet("192.168.0.128/25", "app-private-1", available=6)])
    observation = observe(api, managed(VPC_ID), monitor_config())
    assert observation["error"] is None
    assert observation["vpc_id"] == VPC_ID
    # 容量口径是「已划分给子网的可用地址」：每个子网扣 3 个保留地址
    assert observation["total_capacity"] == (128 - 3) * 2
    assert observation["available_ips"] == 8
    assert observation["subnet_count"] == 2
    assert observation["cidr_count"] == 2
    assert observation["utilization"] == round((250 - 8) * 100.0 / 250, 2) == 96.8
    # 空闲地址按网段整体空间计，用于判断「要不要追加新网段」
    assert observation["state"]["unallocated_addresses"] == 256 + 65536 - 256
    assert is_actionable(observation) is True


def test_observe_reports_unknown_availability_without_a_utilization():
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, subnets=[subnet("10.0.0.0/24", "app-public-1")])
    observation = observe(api, managed(VPC_ID), monitor_config())
    assert observation["state"]["availability_known"] is False
    assert observation["utilization"] is None
    assert observation["error"] is None
    # 利用率读不到就不能判定，也就不是可执行观测
    assert is_actionable(observation) is False


def test_observe_isolates_a_single_failing_vpc():
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"},
                  failures={"show_vpc": [ServiceError("读不到", status_code=500)]})
    observation = observe(api, managed(VPC_ID), monitor_config())
    assert observation["state"] is None
    assert "读不到" in observation["error"]
    assert observation["utilization"] is None
    assert is_actionable(observation) is False


def test_observe_without_an_id_never_reaches_the_cloud():
    api = FakeApi()
    observation = observe(api, {"tags": {TAG_KEY: TAG_VALUE}}, monitor_config())
    assert "缺少 VPC 标识" in observation["error"]
    assert api.calls == []


def test_observe_survives_a_subnet_list_failure():
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"},
                  failures={"list_subnets": [ServiceError("子网读不到", status_code=500)]})
    observation = observe(api, managed(VPC_ID), monitor_config())
    assert "子网读不到" in observation["error"]
    assert observation["state"] is None


# ----------------------------------------------------------------------
# 网段视图与规划前置判断
# ----------------------------------------------------------------------
def test_cidrs_of_keeps_main_block_first_and_skips_empty_entries():
    assert cidrs_of({"cidr": "10.0.0.0/16",
                     "extend_cidrs": ["10.1.0.0/16", None, "10.2.0.0/16", ""]}) == \
        ["10.0.0.0/16", "10.1.0.0/16", "10.2.0.0/16"]
    assert cidrs_of({}) == []
    assert cidrs_of({"cidr": None, "extend_cidrs": []}) == []
    # 只有一个附加网段、主网段读不到时仍然给出可比对的顺序
    assert cidrs_of({"cidr": "", "extend_cidrs": ["10.1.0.0/16"]}) == ["10.1.0.0/16"]


def test_plan_view_names_the_next_consumable_block():
    config = monitor_config()
    view = plan_view({"cidr": "192.168.0.0/24", "extend_cidrs": ["10.1.0.0/16"]},
                     [subnet("192.168.0.0/25", "a")], config)
    assert view == {"next_cidr": "10.2.0.0/16", "plan_exhausted": False}


def test_plan_view_flags_an_exhausted_plan_without_touching_anything():
    config = monitor_config(SECONDARY_CIDR_PLAN='["10.1.0.0/16"]')
    view = plan_view({"cidr": "192.168.0.0/24", "extend_cidrs": ["10.1.0.0/16"]}, [], config)
    assert view == {"next_cidr": None, "plan_exhausted": True}


def test_plan_view_skips_blocks_already_used_by_subnets():
    config = monitor_config(SECONDARY_CIDR_PLAN='["10.1.0.0/16", "10.2.0.0/16"]')
    view = plan_view({"cidr": "10.1.0.0/16", "extend_cidrs": []},
                     [subnet("10.1.0.0/24", "a")], config)
    assert view["next_cidr"] == "10.2.0.0/16"


def test_observe_carries_the_plan_view_into_the_same_record():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": []},
                  subnets=[subnet("192.168.0.0/25", "a", available=0),
                           subnet("192.168.0.128/25", "b", available=0)])
    observation = observe(api, managed(VPC_ID), monitor_config())
    assert observation["next_cidr"] == "10.1.0.0/16"
    assert observation["plan_exhausted"] is False


def test_observation_fields_are_the_same_names_the_metric_sequences_use():
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, subnets=[subnet("10.0.0.0/24", "a", available=1)])
    observation = observe(api, managed(VPC_ID), monitor_config())
    for field in ("utilization", "total_capacity", "available_ips", "subnet_count"):
        assert observation[field] is not None
    assert observation["state"]["utilization"] == observation["utilization"]
