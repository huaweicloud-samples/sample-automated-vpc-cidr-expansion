"""运行期配置：只从环境变量读取，键名由 ``infra/locals.tf`` 的
``expansion_user_data`` 下发，改键名等于改接口契约。

与配置的分工：

* **部署栈给的**（本模块）：区域/项目、状态存储连接参数、三张表名、
  层级规划、地址规划、轮询与重试窗口、锁 TTL、通知主题、留痕桶。
* **事件给的**（见 :mod:`invocation`）：本次要扩展哪个 VPC、触发原因、
  巡检侧算出的利用率。
* **云端读到的**（见 :mod:`hw_cloud_api`）：VPC 现有网段、子网现状。

本模块不做任何网络调用，也不打印任何凭据；
所有数值型参数都带解析兜底，避免单个空串把整条链路打死。
"""

import json
import math

from handler_errors import ConfigError

# 必填键：缺任何一个都无法完成一次扩容
_REQUIRED_KEYS = (
    "HW_REGION",
    "HW_PROJECT_ID",
    "DDB_ENDPOINT",
    "DDB_PORT",
    "DDB_USER_NAME",
    "DDB_PASSWORD",
    "LOCK_TABLE_NAME",
    "CONFIG_TABLE_NAME",
    "HISTORY_TABLE_NAME",
    "TIER_PLAN",
    "SECONDARY_CIDR_PLAN",
)

# 华为云子网掩码约束：前缀长度不能大于 28
_MAX_SUBNET_PREFIX = 28
_MIN_SUBNET_PREFIX = 8


def _get(environ, key):
    value = environ.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _as_int(environ, key, default=None, minimum=None, maximum=None):
    """把环境变量解析成整数，非法值直接报错而不是悄悄用默认值。

    轮询窗口、重试预算这类参数被写错会改变链路行为，
    静默兜底比启动失败更危险，因此这里选择显式失败。
    """
    raw = _get(environ, key)
    if raw is None:
        if default is None:
            raise ConfigError("缺少必需的数值型配置", key=key)
        value = int(default)
    else:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            raise ConfigError("配置不是合法整数", key=key, value=raw)
    if minimum is not None and value < minimum:
        raise ConfigError("配置低于允许下限", key=key, value=value, minimum=minimum)
    if maximum is not None and value > maximum:
        raise ConfigError("配置高于允许上限", key=key, value=value, maximum=maximum)
    return value


def _as_float(environ, key, default):
    raw = _get(environ, key)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ConfigError("配置不是合法数值", key=key, value=raw)


def _as_json_list(environ, key):
    raw = _get(environ, key)
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ConfigError("配置不是合法 JSON", key=key)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ConfigError("配置 JSON 的顶层必须是数组", key=key)
    return parsed


class Tier(object):
    """一个扩容层级：一组同掩码、同可用区集合的子网。

    ``security_group_id`` 只用于留痕与通知——华为云安全组绑定到端口而非子网，
    新建子网不需要、也无法在此处绑定安全组；
    新网段的内网互访由地址组成员承载。
    """

    def __init__(self, tier_name, sample_name, availability_zones, netmask,
                 security_group_id=None):
        self.tier_name = tier_name
        self.sample_name = sample_name
        self.availability_zones = list(availability_zones or [])
        self.netmask = int(netmask)
        self.security_group_id = security_group_id

    @classmethod
    def from_plan(cls, item, default_netmask):
        if not isinstance(item, dict):
            raise ConfigError("TIER_PLAN 的元素必须是对象", value=item)
        tier_name = item.get("tier_name")
        sample_name = item.get("sample_name")
        if not tier_name or not sample_name:
            raise ConfigError("TIER_PLAN 的元素缺少 tier_name / sample_name", value=item)
        zones = item.get("availability_zones") or []
        if isinstance(zones, str):
            zones = [zones]
        if not zones:
            raise ConfigError("TIER_PLAN 的层级没有可用区", tier=tier_name)
        netmask = item.get("netmask")
        netmask = default_netmask if netmask in (None, "") else int(netmask)
        if not _MIN_SUBNET_PREFIX <= netmask <= _MAX_SUBNET_PREFIX:
            raise ConfigError(
                "层级掩码超出可用范围（华为云子网前缀长度不得大于 28）",
                tier=tier_name, netmask=netmask)
        return cls(tier_name, sample_name, zones, netmask, item.get("security_group_id"))

    def subnet_slots(self):
        return len(self.availability_zones)

    def addresses_per_subnet(self):
        return 2 ** (32 - self.netmask)

    def to_dict(self):
        return {
            "tier_name": self.tier_name,
            "sample_name": self.sample_name,
            "availability_zones": self.availability_zones,
            "netmask": self.netmask,
            "security_group_id": self.security_group_id,
        }


