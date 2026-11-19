"""扩容执行序列：路由改写决策、地址组前缀计算、失败与幂等分支。

全部用本地替身驱动，不碰云端：``FakeApi`` 按真实 ``ipaddress`` 语义维护网段现状。

构造现场的一条主线：``unallocated_addresses`` 不够铺开层级时才会消费规划网段，
所以「必须追加新网段」的用例都把现有地址空间整块铺满（``available=0``）。
"""

import ipaddress

import net_plan
import pytest
from expansion_flow import (STATUS_FAILED, STATUS_PARTIAL, STATUS_SKIPPED_THRESHOLD,
                            STATUS_SUCCESS, ExpansionRunner, required_addresses)
from handler_errors import (AssociationTimeout, ConfigError, PartialExpansion, PlanExhausted,
                            ServiceError, StateStoreError, TransportError)
from hw_cloud_api import CloudApi
from helpers import VPC_ID, FakeApi, FakeStore, expansion_config, subnet, vpc_request
from net_plan import MAX_SUBNET_PREFIX

#: 主网段被两个 /25 占满：现有空间装不下任何新层级
FULLY_USED_SUBNETS = [subnet("192.168.0.0/25", "app-public-1", available=0),
                      subnet("192.168.0.128/25", "app-private-1", available=0)]
#: 主网段 /16 只有一个 /24 子网：现有空间足够，不需要追加网段
ROOMY_VPC_SUBNETS = [subnet("10.0.0.0/24", "app-public-1", available=10)]

TABLES = ["demo-locks", "demo-config", "demo-history"]


def used(cidr, name="used"):
    return subnet(cidr, name, available=0)


def runner(api=None, store=None, config=None, sleeper=None, **kwargs):
    return ExpansionRunner(config or expansion_config(), api or FakeApi(),
                           store or FakeStore(tables=list(TABLES)),
                           logger=None, sleeper=sleeper or (lambda seconds: None), **kwargs)


def created_names(api):
    return [call[2] for call in api.calls if call[0] == "create_subnet"]


