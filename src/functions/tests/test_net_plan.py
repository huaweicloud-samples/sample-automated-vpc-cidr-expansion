"""网段规划与容量口径的算术校验（纯计算层，不碰任何云端）。"""

import ipaddress

import net_plan
from net_plan import (RESERVED_IPS_PER_SUBNET, MAX_SUBNET_PREFIX)


def test_reserved_address_constant_is_three():
    """华为云每个子网保留网段/网关/广播三个地址，两个函数必须共用这个常数。"""
    assert RESERVED_IPS_PER_SUBNET == 3
    assert MAX_SUBNET_PREFIX == 28


def test_total_and_usable_addresses_by_mask_boundary():
    assert net_plan.total_addresses("10.0.0.0/24") == 256
    assert net_plan.usable_addresses("10.0.0.0/24") == 253
    # 掩码边界：/28 是允许的最小子网，/29 起小于 8 个地址
    assert net_plan.total_addresses("10.0.0.0/28") == 16
    assert net_plan.usable_addresses("10.0.0.0/28") == 13
    assert net_plan.usable_addresses("10.0.0.0/31") == 0
    assert net_plan.usable_addresses("10.0.0.0/32") == 0


def test_network_rejects_host_bits_and_garbage():
    """主机位置非零（strict=True）与非法输入都归为 None；裸 IP 按 /32 解析。"""
    assert net_plan.network("10.0.0.1/24") is None
    assert net_plan.network("not-a-cidr") is None
    assert net_plan.network(None) is None
    assert net_plan.network("10.0.0.0") == ipaddress.IPv4Network("10.0.0.0/32")
    assert str(net_plan.network("10.0.0.0/16")) == "10.0.0.0/16"


def test_gateway_ip_is_first_host_address():
    assert net_plan.gateway_ip("10.0.40.0/24") == "10.0.40.1"
    assert net_plan.gateway_ip("192.168.7.0/28") == "192.168.7.1"
    assert net_plan.gateway_ip("10.0.0.0/32") is None
    assert net_plan.gateway_ip("bad") is None


def test_summarize_state_capacity_and_utilization():
    """总容量按「地址总数 - 保留位」逐子网累加，利用率 = 已用 / 总容量。"""
    state = net_plan.summarize_state(
        ["10.0.0.0/16", "10.1.0.0/16"],
        [
            {"cidr": "10.0.0.0/24", "available_ip_address_count": 100,
             "availability_zone": "cn-north-4a"},
            {"cidr": "10.0.1.0/24", "available_ip_address_count": 20,
             "availability_zone": "cn-north-4b"},
        ])
    assert state["total_capacity"] == 2 * 253
    assert state["available_ips"] == 120
    assert state["utilization"] == round((506 - 120) * 100.0 / 506, 2)
    assert state["cidr_count"] == 2
    assert state["subnet_count"] == 2
    assert state["availability_zones"] == ["cn-north-4a", "cn-north-4b"]


def test_summarize_state_unallocated_uses_gross_addresses():
    """空闲地址按网段地址总数计（不扣保留位），用于判断是否需要附加新网段。"""
    state = net_plan.summarize_state(["10.0.0.0/24"], [{"cidr": "10.0.0.0/25"}])
    assert state["allocated_addresses"] == 128
    assert state["unallocated_addresses"] == 128


def test_summarize_state_ignores_invalid_and_missing_availability():
    """子网余量读不到时利用率为 None：宁可不扩也不误扩。"""
    state = net_plan.summarize_state(["10.0.0.0/16"], [{"cidr": "10.0.0.0/24"}])
    assert state["availability_known"] is False
    assert state["utilization"] is None
    assert state["available_ips"] is None
    # 非法子网既不贡献容量也不报错
    assert net_plan.summarize_state(["10.0.0.0/16"], [{"cidr": "oops"}])[
        "total_capacity"] == 0


def test_summarize_state_never_returns_negative_utilization():
    """服务端余量偶发偏大时利用率夹到 0，不能出现负数把判定带偏。"""
    state = net_plan.summarize_state(
        ["10.0.0.0/16"], [{"cidr": "10.0.0.0/24", "available_ip_address_count": 300}])
    assert state["utilization"] == 0.0


def test_overlaps_any_treats_unparsable_cidr_as_unsafe():
    assert net_plan.overlaps_any("10.0.5.0/24", ["10.0.0.0/16"]) is True
    assert net_plan.overlaps_any("10.0.5.0/24", ["192.168.0.0/16"]) is False
    assert net_plan.overlaps_any("garbage", ["192.168.0.0/16"]) is True
    assert net_plan.overlaps_any("10.0.5.0/24", ["garbage"]) is False


def test_covers_accepts_bare_ip_and_rejects_child_as_parent():
    assert net_plan.covers("10.0.0.0/16", "10.1.0.0/24") is False
    assert net_plan.covers("10.0.0.0/8", "10.1.0.0/24") is True
    assert net_plan.covers("10.1.2.3", "10.1.2.3/32") is True
    assert net_plan.covers("10.1.0.0/24", "10.0.0.0/16") is False
    assert net_plan.covers("0.0.0.0/0", "10.1.0.0/24") is True


def test_pick_plan_cidr_appends_first_unused_plan_block():
    """次级网段追加：按规划顺序取第一个「未附加且不重叠」的网段。"""
    picked, skipped = net_plan.pick_plan_cidr(
        ["10.1.0.0/16", "10.2.0.0/16"], "10.0.0.0/16", [], [])
    assert picked == "10.1.0.0/16"
    assert skipped == []


