"""巡检函数的运行期配置：只读部署栈下发的键，键名由 ``infra/locals.tf`` 的
``monitor_user_data`` 定义，改键名等于改接口契约。

与扩容函数侧的分工：

* **本模块**：定位信息（区域/项目/账号）、状态存储连接参数、标签筛选条件、
  两个阈值、监控命名空间与维度名、指令主题、配置表名、留痕桶、地址规划。
* **云端读到的**（见 :mod:`inspector`）：被纳管 VPC 的网段与子网现状。

本模块不做任何网络调用，也不打印任何凭据；``describe()`` 是唯一可安全落盘的视图。
"""

import json

from handler_errors import ConfigError

#: 必填键：缺任何一个都无法完成一轮巡检
_REQUIRED_KEYS = (
    "HW_REGION",
    "HW_PROJECT_ID",
    "DDB_ENDPOINT",
    "DDB_USER_NAME",
    "DDB_PASSWORD",
    "MONITOR_TAG_KEY",
    "MONITOR_TAG_VALUE",
    "CES_NAMESPACE",
    "CES_DIMENSION_NAME",
)

#: 两个阈值之间的大小约束：触发线必须不低于二次校验的下限线
_MIN_THRESHOLD = 0.0
_MAX_THRESHOLD = 100.0


def _get(environ, key):
    value = environ.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


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
        return [parsed]
    if not isinstance(parsed, list):
        raise ConfigError("配置 JSON 的顶层必须是数组", key=key)
    return [str(item) for item in parsed]


class MonitorConfig(object):
    """一轮巡检所需的全部运行期参数。"""

    def __init__(self, environ=None):
        env = dict(environ) if environ is not None else _os_environ()
        missing = [key for key in _REQUIRED_KEYS if _get(env, key) is None]
        if missing:
            raise ConfigError("环境变量缺失，无法开始一轮巡检", keys=missing)

        self.region = _get(env, "HW_REGION")
        self.project_id = _get(env, "HW_PROJECT_ID")
        self.account_id = _get(env, "HW_ACCOUNT_ID") or "unknown"

        # 状态存储：VPC 内私有地址 + DynamoDB 兼容接口
        self.ddb_endpoint = normalize_ddb_endpoint(_get(env, "DDB_ENDPOINT"), _get(env, "DDB_PORT"))
        self.ddb_username = _get(env, "DDB_USER_NAME")
        self.ddb_password = _get(env, "DDB_PASSWORD")
        self.config_table = _get(env, "CONFIG_TABLE_NAME")

        # 被纳管对象的筛选条件
        self.tag_key = _get(env, "MONITOR_TAG_KEY")
        self.tag_value = _get(env, "MONITOR_TAG_VALUE")

        # 阈值：触发线 + 二次校验下限线
        self.utilization_threshold = _clamped_threshold(
            env, "UTILIZATION_THRESHOLD", 85.0)
        self.warning_threshold = _clamped_threshold(env, "WARNING_THRESHOLD", 80.0)
        if self.utilization_threshold < self.warning_threshold:
            raise ConfigError(
                "触发阈值不得低于二次校验下限，否则巡检侧与扩容侧的判定互相矛盾",
                utilization_threshold=self.utilization_threshold,
                warning_threshold=self.warning_threshold)

        # 监控指标定位：与大盘取数用的命名空间/指标名/维度名逐字一致
        self.metric_namespace = _get(env, "CES_NAMESPACE")
        self.metric_dimension = _get(env, "CES_DIMENSION_NAME")

        # 指令投递
        self.command_topic_urn = _get(env, "COMMAND_TOPIC_URN")

        # 巡检快照的存放位置
        self.obs_bucket = _get(env, "OBS_BUCKET")
        self.obs_endpoint = _get(env, "OBS_ENDPOINT")

        # 地址规划（只用于判断「还有没有可消费的网段」，不写入任何云资源）
        self.secondary_cidr_plan = _as_json_list(env, "SECONDARY_CIDR_PLAN")

    # ------------------------------------------------------------------
    def describes_plan(self, cidr):
        """某个网段是否落在部署栈交付的地址规划内。"""
        return str(cidr) in [str(item) for item in self.secondary_cidr_plan]

    def describe(self):
        """可安全落盘的配置视图：不含任何凭据。"""
        return {
            "region": self.region,
            "project_id": self.project_id,
            "account_id": self.account_id,
            "tag_filter": "%s=%s" % (self.tag_key, self.tag_value),
            "utilization_threshold": self.utilization_threshold,
            "warning_threshold": self.warning_threshold,
            "metric_namespace": self.metric_namespace,
            "metric_dimension": self.metric_dimension,
            "config_table": self.config_table,
            "command_topic_urn": self.command_topic_urn,
            "obs_bucket": self.obs_bucket,
            "obs_endpoint": self.obs_endpoint,
            "secondary_cidr_plan": self.secondary_cidr_plan,
        }

    @classmethod
    def load(cls, environ=None):
        return cls(environ)


def _clamped_threshold(env, key, default):
    value = _as_float(env, key, default)
    if not _MIN_THRESHOLD < value <= _MAX_THRESHOLD:
        raise ConfigError(
            "阈值取值必须在 0-100 之间（百分比）", key=key, value=value)
    return value


def normalize_ddb_endpoint(raw, port):
    """把 ``DDB_ENDPOINT`` 归一成带协议的基地址。

    栈下发的是状态存储负载均衡的私有地址，可能不带协议与端口；
    该接口在 VPC 内以明文 HTTP 提供服务，缺协议时补 ``http://``，已带协议时原样使用。
    与扩容侧 :func:`runtime_conf._normalize_endpoint` 的口径保持一致。
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
