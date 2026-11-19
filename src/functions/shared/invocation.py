"""一次调用的输入：事件信封、运行凭据与执行标识。

**事件信封**（与消息服务的发布格式绑定）：

1. ``event["record"][0]["smn"]["message"]`` —— SMN 消息触发的标准形态，
   ``message`` 是 **JSON 字符串**，需二次解析；
2. ``event["message"]`` / ``event["Message"]`` / ``event["content"]`` —— 控制台
   「直接发布测试」或函数直调时拿到的裸消息；
3. ``event["vpc_id"]`` —— 手工联调时直接塞进事件的裸字段。

消息体最小字段：``{"vpc_id": "...", "reason": "utilization", "utilization": 87.3}``，
由巡检函数投递。``utilization`` 缺失时，扩容函数会用云端读到的子网余量自行计算。

**运行凭据**只从调用上下文取委托签发的临时三元组
（``getSecurityAccessKey`` / ``getSecuritySecretKey`` / ``getSecurityToken``），
环境变量分支仅用于本地跑通逻辑，值一律是占位符；代码与日志里都不出现长期密钥。
"""

import json
import uuid

from handler_errors import ConfigError

_ENV_ACCESS_KEY = "HW_ACCESS_KEY"
_ENV_SECRET_KEY = "HW_SECRET_KEY"
_ENV_SECURITY_TOKEN = "HW_SECURITY_TOKEN"

_REQUEST_ID_ATTRS = ("requestid", "request_id", "getRequestId", "getrequestid", "requestId")
_FUNCTION_NAME_ATTRS = (
    "funcname", "functionname", "function_name", "getFuncname", "get_current_functionname")


class Credentials(object):
    """委托临时凭据 + 调用定位信息。"""

    def __init__(self, access_key, secret_key, security_token, project_id, source):
        self.access_key = access_key
        self.secret_key = secret_key
        self.security_token = security_token
        self.project_id = project_id
        self.source = source

    def redacted(self):
        return {
            "access_key": _mask(self.access_key),
            "secret_key": "<masked>",
            "security_token": "<masked>" if self.security_token else None,
            "project_id": self.project_id,
            "source": self.source,
        }

    def __repr__(self):
        return "Credentials(%s)" % self.redacted()


class ExpansionRequest(object):
    """解析后的扩容请求。"""

    def __init__(self, vpc_id, reason, utilization, execution_id, envelope, raw_event):
        self.vpc_id = vpc_id
        self.reason = reason
        self.utilization = utilization
        self.execution_id = execution_id
        self.envelope = envelope
        self.raw_event = raw_event

    def to_dict(self):
        return {
            "vpc_id": self.vpc_id,
            "reason": self.reason,
            "utilization": self.utilization,
            "execution_id": self.execution_id,
        }


def parse_event(event, context=None):
    """把任意一种投递形态归一成 :class:`ExpansionRequest`。

    :raises ConfigError: 事件里找不到可用的 ``vpc_id``
    """
    payload = _unwrap(event)
    vpc_id = payload.get("vpc_id") or payload.get("vpcId") or payload.get("VpcId")
    if not vpc_id:
        raise ConfigError(
            "扩容指令缺少 vpc_id", keys=sorted(payload.keys()) if payload else [])
    utilization = payload.get("utilization")
    if utilization is not None:
        try:
            utilization = float(utilization)
        except (TypeError, ValueError):
            utilization = None
    return ExpansionRequest(
        vpc_id=str(vpc_id).strip(),
        reason=payload.get("reason"),
        utilization=utilization,
        execution_id=execution_id(context),
        envelope=payload.get("source") or "unknown",
        raw_event=event,
    )


def resolve_credentials(context=None, environ=None, project_id_hint=None):
    """取出本次调用可用的临时凭据；缺失即失败，绝不回退到内置默认值。"""
    import os

    env = dict(environ) if environ is not None else os.environ
    access_key = _context_get(context, "getSecurityAccessKey")
    secret_key = _context_get(context, "getSecuritySecretKey")
    security_token = _context_get(context, "getSecurityToken")
    source = "agency-context"

    if not access_key or not secret_key:
        source = "environment"
        access_key = access_key or env.get(_ENV_ACCESS_KEY)
        secret_key = secret_key or env.get(_ENV_SECRET_KEY)
        security_token = security_token or env.get(_ENV_SECURITY_TOKEN)

    missing = [name for name, value in (("access_key", access_key), ("secret_key", secret_key))
               if not value]
    if missing:
        raise ConfigError(
            "未取得委托临时凭据：请确认函数已绑定委托（agency），"
            "或在本地联调时提供占位凭据", missing=missing)

    project_id = project_id_hint or env.get("HW_PROJECT_ID") or _context_get(context, "getProjectId")
    if not project_id:
        project_id = _project_id_from_context(context)
    return Credentials(
        access_key=str(access_key),
        secret_key=str(secret_key),
        security_token=str(security_token) if security_token else None,
        project_id=str(project_id) if project_id else None,
        source=source,
    )


def execution_id(context=None):
    """本次执行的唯一标识，用作锁持有者与留痕后缀。"""
    request_id = _context_get(context, *_REQUEST_ID_ATTRS)
    if not request_id:
        request_id = uuid.uuid4().hex
    return str(request_id)


def function_name(context=None):
    return _context_get(context, *_FUNCTION_NAME_ATTRS) or "vpc-cidr-expansion"


# ----------------------------------------------------------------------
def _unwrap(event):
    """按契约的三种形态依次剥壳，返回消息体字典。"""
    if isinstance(event, (bytes, bytearray)):
        event = _loads(event.decode("utf-8", "replace"))
    if isinstance(event, str):
        event = _loads(event)
    if not isinstance(event, dict):
        return {}

    for candidate in _message_candidates(event):
        parsed = _loads(candidate)
        if isinstance(parsed, dict) and (parsed.get("vpc_id") or parsed.get("vpcId")):
            return parsed
        if isinstance(candidate, dict):
            return candidate

    for key in ("vpc_id", "vpcId", "VpcId"):
        if event.get(key):
            return event
    return {}


def _message_candidates(event):
    """产出所有可能的「消息字符串」位置，顺序即优先级。"""
    records = event.get("record") or event.get("Records") or []
    if isinstance(records, dict):
        records = [records]
    for record in records or []:
        if not isinstance(record, dict):
            continue
        for section in ("smn", "Sns", "SMN"):
            block = record.get(section)
            if isinstance(block, dict):
                for field in ("message", "Message", "content"):
                    if block.get(field):
                        yield block[field]
    for section in ("smn", "Sns"):
        block = event.get(section)
        if isinstance(block, dict) and block.get("message"):
            yield block["message"]
    for field in ("message", "Message", "content", "Content"):
        if event.get(field):
            yield event[field]


def _loads(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _context_get(context, *names):
    for name in names:
        getter = getattr(context, name, None)
        if getter is None:
            continue
        if callable(getter):
            try:
                value = getter()
            except TypeError:
                value = None
            except Exception:
                value = None
        else:
            value = getter
        if value:
            return value
    return None


def _project_id_from_context(context):
    """从函数 URN 反解项目 ID：``urn:fss:<region>:<project_id>:function:...``。"""
    urn = _context_get(context, "function_urn", "getFunctionUrn", "func_urn")
    if not urn or not str(urn).startswith("urn:"):
        return None
    parts = str(urn).split(":")
    return parts[3] if len(parts) >= 4 else None


def _mask(value):
    if not value:
        return None
    text = str(value)
    return "****" if len(text) <= 4 else text[:2] + "****" + text[-2:]