def test_pick_plan_cidr_skips_attached_overlapping_and_invalid():
    picked, skipped = net_plan.pick_plan_cidr(
        ["10.1.0.0/16",            # 已附加
         "10.9.9.9/33",            # 非法
         "10.0.5.0/24",            # 与主网段重叠
         "10.5.0.0/16",            # 与既有子网重叠
         "10.2.0.0/16"],           # 应选中
        "10.0.0.0/16", ["10.1.0.0/16"], ["10.5.0.128/25"])
    assert picked == "10.2.0.0/16"
    assert [item["reason"] for item in skipped] == [
        "already_associated", "invalid_format", "overlaps_vpc_cidrs",
        "overlaps_existing_subnets"]


def test_pick_plan_cidr_returns_none_when_plan_exhausted():
    picked, skipped = net_plan.pick_plan_cidr([], "10.0.0.0/16", [], [])
    assert picked is None and skipped == []


def test_free_blocks_drops_blocks_fully_consumed_by_a_subnet():
    """子网与剩余空闲块恰好等宽时必须整块剔除，否则会切出重叠子网。"""
    assert net_plan.free_blocks(["10.0.0.0/24"], ["10.0.0.0/25", "10.0.0.128/25"]) == []
    assert net_plan.free_blocks(["10.0.0.0/25"], ["10.0.0.0/25"]) == []
    # 覆盖更大范围：VPC 主网段被一个同宽子网占满 → 没有空闲空间可分
    assert net_plan.allocate_from_free(
        net_plan.free_blocks(["10.0.0.0/24"], ["10.0.0.0/24"]), 28)[0] is None
    # 部分重叠（子网比剩余块更宽）同样按空处理
    assert net_plan.free_blocks(["10.0.0.128/25"], ["10.0.0.0/24"]) == []


def test_free_blocks_carves_unused_space_and_sorts_by_start():
    blocks = net_plan.free_blocks(["10.0.0.0/24"], ["10.0.0.0/25"])
    assert [str(item) for item in blocks] == ["10.0.0.128/25"]
    # 非法网段被忽略，而不是把整次计算打死
    assert [str(item) for item in net_plan.free_blocks(["10.0.0.0/24", "bad"], [])] == \
        ["10.0.0.0/24"]
    # 两个子网把中间一段占掉，剩余块按起始地址排序
    carved = net_plan.free_blocks(["10.0.0.0/24"], ["10.0.0.64/26", "10.0.0.192/26"])
    assert [str(item) for item in carved] == ["10.0.0.0/26", "10.0.0.128/26"]


def test_allocate_from_free_returns_disjoint_blocks_across_calls():
    """同一轮连续分配不得互相重叠：每次成功都会把已占用块从空闲列表里剔除。"""
    blocks = net_plan.free_blocks(["10.0.0.0/24"], [])
    first, blocks = net_plan.allocate_from_free(blocks, 28)
    second, blocks = net_plan.allocate_from_free(blocks, 28)
    assert first == "10.0.0.0/28"
    assert second == "10.0.0.16/28"
    assert net_plan.overlaps_any(first, [second]) is False


def test_allocate_from_free_refuses_mask_beyond_limit():
    blocks = net_plan.free_blocks(["10.0.0.0/24"], [])
    cidr, unchanged = net_plan.allocate_from_free(blocks, MAX_SUBNET_PREFIX + 1)
    assert cidr is None
    assert unchanged is blocks
    # 空闲块不够切时也返回 None（/28 装不进 /29 的空闲块）
    small = net_plan.free_blocks(["10.0.0.0/29"], [])
    assert net_plan.allocate_from_free(small, 28)[0] is None


def test_required_netmask_rounds_up_to_power_of_two():
    assert net_plan.required_netmask(256) == 24
    assert net_plan.required_netmask(200) == 24
    assert net_plan.required_netmask(257) == 23
    assert net_plan.required_netmask(16) == 28
    assert net_plan.required_netmask(1) == 32
    assert net_plan.required_netmask(0) == 32


def test_split_name_and_next_names_continue_the_sequence():
    assert net_plan.split_name("app-public-2") == ("app-public-", "2", "")
    assert net_plan.split_name("app-public") == ("app-public", None, "")
    assert net_plan.next_names("app-public-2", ["app-public-2"], 2) == \
        ["app-public-3", "app-public-4"]
    # 取现有同名前缀的最大序号，而不是样例名里的序号
    assert net_plan.next_names("app-public-1", ["app-public-7", "app-public-9"], 1) == \
        ["app-public-10"]
    # 样例名没有序号时退化为追加 -1/-2
    assert net_plan.next_names("db", [], 2) == ["db-1", "db-2"]


def test_next_names_keeps_zero_padding_width():
    assert net_plan.next_names("tier-08", ["tier-08"], 2) == ["tier-09", "tier-10"]
    assert net_plan.next_names("tier-09", ["tier-09"], 1) == ["tier-10"]


def test_matching_names_selects_one_tier_only():
    """层级样例名带序号时按前缀归组；``app-2`` 的前缀是 ``app-``，会命中两个层级。"""
    subnets = [{"name": "app-public-1"}, {"name": "app-private-1"}, {"name": None}]
    assert net_plan.matching_names("app-public-2", subnets) == ["app-public-1"]
    assert net_plan.matching_names("app-2", subnets) == ["app-public-1", "app-private-1"]
    # 样例名不带序号时按整名匹配
    assert net_plan.matching_names("app-private-1", subnets) == ["app-private-1"]
    assert net_plan.matching_names("app-public-2", subnets) == ["app-public-1"]
    assert net_plan.matching_names("app-public-2", None) == []


def test_name_is_within_limit():
    assert net_plan.name_is_within_limit("x" * 64) is True
    assert net_plan.name_is_within_limit("x" * 65) is False
    assert net_plan.name_is_within_limit("") is False
    assert net_plan.name_is_within_limit(None) is False
