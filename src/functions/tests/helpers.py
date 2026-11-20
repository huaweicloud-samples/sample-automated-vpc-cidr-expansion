"""本地替身与配置工厂：让整条链路在纯本地跑起来。

设计约束：

* 每个替身都记录调用清单（``calls``），断言「哪一步真的被调用了几次」而不是只看返回值；
* 失败注入按「方法名 -> 待抛异常队列」给出，用于覆盖云端返回错误与结果未知的分支；
* 替身不实现任何真实协议，也不打开套接字。
"""

import re

import net_plan
from state_store import ConditionNotMet

PLACEHOLDER_ACCESS_KEY = "<HW_ACCESS_KEY>"
PLACEHOLDER_SECRET_KEY = "<HW_SECRET_KEY>"
PLACEHOLDER_SECURITY_TOKEN = "<HW_SECURITY_TOKEN>"
PLACEHOLDER_PROJECT_ID = "<HW_PROJECT_ID>"
PLACEHOLDER_DDB_USER = "<DDB_USER_NAME>"
PLACEHOLDER_DDB_PASSWORD = "<DDB_PASSWORD>"

REGION = "cn-north-4"
VPC_ID = "vpc-example-0001"

#: 一份可直接解析的层级规划：两个层级、每层两个可用区、/28 子网
TIER_PLAN_JSON = (
    '[{"tier_name":"public","sample_name":"app-public-1",'
    '"availability_zones":["cn-north-4a","cn-north-4b"],"netmask":28},'
    '{"tier_name":"private","sample_name":"app-private-1",'
    '"availability_zones":["cn-north-4a","cn-north-4b"],"netmask":28}]'
)
CIDR_PLAN_JSON = '["10.1.0.0/16", "10.2.0.0/16", "192.168.0.0/24"]'

#: 主网段 ``192.168.0.0/24`` 被这两个 /25 整块铺满且余量为零：用例必须先消费
#: 规划中的扩展网段才可能建出新子网，用于覆盖「附加扩展网段」那一步。
FULLY_USED = (("192.168.0.0/25", "app-public-1"), ("192.168.0.128/25", "app-private-1"))


# ----------------------------------------------------------------------
# 运行期配置
# ----------------------------------------------------------------------
def expansion_environ(**overrides):
    """扩容函数 ``RuntimeConfig`` 需要的完整环境变量集合（键名与栈下发一致）。"""
    env = {
        "HW_REGION": REGION,
        "HW_PROJECT_ID": PLACEHOLDER_PROJECT_ID,
        "HW_ACCOUNT_ID": "<HW_ACCOUNT_ID>",
        "DDB_ENDPOINT": "10.0.40.12",
        "DDB_PORT": "8900",
        "DDB_USER_NAME": PLACEHOLDER_DDB_USER,
        "DDB_PASSWORD": PLACEHOLDER_DDB_PASSWORD,
        "LOCK_TABLE_NAME": "demo-locks",
        "CONFIG_TABLE_NAME": "demo-config",
        "HISTORY_TABLE_NAME": "demo-history",
        "TARGET_VPC_ID": VPC_ID,
        "ADDRESS_GROUP_ID": "address-group-demo",
        "DEFAULT_ROUTE_TABLE_ID": "route-table-demo",
        "NOTIFICATION_TOPIC_URN": "urn:smn:cn-north-4:demo:cidr-notify",
        "SECONDARY_CIDR_PLAN": CIDR_PLAN_JSON,
        "TIER_PLAN": TIER_PLAN_JSON,
        "CIDR_MAX_COUNT": "5",
        "SUBNET_NETMASK": "24",
        "WARNING_THRESHOLD": "80",
        "LOCK_TTL_SECONDS": "900",
        "POLL_INTERVAL_SECONDS": "1",
        "POLL_DEADLINE_SECONDS": "10",
        "SUBNET_RETRY_ATTEMPTS": "2",
        "SUBNET_RETRY_INTERVAL": "1",
    }
    env.update({key: value for key, value in overrides.items() if value is not None})
    env.update({key: "" for key, value in overrides.items() if value is False})
    return env


def expansion_config(**overrides):
    from runtime_conf import RuntimeConfig

    return RuntimeConfig(expansion_environ(**overrides))