# ----------------------------------------------------------------------
# 顺序执行体
# ----------------------------------------------------------------------
def test_secondary_cidr_mode_consumes_plan_and_spreads_tiers():
    """现有空间装不下全层级：消费一个规划网段，附加、并入地址组、按层级铺开子网。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": []},
                  subnets=list(FULLY_USED_SUBNETS))
    result = runner(api=api).run(vpc_request(utilization=92.0))
    assert result["status"] == STATUS_SUCCESS
    assert result["mode"] == "secondary_cidr"
    assert result["secondary_cidr_added"] is True
    assert result["new_cidr"] == "10.1.0.0/16"
    assert result["cidr_count_after"] == 2
    assert result["required_addresses"] == required_addresses(
        expansion_config().tier_plan) == 64
    assert len(result["subnets_created"]) == 4
    assert result["subnets_failed"] == []
    # 附加动作真的发生了一次，且只一次
    assert [call[0] for call in api.calls].count("add_extend_cidr") == 1
    assert "10.1.0.0/16" in api.vpc["extend_cidrs"]


def test_created_subnets_are_disjoint_and_within_the_new_block():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": []},
                  subnets=list(FULLY_USED_SUBNETS))
    result = runner(api=api).run(vpc_request())
    networks = [ipaddress.IPv4Network(item["cidr"]) for item in result["subnets_created"]]
    assert len(networks) == 4
    for index, net in enumerate(networks):
        assert net.prefixlen == MAX_SUBNET_PREFIX
        assert net.subnet_of(ipaddress.IPv4Network("10.1.0.0/16"))
        assert not any(net.overlaps(other) for other in networks[:index])
        assert net_plan.network(item_cidr(net)) == net
    assert [item["gateway_ip"] for item in result["subnets_created"]] == \
        [net_plan.gateway_ip(str(net)) for net in networks]


def item_cidr(net):
    return str(net)


def test_existing_space_mode_does_not_touch_the_vpc_cidrs():
    """现有空闲地址够用时不追加网段，也不写地址组。"""
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, subnets=list(ROOMY_VPC_SUBNETS))
    result = runner(api=api).run(vpc_request())
    assert result["mode"] == "existing_space"
    assert result["secondary_cidr_added"] is False
    assert result["new_cidr"] is None
    assert "add_extend_cidr" not in [call[0] for call in api.calls]
    assert "set_address_group_members" not in [call[0] for call in api.calls]
    assert result["route_check"]["checked"] is False
    assert result["address_group"]["updated"] is False
    assert len(result["subnets_created"]) == 4


def test_below_threshold_skips_without_any_write():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    store = FakeStore(tables=list(TABLES))
    result = runner(api, store).run(vpc_request(utilization=50.0))
    assert result["status"] == STATUS_SKIPPED_THRESHOLD
    assert result["mode"] == "not_required"
    assert "add_extend_cidr" not in [call[0] for call in api.calls]
    assert store.puts == [] and store.updates == []


def test_utilization_missing_from_event_is_recomputed_from_the_cloud():
    """事件没带巡检侧数值时用同一常数自行计算。"""
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"},
                  subnets=[subnet("10.0.0.0/24", "app-public-1", available=1)])
    result = runner(api).run(vpc_request(utilization=None))
    assert result["utilization"] == round((253 - 1) * 100.0 / 253, 2)
    assert result["status"] == STATUS_SUCCESS


def test_config_table_override_replaces_threshold_plan_and_tiers():
    """按 VPC 覆盖规划：阈值、网段规划、层级三份字段都可以被配置表改写。"""
    store = FakeStore(rows={"demo-config": {VPC_ID: {
        "vpc_id": VPC_ID, "warning_threshold": "90",
        "secondary_cidrs": ["172.16.0.0/16"],
        "tiers": [{"tier_name": "only", "sample_name": "only-1",
                   "availability_zones": ["cn-north-4a"], "netmask": 28}]}}})
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    result = runner(api, store).run(vpc_request(utilization=92.0))
    assert result["plan_source"] == "config_table(warning_threshold+secondary_cidrs+tiers)"
    assert result["warning_threshold"] == 90.0
    assert result["new_cidr"] == "172.16.0.0/16"
    assert len(result["subnets_created"]) == 1
    assert [item["tier"] for item in result["subnets_created"]] == ["only"]


def test_threshold_below_override_keeps_the_resource_untouched():
    """覆盖后的阈值更高时，同一条指令会被二次校验拦下来。"""
    store = FakeStore(rows={"demo-config": {VPC_ID: {"vpc_id": VPC_ID,
                                                     "warning_threshold": "95"}}})
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    result = runner(api, store).run(vpc_request(utilization=92.0))
    assert result["status"] == STATUS_SKIPPED_THRESHOLD
    assert result["warning_threshold"] == 95.0


def test_unreadable_config_row_falls_back_to_deployed_plan():
    store = FakeStore(raise_on={"get_item": StateStoreError("表还不存在")})
    result = runner(FakeApi(vpc={"cidr": "192.168.0.0/24"},
                            subnets=list(FULLY_USED_SUBNETS)), store).run(vpc_request())
    assert result["plan_source"] == "environment"
    assert result["new_cidr"] == "10.1.0.0/16"


def test_invalid_config_override_fails_instead_of_silently_reverting():
    """覆盖内容非法就直接失败：静默回落到全局规划会让「按 VPC 定制」不可信。"""
    store = FakeStore(rows={"demo-config": {VPC_ID: {
        "vpc_id": VPC_ID, "tiers": [{"tier_name": "bad", "sample_name": "bad"}]}}})
    with pytest.raises(ConfigError):
        runner(FakeApi(), store).run(vpc_request())


def test_plan_exhausted_is_reported_with_the_plan_context():
    """规划里的网段已全部附加：报「已用尽」并带上规划与数量上下文。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": ["10.1.0.0/28"]},
                  subnets=FULLY_USED_SUBNETS + [used("10.1.0.0/28")])
    config = expansion_config(SECONDARY_CIDR_PLAN='["10.1.0.0/28"]')
    with pytest.raises(PlanExhausted) as error:
        runner(api, config=config).run(vpc_request())
    assert error.value.fields["vpc_id"] == VPC_ID
    assert error.value.fields["plan"] == ["10.1.0.0/28"]
    assert error.value.retryable is False


