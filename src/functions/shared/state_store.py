"""状态存储客户端：锁、每 VPC 配置、扩容历史三张表。

走 GeminiDB 的 DynamoDB 兼容接口（``application/x-amz-json-1.0`` + 请求签名，
签名用的账号口令就是状态存储的数据库账号，见 :mod:`ddb_signer`）。
只用到该接口明确支持的
``DescribeTable`` / ``CreateTable`` / ``PutItem`` / ``GetItem`` / ``UpdateItem``，
不使用事务、备份等该接口不支持的能力。

三张表的键结构：

* ``locks``：主键 ``vpc_id``(S)。属性 ``lock_holder``、``status``、``lock_ttl``(N)、
  ``locked_at``、``released_at``。**TTL 语义不依赖接口**——锁是否过期由本函数
  按 ``lock_ttl`` 与当前时间显式判定。
* ``config``：主键 ``vpc_id``(S)。由巡检函数写入，扩容函数只读，
  用于承载「按 VPC 覆盖规划」：``warning_threshold``、``secondary_cidrs``、``tiers``。
* ``history``：主键 ``vpc_id``(S) + 排序键 ``timestamp``(S)。扩容留痕的一条记录。

表由函数首次运行时按需创建（部署栈只交付实例与网络，不声明表资源）。
"""

import json

import ddb_signer
from handler_errors import ServiceError, StateStoreError, TransportError
from hw_transport import HttpTransport

PROTOCOL_VERSION = "DynamoDB_20120810"
TIMEOUT_STATE_READ = 10
TIMEOUT_STATE_WRITE = 10
RETRY_STATE_READ = 2
RETRY_STATE_WRITE = 0

#: 表不存在时用来判定「需要建表」的服务端错误码片段
_TABLE_MISSING = ("ResourceNotFoundException", "TableNotFound", "table not found")
_CONDITION_FAILED = ("ConditionalCheckFailedException",)

_DEFAULT_CAPACITY = 10


class ConditionNotMet(StateStoreError):
    """条件写未命中：锁已被占用，或锁持有者不是本次执行。"""

    def __init__(self, message, table=None):
        super(ConditionNotMet, self).__init__(message, error_code="ConditionalCheckFailedException")
        self.table = table


class StateStore(object):
    """三张表的读写入口；一次函数调用内复用一个实例。"""

    def __init__(self, endpoint, username, password, region, logger=None, transport=None):
        if not endpoint:
            raise StateStoreError("状态存储地址为空", key="DDB_ENDPOINT")
        self.endpoint = endpoint.rstrip("/")
        self.username = username
        self.password = password
        self.region = region
        self.logger = logger
        self.transport = transport or HttpTransport(logger=logger)
        self._ensured = set()

    # ------------------------------------------------------------------
    # 表
    # ------------------------------------------------------------------
    def ensure_table(self, table_name, key_schema):
        """表存在即返回；不存在则按给定的主键结构创建。

        该接口的 DDL 是同步的，创建后无需等待激活。
        """
        if table_name in self._ensured:
            return {"table": table_name, "created": False, "cached": True}
        try:
            self._call(table_name, "DescribeTable", {"TableName": table_name},
                       timeout=TIMEOUT_STATE_READ, retries=RETRY_STATE_READ)
            self._ensured.add(table_name)
            return {"table": table_name, "created": False}
        except StateStoreError as error:
            if not _is_missing_table(error):
                raise

        attributes = attribute_definitions([spec["AttributeName"] for spec in key_schema])
        body = {
            "TableName": table_name,
            "KeySchema": key_schema,
            # 该接口要求显式声明容量项，不支持省略
            "ProvisionedThroughput": {
                "ReadCapacityUnits": _DEFAULT_CAPACITY,
                "WriteCapacityUnits": _DEFAULT_CAPACITY,
            },
            "AttributeDefinitions": attributes,
        }
        self._call(table_name, "CreateTable", body,
                   timeout=TIMEOUT_STATE_WRITE, retries=RETRY_STATE_WRITE)
        self._ensured.add(table_name)
        if self.logger:
            self.logger.info("state_store table_created=%s", table_name)
        return {"table": table_name, "created": True}

    # ------------------------------------------------------------------
    # 项
    # ------------------------------------------------------------------
    def get_item(self, table_name, key):
        payload = self._call(
            table_name, "GetItem",
            {"TableName": table_name, "Key": dump_item(key), "ConsistentRead": True},
            timeout=TIMEOUT_STATE_READ, retries=RETRY_STATE_READ)
        item = payload.get("Item")
        return load_item(item) if isinstance(item, dict) else None

    def put_item(self, table_name, item, condition_expression=None,
                 expression_attribute_names=None, expression_attribute_values=None):
        """写入一条记录；``condition_expression`` 未命中时抛 :class:`ConditionNotMet`。"""
        body = {"TableName": table_name, "Item": dump_item(item)}
        _attach_condition(body, condition_expression,
                          expression_attribute_names, expression_attribute_values)
        self._call(table_name, "PutItem", body,
                   timeout=TIMEOUT_STATE_WRITE, retries=RETRY_STATE_WRITE)
        return True

    def update_item(self, table_name, key, update_expression, condition_expression=None,
                    expression_attribute_names=None, expression_attribute_values=None):
        body = {
            "TableName": table_name,
            "Key": dump_item(key),
            "UpdateExpression": update_expression,
        }
        _attach_condition(body, condition_expression,
                          expression_attribute_names, expression_attribute_values)
        self._call(table_name, "UpdateItem", body,
                   timeout=TIMEOUT_STATE_WRITE, retries=RETRY_STATE_WRITE)
        return True

    # ------------------------------------------------------------------
    # 内部：一次 DynamoDB 协议调用
    # ------------------------------------------------------------------
    def _call(self, table_name, operation, body, timeout, retries):
        """签名并向接口根路径发一个 JSON 请求。

        ``table_name`` 只用于错误上下文：接口侧的表定位在请求体里，
        出错时把它带上，日志与工单都能直接定位到具体表。
        """
        target = "%s.%s" % (PROTOCOL_VERSION, operation)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = "%s/" % self.endpoint
        headers = ddb_signer.sign(
            method="POST", url=url, target=target, body_bytes=payload,
            username=self.username, password=self.password, region=self.region)
        try:
            response = self.transport.request(
                method="POST", url=url, headers=headers, body=payload,
                timeout=timeout, retries=retries)
        except (TransportError, ServiceError) as error:
            raise _translate(error, table_name, operation)
        data = response.json()
        if self.logger:
            self.logger.info("state_store op=%s table=%s status=%s", operation, table_name,
                             response.status_code)
        return data if isinstance(data, dict) else {}