def monitor_environ(**overrides):
    """巡检函数 ``MonitorConfig`` 需要的完整环境变量集合。"""
    env = {
        "HW_REGION": REGION,
        "HW_PROJECT_ID": PLACEHOLDER_PROJECT_ID,
        "HW_ACCOUNT_ID": "<HW_ACCOUNT_ID>",
        "DDB_ENDPOINT": "10.0.40.12",
        "DDB_PORT": "8900",
        "DDB_USER_NAME": PLACEHOLDER_DDB_USER,
        "DDB_PASSWORD": PLACEHOLDER_DDB_PASSWORD,
        "CONFIG_TABLE_NAME": "demo-config",
        "MONITOR_TAG_KEY": "cidr-expansion",
        "MONITOR_TAG_VALUE": "managed",
        "UTILIZATION_THRESHOLD": "85",
        "WARNING_THRESHOLD": "80",
        "CES_NAMESPACE": "vpc_cidr_expansion.custom",
        "CES_DIMENSION_NAME": "VpcId",
        "COMMAND_TOPIC_URN": "urn:smn:cn-north-4:demo:cidr-command",
        "SECONDARY_CIDR_PLAN": CIDR_PLAN_JSON,
    }
    env.update({key: value for key, value in overrides.items() if value is not None})
    env.update({key: "" for key, value in overrides.items() if value is False})
    return env


def monitor_config(**overrides):
    from monitor_conf import MonitorConfig

    return MonitorConfig(monitor_environ(**overrides))


class FakeCredentials(object):
    """形似 :class:`invocation.Credentials` 的本地凭据对象（全部是占位符）。"""

    def __init__(self, project_id=PLACEHOLDER_PROJECT_ID, source="agency-context",
                 security_token=PLACEHOLDER_SECURITY_TOKEN):
        self.access_key = PLACEHOLDER_ACCESS_KEY
        self.secret_key = PLACEHOLDER_SECRET_KEY
        self.security_token = security_token
        self.project_id = project_id
        self.source = source


class FakeContext(object):
    """FunctionGraph 调用上下文的最小替身。"""

    def __init__(self, request_id="exec-demo-0001", name="vpc-cidr-expansion",
                 with_credentials=True, project_id=PLACEHOLDER_PROJECT_ID):
        self._request_id = request_id
        self._name = name
        self._with_credentials = with_credentials
        self._project_id = project_id

    def getRequestId(self):
        return self._request_id

    def getFuncname(self):
        return self._name

    def getProjectId(self):
        return self._project_id

    def getSecurityAccessKey(self):
        return PLACEHOLDER_ACCESS_KEY if self._with_credentials else None

    def getSecuritySecretKey(self):
        return PLACEHOLDER_SECRET_KEY if self._with_credentials else None

    def getSecurityToken(self):
        return PLACEHOLDER_SECURITY_TOKEN if self._with_credentials else None


# ----------------------------------------------------------------------
# 状态存储替身
# ----------------------------------------------------------------------
class FakeStore(object):
    """三张表的最小内存实现，并记录每次写入用的表达式。

    :param rows: ``{表名: {主键值: 行}}`` 初始数据
    :param condition_failures: 需要让条件写未命中的 ``(表名, 第几次 put)`` 集合
    """

    def __init__(self, rows=None, tables=None, condition_failures=None, raise_on=None):
        self.rows = {table: dict(values) for table, values in (rows or {}).items()}
        self.tables = set((tables or []) + list(self.rows))
        self.calls = []
        self.updates = []
        self.puts = []
        self._condition_failures = set(condition_failures or ())
        self._raise_on = dict(raise_on or {})

    # -- 表 ------------------------------------------------------------
    def ensure_table(self, table_name, key_schema):
        self.calls.append(("ensure_table", table_name))
        if isinstance(self._raise_on.get("ensure_table"), Exception):
            raise self._raise_on["ensure_table"]
        created = table_name not in self.tables
        self.tables.add(table_name)
        self.rows.setdefault(table_name, {})
        return {"table": table_name, "created": created}

    def _table(self, table_name):
        if table_name not in self.tables:
            from handler_errors import StateStoreError

            raise StateStoreError("表不存在", error_code="ResourceNotFoundException")
        return self.rows.setdefault(table_name, {})

    # -- 项 ------------------------------------------------------------
    def get_item(self, table_name, key):
        self.calls.append(("get_item", table_name, dict(key)))
        if isinstance(self._raise_on.get("get_item"), Exception):
            raise self._raise_on["get_item"]
        row = self._table(table_name).get(_key_of(key))
        return dict(row) if row else None

    def put_item(self, table_name, item, condition_expression=None,
                 expression_attribute_names=None, expression_attribute_values=None):
        self.calls.append(("put_item", table_name, _key_of(item)))
        self.puts.append({
            "table": table_name,
            "item": dict(item),
            "condition": condition_expression,
            "values": dict(expression_attribute_values or {}),
        })
        # ``put_item`` 让所有表都失败；``put_item:<表名>`` 只让那一张表失败——
        # 用于「台账写不进去但锁与配置表照常」这类分支，否则故障会落在取锁阶段。
        for gate in ("put_item:%s" % table_name, "put_item"):
            error = self._raise_on.get(gate)
            if isinstance(error, Exception):
                raise error
        if (table_name, len(self.puts)) in self._condition_failures:
            raise ConditionNotMet("条件写未命中", table=table_name)
        self._table(table_name)[_key_of(item)] = dict(item)
        return True

    def update_item(self, table_name, key, update_expression, condition_expression=None,
                    expression_attribute_names=None, expression_attribute_values=None):
        self.calls.append(("update_item", table_name, _key_of(key)))
        self.updates.append({
            "table": table_name,
            "expression": update_expression,
            "condition": condition_expression,
            "values": dict(expression_attribute_values or {}),
        })
        if isinstance(self._raise_on.get("update_item"), Exception):
            raise self._raise_on["update_item"]
        if condition_expression == "lock_holder = :holder":
            row = self._table(table_name).get(_key_of(key)) or {}
            if row.get("lock_holder") != (expression_attribute_values or {}).get(":holder"):
                raise ConditionNotMet("条件写未命中", table=table_name)
        row = self._table(table_name).setdefault(_key_of(key), dict(key))
        _apply_set(row, update_expression, expression_attribute_names,
                   expression_attribute_values)
        return True