def test_cidr_count_reaching_the_limit_stops_before_any_write():
    """网段数量触顶：先判数量上限，一次附加都不发。"""
    attached = ["10.1.0.0/28", "10.2.0.0/28", "10.3.0.0/28", "10.4.0.0/28"]
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": list(attached)},
                  subnets=FULLY_USED_SUBNETS + [used(item) for item in attached])
    store = FakeStore(tables=list(TABLES))
    with pytest.raises(PlanExhausted) as error:
        runner(api, store, expansion_config(CIDR_MAX_COUNT="5")).run(vpc_request())
    assert error.value.fields["max_count"] == 5
    assert error.value.fields["cidr_count"] == 5
    assert "add_extend_cidr" not in [call[0] for call in api.calls]
    assert store.puts == []


# ----------------------------------------------------------------------
# 失败分支：云端返回错误 / 结果未知
# ----------------------------------------------------------------------
def test_associate_conflict_is_read_as_already_associated():
    """服务端对重复附加返回冲突：这类「事实已经如此」不是故障，转回读确认。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": ["10.1.0.0/16"]},
                  failures={"add_extend_cidr": [ServiceError("已经附加", status_code=409)]})
    outcome = runner(api)._associate(VPC_ID, "10.1.0.0/16")
    assert outcome["state"] == "associated"
    assert [call[0] for call in api.calls].count("add_extend_cidr") == 1


def test_associate_client_error_fails_immediately():
    """参数/权限类错误：等待与重试都不会变好，立刻失败。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  failures={"add_extend_cidr": [ServiceError("无权操作", status_code=403)]})
    with pytest.raises(ServiceError):
        runner(api)._associate(VPC_ID, "10.1.0.0/16")


def test_associate_timeout_is_confirmed_by_readback_not_resend():
    """写超时后结果未知：不重发，改由轮询确认事实状态。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": ["10.1.0.0/16"]},
                  failures={"add_extend_cidr": [TransportError("请求未得到响应")]})
    assert runner(api)._associate(VPC_ID, "10.1.0.0/16")["state"] == "associated"
    assert [call[0] for call in api.calls].count("add_extend_cidr") == 1


def test_associate_genuinely_absent_raises_association_timeout():
    """轮询窗口内没看到生效：判定超时，整次执行不改动其它资源。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24", "extend_cidrs": []},
                  subnets=list(FULLY_USED_SUBNETS))
    api.add_extend_cidr = lambda vpc_id, cidr: None
    store = FakeStore(tables=list(TABLES))
    with pytest.raises(AssociationTimeout) as error:
        runner(api, store).run(vpc_request())
    assert error.value.fields["cidr"] == "10.1.0.0/16"
    assert error.value.fields["vpc_id"] == VPC_ID
    # 附加没确认就停在这里：既不铺子网，也不写地址组
    methods = [call[0] for call in api.calls]
    assert "create_subnet" not in methods
    assert "set_address_group_members" not in methods
    assert store.puts == []


def test_subnet_transport_timeout_reads_back_by_name_instead_of_resending():
    """创建子网超时后按名字回读：命中就当创建成功，绝不重发。"""
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, subnets=list(ROOMY_VPC_SUBNETS),
                  failures={"create_subnet": [TransportError("请求未得到响应")]})
    read_back_counts = {}

    def read_back(vpc_id, name):
        read_back_counts[name] = read_back_counts.get(name, 0) + 1
        return {"id": "subnet-readback", "name": name, "cidr": "10.0.8.0/28",
                "availability_zone": "cn-north-4a", "gateway_ip": "10.0.8.1"}

    api.subnet_exists = read_back
    result = runner(api).run(vpc_request())
    assert len(created_names(api)) == 4
    assert created_names(api).count(created_names(api)[0]) == 1
    assert read_back_counts and set(read_back_counts.values()) == {1}
    assert any(item["id"] == "subnet-readback" for item in result["subnets_created"])
    assert result["status"] == STATUS_SUCCESS


