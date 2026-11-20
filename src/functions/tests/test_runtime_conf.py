"""扩容函数运行期配置：层级规划、掩码约束与容量算术。"""

import pytest

from helpers import CIDR_PLAN_JSON, TIER_PLAN_JSON, expansion_config, expansion_environ
from handler_errors import ConfigError
from runtime_conf import RuntimeConfig, Tier


def test_required_keys_are_enforced_with_a_clear_list():
    env = expansion_environ()
    del env["TIER_PLAN"]
    del env["LOCK_TABLE_NAME"]
    with pytest.raises(ConfigError) as error:
        RuntimeConfig(env)
    assert sorted(error.value.fields["keys"]) == ["LOCK_TABLE_NAME", "TIER_PLAN"]


def test_loaded_plan_exposes_tables_and_plan_blocks():
    config = expansion_config()
    assert config.region == "cn-north-4"
    assert config.ddb_endpoint == "http://10.0.40.12:8900"
    assert config.lock_table == "demo-locks"
    assert config.secondary_cidr_plan == ["10.1.0.0/16", "10.2.0.0/16", "192.168.0.0/24"]
    assert [tier.tier_name for tier in config.tier_plan] == ["public", "private"]


def test_plan_capacity_and_required_addresses_are_consistent():
    """规划网段容量、全层级所需地址、最小前缀三者必须按同一口径算。"""
    config = expansion_config()
    # 两个层级 × 每层两个可用区 × 每个 /28 的 16 个地址
    assert config.required_addresses() == 2 * 2 * 16
    assert config.tightest_netmask() == 28
    assert config.plan_capacity("10.1.0.0/16") == 65536
    assert config.plan_capacity("10.1.0.0/28") == 16
    assert config.plan_capacity("not-a-cidr") == 0
    assert config.fits_plan("10.1.0.0/16") is True
    assert config.fits_plan("10.0.0.0/28") is False


def test_netmask_for_capacity_matches_plan_capacity_round_trip():
    """最小前缀能装下给定地址数，而再收紧一位就不够。"""
    config = expansion_config()
    for addresses in (1, 2, 16, 17, 256, 1024):
        prefix = config.netmask_for_capacity(addresses)
        assert config.plan_capacity("10.0.0.0/%d" % prefix) >= addresses
        assert config.plan_capacity("10.0.0.0/%d" % (prefix + 1)) < addresses


def test_tier_netmask_default_and_rejects_out_of_range():
    assert Tier.from_plan({"tier_name": "t", "sample_name": "s",
                           "availability_zones": ["cn-north-4a"]}, 24).netmask == 24
    with pytest.raises(ConfigError):
        Tier.from_plan({"tier_name": "t", "sample_name": "s", "availability_zones": ["z"],
                        "netmask": 29}, 24)
    with pytest.raises(ConfigError):
        Tier.from_plan({"tier_name": "t", "sample_name": "s", "availability_zones": ["z"],
                        "netmask": 7}, 24)
    with pytest.raises(ConfigError):
        Tier.from_plan({"tier_name": "t", "sample_name": "s"}, 24)
    with pytest.raises(ConfigError):
        Tier.from_plan("not-a-dict", 24)


def test_tier_accepts_single_zone_as_string():
    tier = Tier.from_plan({"tier_name": "t", "sample_name": "s",
                           "availability_zones": "cn-north-4a"}, 28)
    assert tier.availability_zones == ["cn-north-4a"]
    assert tier.subnet_slots() == 1
    assert tier.addresses_per_subnet() == 16


def test_empty_tier_plan_is_rejected():
    with pytest.raises(ConfigError):
        RuntimeConfig(expansion_environ(TIER_PLAN="[]"))


def test_invalid_json_and_out_of_range_numbers_are_reported_not_swallowed():
    with pytest.raises(ConfigError):
        RuntimeConfig(expansion_environ(SECONDARY_CIDR_PLAN="10.1.0.0/16,"))
    with pytest.raises(ConfigError):
        RuntimeConfig(expansion_environ(CIDR_MAX_COUNT="99"))
    with pytest.raises(ConfigError):
        RuntimeConfig(expansion_environ(SUBNET_RETRY_ATTEMPTS="abc"))
    with pytest.raises(ConfigError):
        RuntimeConfig(expansion_environ(WARNING_THRESHOLD="eighty"))


def test_poll_deadline_must_not_be_below_poll_interval():
    with pytest.raises(ConfigError):
        RuntimeConfig(expansion_environ(POLL_INTERVAL_SECONDS="30", POLL_DEADLINE_SECONDS="10"))


def test_describe_never_exposes_state_store_password():
    """配置视图要进日志与留痕，必须不含任何口令或密钥字段。"""
    view = expansion_config().describe()
    rendered = sorted(view)
    assert "ddb_password" not in rendered
    assert "ddb_username" not in rendered
    for key in ("region", "target_vpc_id", "secondary_cidr_plan", "tiers", "tables"):
        assert key in rendered
    assert "demo-locks" in view["tables"]


@pytest.mark.parametrize("raw, port, expected", [
    ("10.0.40.12", "8900", "http://10.0.40.12:8900"),
    ("10.0.40.12:8900", "8900", "http://10.0.40.12:8900"),
    ("http://10.0.40.12:8900/", "8900", "http://10.0.40.12:8900"),
    ("https://10.0.40.12:8900", "8900", "https://10.0.40.12:8900"),
])
def test_endpoint_normalization_keeps_explicit_scheme(raw, port, expected):
    assert RuntimeConfig(expansion_environ(DDB_ENDPOINT=raw, DDB_PORT=port)).ddb_endpoint == \
        expected


def test_plan_json_accepts_single_object():
    config = RuntimeConfig(expansion_environ(
        TIER_PLAN='{"tier_name":"only","sample_name":"only-1",'
                  '"availability_zones":["cn-north-4a"],"netmask":28}'))
    assert [tier.tier_name for tier in config.tier_plan] == ["only"]


def test_address_plan_is_shared_across_two_constructions():
    """同一份 ``SECONDARY_CIDR_PLAN`` 两次解析结果必须一致（扩容与巡检共用口径）。"""
    first = RuntimeConfig(expansion_environ()).secondary_cidr_plan
    second = RuntimeConfig(expansion_environ()).secondary_cidr_plan
    assert first == second == ["10.1.0.0/16", "10.2.0.0/16", "192.168.0.0/24"]