class RuntimeConfig(object):
    """扩容函数的运行期配置视图。"""

    def __init__(self, environ=None):
        env = dict(environ) if environ is not None else _os_environ()
        missing = [key for key in _REQUIRED_KEYS if _get(env, key) is None]
        if missing:
            raise ConfigError("环境变量缺失，无法启动一次扩容", keys=missing)

        self.region = _get(env, "HW_REGION")
        self.project_id = _get(env, "HW_PROJECT_ID")
        self.account_id = _get(env, "HW_ACCOUNT_ID") or "unknown"

        # 状态存储：VPC 内私有地址 + DynamoDB 兼容接口
        self.ddb_endpoint = _normalize_endpoint(_get(env, "DDB_ENDPOINT"), _get(env, "DDB_PORT"))
        self.ddb_username = _get(env, "DDB_USER_NAME")
        self.ddb_password = _get(env, "DDB_PASSWORD")
        self.lock_table = _get(env, "LOCK_TABLE_NAME")
        self.config_table = _get(env, "CONFIG_TABLE_NAME")
        self.history_table = _get(env, "HISTORY_TABLE_NAME")

        # 网络定位与被扩展对象
        self.target_vpc_id = _get(env, "TARGET_VPC_ID")
        self.address_group_id = _get(env, "ADDRESS_GROUP_ID")
        self.default_route_table_id = _get(env, "DEFAULT_ROUTE_TABLE_ID")
        self.notification_topic_urn = _get(env, "NOTIFICATION_TOPIC_URN")

        # 规划与阈值
        self.secondary_cidr_plan = _as_json_list(env, "SECONDARY_CIDR_PLAN")
        self.cidr_max_count = _as_int(env, "CIDR_MAX_COUNT", default=5, minimum=1, maximum=20)
        self.subnet_netmask = _as_int(
            env, "SUBNET_NETMASK", default=24, minimum=_MIN_SUBNET_PREFIX,
            maximum=_MAX_SUBNET_PREFIX)
        self.warning_threshold = _as_float(env, "WARNING_THRESHOLD", 80.0)
        self.tier_plan = [
            Tier.from_plan(item, self.subnet_netmask) for item in _as_json_list(env, "TIER_PLAN")
        ]
        if not self.tier_plan:
            raise ConfigError("TIER_PLAN 为空，没有任何可扩展的层级")

        # 留痕
        self.obs_bucket = _get(env, "OBS_BUCKET")
        self.obs_endpoint = _get(env, "OBS_ENDPOINT")
        self.history_prefix = _get(env, "HISTORY_OBJECT_PREFIX") or "expansion-history"

        # 时序参数
        self.lock_ttl_seconds = _as_int(env, "LOCK_TTL_SECONDS", default=900, minimum=60)
        self.poll_interval_seconds = _as_int(env, "POLL_INTERVAL_SECONDS", default=10, minimum=1)
        self.poll_deadline_seconds = _as_int(env, "POLL_DEADLINE_SECONDS", default=600, minimum=10)
        self.subnet_retry_attempts = _as_int(env, "SUBNET_RETRY_ATTEMPTS", default=3, minimum=1)
        self.subnet_retry_interval = _as_int(env, "SUBNET_RETRY_INTERVAL", default=30, minimum=1)

        if self.poll_deadline_seconds < self.poll_interval_seconds:
            raise ConfigError(
                "轮询总时长必须不小于轮询间隔",
                poll_deadline_seconds=self.poll_deadline_seconds,
                poll_interval_seconds=self.poll_interval_seconds)

    # ------------------------------------------------------------------
    def required_addresses(self):
        """一次「全层级铺开」需要的地址总数：每层每个可用区一个子网。"""
        return sum(tier.subnet_slots() * tier.addresses_per_subnet()
                   for tier in self.tier_plan)

    def tightest_netmask(self):
        """层级里最小的前缀长度（最大的子网），用于校验规划网段是否装得下。"""
        return min(tier.netmask for tier in self.tier_plan)

    def plan_capacity(self, cidr):
        """规划网段可提供的地址数；非法网段返回 0。"""
        try:
            prefix = int(str(cidr).split("/")[1])
        except (IndexError, TypeError, ValueError):
            return 0
        if prefix > 32:
            return 0
        return 2 ** (32 - prefix)

    def fits_plan(self, cidr):
        """规划网段是否足以承载全部层级的子网（不足以提示，不阻断执行）。"""
        return self.plan_capacity(cidr) >= self.required_addresses()

    def netmask_for_capacity(self, addresses):
        """能装下 ``addresses`` 个地址的最小前缀长度（向上取整）。"""
        if addresses <= 1:
            return 32
        return 32 - int(math.ceil(math.log(addresses, 2)))

    def describe(self):
        """可安全落盘的配置视图：不含任何凭据。"""
        return {
            "region": self.region,
            "project_id": self.project_id,
            "account_id": self.account_id,
            "target_vpc_id": self.target_vpc_id,
            "address_group_id": self.address_group_id,
            "default_route_table_id": self.default_route_table_id,
            "notification_topic_urn": self.notification_topic_urn,
            "tables": [self.lock_table, self.config_table, self.history_table],
            "secondary_cidr_plan": self.secondary_cidr_plan,
            "cidr_max_count": self.cidr_max_count,
            "subnet_netmask": self.subnet_netmask,
            "warning_threshold": self.warning_threshold,
            "tiers": [tier.to_dict() for tier in self.tier_plan],
            "obs_bucket": self.obs_bucket,
            "obs_endpoint": self.obs_endpoint,
            "history_prefix": self.history_prefix,
            "lock_ttl_seconds": self.lock_ttl_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "poll_deadline_seconds": self.poll_deadline_seconds,
            "subnet_retry_attempts": self.subnet_retry_attempts,
            "subnet_retry_interval": self.subnet_retry_interval,
        }

    @classmethod
    def load(cls, environ=None):
        return cls(environ)


def _normalize_endpoint(raw, port):
    """把 ``DDB_ENDPOINT`` 归一成带协议的基地址。

    栈下发的是 GeminiDB 负载均衡的私有 IP，没有协议前缀；
    该接口在 VPC 内以明文 HTTP 提供服务（开启 SSL 属实例侧额外开关），
    因此缺协议时补 ``http://``，已带协议时原样使用。
    """
    value = str(raw).strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    host = value
    if port and not host.endswith(":%s" % port):
        host = "%s:%s" % (host, port)
    return "http://%s" % host


def _os_environ():
    import os

    return os.environ