def test_subnet_conflict_is_read_as_created():
    """同名子网已存在（409）同样按创建成功处理。"""
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, subnets=list(ROOMY_VPC_SUBNETS),
                  failures={"create_subnet": [ServiceError("名称已存在", status_code=409)]})
    api.subnet_exists = lambda vpc_id, name: {
        "id": "subnet-conflict", "name": name, "cidr": "10.0.8.0/28",
        "availability_zone": "cn-north-4a", "gateway_ip": "10.0.8.1"}
    result = runner(api).run(vpc_request())
    assert len(created_names(api)) == 4
    assert any(item["id"] == "subnet-conflict" for item in result["subnets_created"])


def test_all_subnets_failing_raises_with_the_scene_attached():
    """子网全灭：留痕已写、现场挂在异常上，供入口的失败通知使用。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  failures={"create_subnet": [ServiceError("配额不足", status_code=400)] * 8})
    store = FakeStore(tables=list(TABLES))
    with pytest.raises(PartialExpansion) as error:
        runner(api, store).run(vpc_request())
    scene = error.value.result
    assert scene["status"] == STATUS_FAILED
    assert scene["subnets_created"] == []
    assert len(scene["subnets_failed"]) == 4
    assert scene["secondary_cidr_added"] is True
    # 不可重试的状态码只发一次，不消耗整段重试预算
    assert [item["attempts"] for item in scene["subnets_failed"]] == [1, 1, 1, 1]
    # 资源已经改动，台账必须落表：写失败不回滚已创建的云资源
    assert store.puts and store.puts[-1]["table"] == "demo-history"


def test_partial_success_when_one_zone_keeps_failing():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    original = api.create_subnet

    def flaky(vpc_id, spec):
        if spec["availability_zone"] == "cn-north-4b":
            raise ServiceError("该可用区暂不可用", status_code=400)
        return original(vpc_id, spec)

    api.create_subnet = flaky
    result = runner(api).run(vpc_request())
    assert result["status"] == STATUS_PARTIAL
    assert len(result["subnets_created"]) == 2
    assert len(result["subnets_failed"]) == 2
    assert result["subnet_retry_budget_seconds"] == 2


def test_retry_budget_is_shared_across_the_whole_subnet_stage():
    """预算是「整段共享」：用完就不再等，剩余子网按失败记录。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  failures={"create_subnet": [ServiceError("暂时不可用", status_code=503)] * 20})
    slept = []
    with pytest.raises(PartialExpansion) as error:
        runner(api, sleeper=slept.append).run(vpc_request())
    scene = error.value.result
    assert scene["subnet_retry_budget_seconds"] == 2
    assert scene["subnet_retry_used_seconds"] == 2
    # 预算只花在子网重试上：附加网段轮询的那次 0 秒等待不占预算
    assert [seconds for seconds in slept if seconds] == [1, 1]
    assert scene["subnets_created"] == []
    assert [item["attempts"] for item in scene["subnets_failed"]] == [2, 2, 1, 1]


def test_subnet_name_over_the_limit_is_recorded_as_a_failure():
    """名称超过 64 字符上限：记为失败而不是让服务端报错。"""
    long_sample = "x" * 63 + "-1"
    api = FakeApi(vpc={"cidr": "10.0.0.0/16"}, subnets=list(ROOMY_VPC_SUBNETS))
    config = expansion_config(TIER_PLAN=(
        '[{"tier_name":"public","sample_name":"%s","availability_zones":'
        '["cn-north-4a","cn-north-4b"],"netmask":28},'
        '{"tier_name":"private","sample_name":"app-private-1",'
        '"availability_zones":["cn-north-4a"],"netmask":28}]' % long_sample))
    result = runner(api, config=config).run(vpc_request())
    assert result["status"] == STATUS_PARTIAL
    assert [item["tier"] for item in result["subnets_created"]] == ["private"]
    assert len(created_names(api)) == 1
    assert {item["tier"] for item in result["subnets_failed"]} == {"public"}
    assert len(result["subnets_failed"]) == 2
    assert "64" in result["subnets_failed"][0]["reason"]