_SET_ASSIGNMENT = re.compile(r"([#A-Za-z0-9_]+)\s*=\s*(:[A-Za-z0-9_]+)")


def _apply_set(row, update_expression, names, values):
    """按 ``SET 字段 = :占位`` 落地：占位名先经 ExpressionAttributeNames 还原。

    算术类写法（``if_not_exists(...) + :one``）不在这里模拟，
    相关用例直接断言表达式文本。
    """
    for token, placeholder in _SET_ASSIGNMENT.findall(update_expression or ""):
        if token.upper() in ("SET", "REMOVE", "ADD", "DELETE"):
            continue
        row[(names or {}).get(token, token)] = (values or {}).get(placeholder)


def _key_of(item, fields=("vpc_id", "timestamp")):
    """行标识：主键字段按固定顺序拼接，缺字段就跳过（历史表带排序键）。"""
    parts = [str(item[field]) for field in fields if item.get(field) is not None]
    return "|".join(parts)


# ----------------------------------------------------------------------
# 服务端替身
# ----------------------------------------------------------------------
class FakeApi(object):
    """华为云侧读写的本地替身（网络现状用真实 ``ipaddress`` 语义维护）。"""

    def __init__(self, vpc=None, subnets=None, route_table=None, address_group=None,
                 failures=None, monitored_tags=None):
        self.vpc = dict({"id": VPC_ID, "name": "demo-vpc", "cidr": "10.0.0.0/16",
                         "extend_cidrs": [], "status": "AVAILABLE"}, **(vpc or {}))
        self.subnets = list(subnets or [])
        self.route_table = route_table if route_table is not None else {
            "id": "route-table-demo",
            "routes": [{"destination": "10.0.0.0/8", "type": "peering"},
                       {"destination": "0.0.0.0/0", "type": "default"}],
        }
        self.address_group = dict({"id": "address-group-demo", "name": "demo-group",
                                   "ip_set": ["10.0.0.0/16"], "max_capacity": 1000},
                                  **(address_group or {}))
        self.monitored_tags = monitored_tags or {}
        self._failures = {name: list(items) for name, items in (failures or {}).items()}
        self.calls = []
        self.published = []
        self.reported = []
        self.created = []
        self._sequence = 0

    # -- 失败注入 ------------------------------------------------------
    def _maybe_fail(self, method):
        queue = self._failures.get(method)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    def _next_id(self, prefix):
        self._sequence += 1
        return "%s-%04d" % (prefix, self._sequence)

    # -- VPC ----------------------------------------------------------
    def list_vpcs(self):
        self.calls.append(("list_vpcs",))
        self._maybe_fail("list_vpcs")
        item = {"id": self.vpc["id"], "name": self.vpc.get("name"),
                "cidr": self.vpc.get("cidr"), "status": self.vpc.get("status"),
                "tags": dict(self.monitored_tags)}
        return [item]

    def show_vpc(self, vpc_id):
        self.calls.append(("show_vpc", vpc_id))
        self._maybe_fail("show_vpc")
        return {"id": vpc_id, "name": self.vpc.get("name"), "cidr": self.vpc.get("cidr"),
                "extend_cidrs": list(self.vpc.get("extend_cidrs") or []),
                "status": self.vpc.get("status")}

    def add_extend_cidr(self, vpc_id, cidr):
        self.calls.append(("add_extend_cidr", vpc_id, cidr))
        self._maybe_fail("add_extend_cidr")
        existing = list(self.vpc.get("extend_cidrs") or [])
        if cidr not in existing:
            existing.append(cidr)
        self.vpc["extend_cidrs"] = existing
        return {"ok": True}

    def wait_for_extend_cidr(self, vpc_id, cidr, interval_seconds, deadline_seconds,
                             sleeper=None):
        self.calls.append(("wait_for_extend_cidr", vpc_id, cidr))
        self._maybe_fail("wait_for_extend_cidr")
        if sleeper is not None:
            sleeper(0)
        associated = cidr in (self.vpc.get("extend_cidrs") or [])
        return {"state": "associated" if associated else "timeout",
                "waited_seconds": 0 if associated else int(deadline_seconds),
                "vpc": self.show_vpc(vpc_id)}

    # -- 子网 ----------------------------------------------------------
    def list_subnets(self, vpc_id):
        self.calls.append(("list_subnets", vpc_id))
        self._maybe_fail("list_subnets")
        return [dict(item) for item in self.subnets]

    def create_subnet(self, vpc_id, spec):
        self.calls.append(("create_subnet", vpc_id, spec["name"], spec["cidr"]))
        self._maybe_fail("create_subnet")
        created = dict(spec)
        created["id"] = self._next_id("subnet")
        created["vpc_id"] = vpc_id
        self.subnets.append(dict(created))
        self.created.append(created)
        return created

    def subnet_exists(self, vpc_id, name):
        self.calls.append(("subnet_exists", vpc_id, name))
        self._maybe_fail("subnet_exists")
        for item in self.subnets:
            if item.get("name") == name:
                return dict(item)
        return None

    # -- 路由 / 地址组 --------------------------------------------------
    def show_route_table(self, route_table_id):
        self.calls.append(("show_route_table", route_table_id))
        self._maybe_fail("show_route_table")
        if self.route_table is None:
            from handler_errors import ServiceError

            raise ServiceError("路由表响应缺少 routetable 字段")
        return dict(self.route_table)

    def show_address_group(self, address_group_id):
        self.calls.append(("show_address_group", address_group_id))
        self._maybe_fail("show_address_group")
        return dict(self.address_group)

    def set_address_group_members(self, address_group_id, ip_set):
        self.calls.append(("set_address_group_members", address_group_id, tuple(ip_set)))
        self._maybe_fail("set_address_group_members")
        self.address_group["ip_set"] = list(ip_set)
        return {"ok": True}

    # -- 指标 / 消息 ----------------------------------------------------
    def report_metric_batch(self, samples):
        self.calls.append(("report_metric_batch", len(samples)))
        self._maybe_fail("report_metric_batch")
        self.reported.extend(dict(item) for item in samples)
        return {"ok": True}

    def publish_message(self, topic_urn, subject, message):
        self.calls.append(("publish_message", topic_urn, subject))
        self._maybe_fail("publish_message")
        self.published.append({"topic": topic_urn, "subject": subject, "message": message})
        return {"ok": True}


def subnet(cidr, name, zone="cn-north-4a", available=None, **extra):
    """一条子网记录；``available`` 缺省表示「余量读不到」，用于观测失效分支。"""
    item = {
        "id": "subnet-%s" % name,
        "name": name,
        "cidr": cidr,
        "vpc_id": VPC_ID,
        "availability_zone": zone,
        "gateway_ip": net_plan.gateway_ip(cidr),
        "dhcp_enable": True,
        "primary_dns": "100.125.1.25",
    }
    if available is not None:
        item["available_ip_address_count"] = available
    item.update(extra)
    return item


def vpc_request(vpc_id=VPC_ID, utilization=92.0, reason="utilization",
                execution_id="exec-demo-0001"):
    from invocation import ExpansionRequest

    return ExpansionRequest(vpc_id=vpc_id, reason=reason, utilization=utilization,
                            execution_id=execution_id, envelope="unit-test",
                            raw_event={})