def key_schema(hash_key, range_key=None):
    """主键结构：一个分区键，可选一个排序键（均为字符串）。"""
    schema = [{"AttributeName": hash_key, "KeyType": "HASH"}]
    if range_key:
        schema.append({"AttributeName": range_key, "KeyType": "RANGE"})
    return schema


def attribute_definitions(names):
    return [{"AttributeName": name, "AttributeType": "S"} for name in names]


# ----------------------------------------------------------------------
# 值映射：Python <-> DynamoDB 属性描述
# ----------------------------------------------------------------------
def dump_value(value):
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, bytes):
        return {"S": value.decode("utf-8", "replace")}
    if isinstance(value, (list, tuple)):
        return {"L": [dump_value(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {key: dump_value(item) for key, item in value.items()}}
    return {"S": str(value)}


def load_value(descriptor):
    if not isinstance(descriptor, dict) or len(descriptor) != 1:
        return descriptor
    (kind, value), = descriptor.items()
    if kind == "S":
        return value
    if kind == "N":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if kind == "BOOL":
        return bool(value)
    if kind == "NULL":
        return None
    if kind == "L":
        return [load_value(item) for item in (value or [])]
    if kind == "M":
        return load_item(value or {})
    return value


def dump_item(item):
    return {key: dump_value(value) for key, value in item.items()}


def load_item(item):
    return {key: load_value(value) for key, value in item.items()}


def dump_values(mapping):
    return {key: dump_value(value) for key, value in (mapping or {}).items()}


# ----------------------------------------------------------------------
def _attach_condition(body, condition_expression, names, values):
    if condition_expression:
        body["ConditionExpression"] = condition_expression
    if names:
        body["ExpressionAttributeNames"] = names
    if values:
        body["ExpressionAttributeValues"] = dump_values(values)


def _is_missing_table(error):
    text = " ".join([str(error), str(getattr(error, "error_code", ""))])
    return any(marker.lower() in text.lower() for marker in _TABLE_MISSING)


def _translate(error, table_name, operation):
    """把传输层/服务层的异常翻译成状态存储侧的语义异常。

    ``ConditionalCheckFailedException`` 不是故障而是**条件未命中**，
    必须翻成 :class:`ConditionNotMet`，否则取锁竞争会被当成链路失败。
    """
    body = getattr(error, "body", None)
    error_code = _error_code_of(body) or getattr(error, "error_code", None)
    if error_code and any(marker in str(error_code) for marker in _CONDITION_FAILED):
        return ConditionNotMet("条件写未命中", table=table_name)
    if isinstance(error, TransportError):
        return StateStoreError(
            "状态存储不可达：请确认函数子网与实例安全组已互通",
            error_code="transport", status_code=None,
            body=_clip(str(getattr(error, "fields", error))))
    return StateStoreError(
        "状态存储调用失败",
        error_code=error_code,
        status_code=getattr(error, "status_code", None),
        body=_clip(body) or _clip(str(error)),
    )


def _error_code_of(body):
    if not body:
        return None
    text = body if isinstance(body, str) else str(body)
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("__type") or payload.get("code") or payload.get("Code")
    if not raw:
        return None
    return str(raw).split("#")[-1].split(":")[-1].strip()


def _clip(text, limit=300):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."