# ----------------------------------------------------------------------
# 路由改写决策（只读校验）与地址组
# ----------------------------------------------------------------------
def test_route_check_picks_the_most_specific_covering_route():
    api = FakeApi(route_table={"id": "rt-1", "routes": [
        {"destination": "0.0.0.0/0"},        # 默认路由必须排除
        {"destination": "10.0.0.0/8"},
        {"destination": "10.1.0.0/16"},
        {"destination": "10.2.0.0/16"},       # 不覆盖新网段
    ]}, vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    check = runner(api).run(vpc_request())["route_check"]
    assert check["checked"] is True and check["covered"] is True
    assert check["matched"] == "10.1.0.0/16"
    assert check["route_count"] == 4


def test_default_route_alone_does_not_count_as_coverage():
    """拿 0.0.0.0/0 判定「已被本地路由覆盖」等于永远判定通过。"""
    api = FakeApi(route_table={"id": "rt-1", "routes": [{"destination": "0.0.0.0/0"}]},
                  vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    check = runner(api).run(vpc_request())["route_check"]
    assert check["checked"] is True and check["covered"] is False
    assert check["matched"] is None


def test_bare_ip_destination_is_read_as_a_host_route():
    api = FakeApi(route_table={"id": "rt-1", "routes": [{"destination": "10.1.2.3"}]},
                  vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    check = runner(api).run(vpc_request())["route_check"]
    assert check["covered"] is False


def test_route_check_degrades_when_the_table_is_unreadable():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  failures={"show_route_table": [ServiceError("路由表读不到", status_code=500)]})
    check = runner(api).run(vpc_request())["route_check"]
    assert check["checked"] is False and check["reason"] == "路由表读取失败"
    assert check["detail"]


def test_route_check_and_address_group_skip_when_ids_are_missing():
    config = expansion_config(ADDRESS_GROUP_ID=False, DEFAULT_ROUTE_TABLE_ID=False)
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    result = runner(api, config=config).run(vpc_request())
    assert result["route_check"]["reason"] == "未配置 DEFAULT_ROUTE_TABLE_ID"
    assert result["address_group"]["reason"] == "未配置 ADDRESS_GROUP_ID"


def test_function_never_writes_routes():
    """路由改写由平台在附加网段生效时自动维护：本实践的客户端不提供写路由能力。"""
    for forbidden in ("create_route", "update_route", "delete_route",
                      "add_route", "set_route", "update_route_table"):
        assert not hasattr(CloudApi, forbidden), forbidden


def test_address_group_gets_the_new_prefix_once():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    group = runner(api).run(vpc_request())["address_group"]
    assert group["updated"] is True
    assert group["members"] == ["10.0.0.0/16", "10.1.0.0/16"]
    assert [call[0] for call in api.calls].count("set_address_group_members") == 1


def test_address_group_write_is_idempotent_when_cidr_is_already_a_member():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  address_group={"ip_set": ["10.0.0.0/16", "10.1.0.0/16"],
                                 "max_capacity": 1000})
    group = runner(api).run(vpc_request())["address_group"]
    assert group["updated"] is False
    assert group["reason"] == "已是成员"
    assert "set_address_group_members" not in [call[0] for call in api.calls]


def test_address_group_capacity_overflow_is_a_hard_failure():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  address_group={"ip_set": ["10.0.0.0/16", "10.2.0.0/16"], "max_capacity": 2})
    with pytest.raises(ServiceError) as error:
        runner(api).run(vpc_request())
    assert error.value.error_code == "AddressGroupCapacityFull"
    assert api.address_group["ip_set"] == ["10.0.0.0/16", "10.2.0.0/16"]


def test_address_group_write_timeout_confirmed_by_readback():
    """写超时后回读确认：成员真的进去了就当成功，不再重发。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    original = api.set_address_group_members

    def write_then_timeout(group_id, ip_set):
        original(group_id, ip_set)
        raise TransportError("请求未得到响应")

    api.set_address_group_members = write_then_timeout
    group = runner(api).run(vpc_request())["address_group"]
    assert group["updated"] is True
    assert group["verified_after_error"] is True
    assert group["members"] == ["10.0.0.0/16", "10.1.0.0/16"]
    assert [call[0] for call in api.calls].count("set_address_group_members") == 1


def test_address_group_write_failure_without_effect_is_raised():
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS),
                  failures={"set_address_group_members": [ServiceError("写入被拒",
                                                                       status_code=400)]})
    with pytest.raises(ServiceError):
        runner(api).run(vpc_request())


# ----------------------------------------------------------------------
# 幂等：重复触发不重复扩容
# ----------------------------------------------------------------------
def test_second_run_on_an_expanded_vpc_does_not_consume_a_second_block():
    """同一轮结果之后再触发一次：已附加的网段不会被再次附加。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    store = FakeStore(tables=list(TABLES))
    first = runner(api, store).run(vpc_request())
    assert first["new_cidr"] == "10.1.0.0/16"

    second = runner(api, store).run(vpc_request(execution_id="exec-2"))
    # 上一轮已把新空间铺开，本轮无需再消费网段
    assert second["mode"] == "existing_space"
    assert second["secondary_cidr_added"] is False
    assert api.vpc["extend_cidrs"] == ["10.1.0.0/16"]
    assert [call[0] for call in api.calls].count("add_extend_cidr") == 1
    assert len(second["subnets_created"]) == 4
    # 新切的子网与上一轮的四个子网互不重叠
    all_nets = [ipaddress.IPv4Network(item["cidr"])
                for item in first["subnets_created"] + second["subnets_created"]]
    for index, net in enumerate(all_nets):
        assert not any(net.overlaps(other) for other in all_nets[:index])


def test_same_plan_block_is_never_consumed_twice_in_one_round():
    """同一次调用里连续分配的地址段必须互不重叠。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    created = runner(api).run(vpc_request())["subnets_created"]
    assert len({item["cidr"] for item in created}) == len(created)


def test_expansion_never_writes_back_the_operations_owned_config_fields():
    """扩容函数对配置表只读：运维维护的按 VPC 覆盖字段不被回写。"""
    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    store = FakeStore(rows={"demo-config": {VPC_ID: {
        "vpc_id": VPC_ID, "warning_threshold": "80", "secondary_cidrs": ["10.1.0.0/16"],
        "tiers": [{"tier_name": "public", "sample_name": "app-public-1",
                   "availability_zones": ["cn-north-4a", "cn-north-4b"], "netmask": 28}]}}})
    runner(api, store).run(vpc_request())
    assert [item["table"] for item in store.updates] == []
    assert {item["table"] for item in store.puts} == {"demo-history"}
    # 配置表里那三个字段保持部署时运维写入的原值
    assert store.rows["demo-config"][VPC_ID]["secondary_cidrs"] == ["10.1.0.0/16"]


def test_history_record_carries_the_whole_scene():
    from history_writer import build_record

    api = FakeApi(vpc={"cidr": "192.168.0.0/24"}, subnets=list(FULLY_USED_SUBNETS))
    store = FakeStore(tables=list(TABLES))
    config = expansion_config()
    result = runner(api, store).run(vpc_request())
    record = build_record(result, vpc_request(), {"tiers": config.tier_plan,
                                                 "warning_threshold": 80.0,
                                                 "source": "environment"},
                          config, "2026-05-01T10:30:00Z")
    assert record["timestamp"] == "2026-05-01T10:30:00Z"
    assert record["new_cidr"] == "10.1.0.0/16"
    assert record["secondary_cidr_added"] is True
    assert len(record["subnets_created"]) == 4
    assert record["address_group"]["updated"] is True
    assert record["plan_source"] == "environment"
    assert store.puts[-1]["table"] == "demo-history"
    assert store.puts[-1]["item"]["vpc_id"] == VPC_ID
