"""超阈值时投递一条扩容指令，并把投递这件事本身记在配置表里。

判定顺序（任何一条不满足就不投，且都会写明原因）：

1. **观测可用**：利用率算得出来（子网余量读不到时不投，宁可不扩也不误扩）；
2. **达到触发线**：``utilization >= UTILIZATION_THRESHOLD``；
3. **规划里还有网段**：``plan_exhausted`` 为真时不投——投了也只会在扩容侧
   变成一次「地址规划已用尽」的失败通知与一封死信邮件；
4. **不在静默窗口内**：距上一次投递超过 :data:`DISPATCH_COOLDOWN_SECONDS`。

第 4 条是防指令风暴的关键：一次扩容最长跑满函数超时，期间巡检还会再来若干轮，
每轮都看到高利用率。巡检侧读不到锁表（锁表不在本函数的配置里），
因此用「自己写下的上一次投递时间」做节流，窗口取值只需大于扩容函数的超时上限。

配置表行的写入一律用 ``UpdateItem`` 的字段级 ``SET``，绝不用整行覆盖：
同一行里 ``warning_threshold`` / ``secondary_cidrs`` / ``tiers`` 三个字段是
「按 VPC 覆盖规划」的载体，由运维手工写入、扩容函数读取，
巡检函数只观测它们、永远不改写它们。
"""

from datetime import datetime, timezone

#: 投递静默窗口（秒）。取值依据：扩容函数单次调用最长 900 秒
#: （``var.expansion_timeout``），窗口必须覆盖一整次扩容，否则扩容还没跑完
#: 巡检就会再投一条，锁表只让第一次拿到锁、后面的都变成一次无谓的跳过。
DISPATCH_COOLDOWN_SECONDS = 1800

#: 指令里的触发原因标识，进扩容函数的留痕与通知文案
COMMAND_REASON = "utilization"

#: 巡检自己维护的配置表字段（其余字段属于运维，不在本模块的写范围内）
OWNED_FIELDS = (
    "utilization",
    "total_capacity",
    "available_ips",
    "subnet_count",
    "cidr_count",
    "next_cidr",
    "plan_exhausted",
    "monitored",
    "last_inspected_at",
    "last_dispatch_at",
    "last_dispatch_execution",
    "dispatch_count",
)


class Decision(object):
    """一个 VPC 在本轮的判定结论。"""

    def __init__(self, observation, should_dispatch, reason):
        self.observation = observation
        self.should_dispatch = bool(should_dispatch)
        self.reason = reason

    def to_dict(self):
        return {
            "vpc_id": self.observation.get("vpc_id"),
            "vpc_name": self.observation.get("vpc_name"),
            "utilization": self.observation.get("utilization"),
            "next_cidr": self.observation.get("next_cidr"),
            "dispatched": self.should_dispatch,
            "reason": self.reason,
        }


def decide(observation, config, seconds_since_dispatch=None):
    """按上面的四条顺序给出判定，不做任何写入。"""
    if observation.get("error"):
        return Decision(observation, False, "observe_failed")
    if observation.get("utilization") is None:
        return Decision(observation, False, "utilization_unknown")
    if observation.get("utilization") < config.utilization_threshold:
        return Decision(observation, False, "below_threshold")
    if observation.get("plan_exhausted"):
        return Decision(observation, False, "plan_exhausted")
    if seconds_since_dispatch is not None and seconds_since_dispatch < DISPATCH_COOLDOWN_SECONDS:
        return Decision(observation, False, "within_cooldown")
    return Decision(observation, True, "above_threshold")


class CommandDispatcher(object):
    """配置表读写 + 指令投递。"""

    def __init__(self, api, store, config, logger=None):
        self.api = api
        self.store = store
        self.config = config
        self.logger = logger

    # ------------------------------------------------------------------
    # 配置表
    # ------------------------------------------------------------------
    def ensure_table(self):
        """配置表由巡检函数创建：它承载「按 VPC 覆盖规划」，必须先于任何读写存在。"""
        if not self.config.config_table:
            return {"created": False, "reason": "CONFIG_TABLE_NAME 未配置"}
        try:
            from state_store import key_schema

            result = self.store.ensure_table(self.config.config_table, key_schema("vpc_id"))
        except Exception as error:
            # 表建不出来不影响观测与指标，只影响「按 VPC 覆盖」与节流
            self._log("warning", "config_table_unavailable detail=%s", error)
            return {"created": False, "error": str(error)}
        self._log("info", "config_table_ensured table=%s created=%s",
                  self.config.config_table, result.get("created"))
        return result

    def seconds_since_dispatch(self, vpc_id):
        """读上一次投递的时间戳；读不到或解析不了都当作「从未投递」。

        静默窗口失效的后果是多投一条指令，而扩容函数自己有 VPC 级锁兜底，
        所以这里宁可宽松也不因一次脏数据卡死整条自动化。
        """
        row = self.row(vpc_id)
        if not row:
            return None
        return seconds_since(row.get("last_dispatch_at"), _now())

    def row(self, vpc_id):
        if not self.config.config_table:
            return None
        try:
            row = self.store.get_item(self.config.config_table, {"vpc_id": vpc_id})
        except Exception as error:
            self._log("warning", "config_row_unread vpc_id=%s detail=%s", vpc_id, error)
            return None
        return row if isinstance(row, dict) else None

    def record_observation(self, observation):
        """把本轮观测写回配置表（只动自己维护的字段）。"""
        if not self.config.config_table:
            return {"written": False, "reason": "CONFIG_TABLE_NAME 未配置"}
        vpc_id = observation.get("vpc_id")
        if not vpc_id:
            return {"written": False, "reason": "missing vpc_id"}

        values = {
            "utilization": observation.get("utilization"),
            "total_capacity": observation.get("total_capacity"),
            "available_ips": observation.get("available_ips"),
            "subnet_count": observation.get("subnet_count"),
            "cidr_count": observation.get("cidr_count"),
            "next_cidr": observation.get("next_cidr"),
            "plan_exhausted": bool(observation.get("plan_exhausted")),
            "monitored": True,
            "last_inspected_at": _utc_now(),
        }
        expression = ", ".join("%s = :%s" % (name, name) for name in sorted(values))
        extra = sorted(set(values) - set(OWNED_FIELDS))
        if extra:
            # 守护：多写一个字段就可能覆盖运维维护的「按 VPC 覆盖规划」
            self._log("error", "config_field_not_owned fields=%s", extra)
            return {"written": False, "reason": "字段不在巡检的写范围内", "fields": extra}
        try:
            self.store.update_item(
                self.config.config_table,
                {"vpc_id": vpc_id},
                update_expression="SET " + expression,
                expression_attribute_values={(":%s" % name): value
                                             for name, value in values.items()},
            )
        except Exception as error:
            # 写不回观测值不影响指标与指令，本轮判定照常完成
            self._log("warning", "config_row_write_failed vpc_id=%s detail=%s", vpc_id, error)
            return {"written": False, "error": str(error)}
        return {"written": True, "fields": len(values)}

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    def dispatch(self, observation, execution_id=None):
        """向指令主题投递一条扩容请求。

        消息体只带扩容函数需要的三个最小字段（``vpc_id`` / ``reason`` /
        ``utilization``），其余是给人看的上下文；扩容函数绑定的是自己的
        ``TARGET_VPC_ID``，多余字段不会影响它的行为。
        """
        vpc_id = observation.get("vpc_id")
        payload = {
            "vpc_id": vpc_id,
            "reason": COMMAND_REASON,
            "utilization": observation.get("utilization"),
            "requested_by": "cidr-monitor",
            "requested_at": _utc_now(),
            "request_id": execution_id,
            "vpc_name": observation.get("vpc_name"),
            "cidr_count": observation.get("cidr_count"),
            "subnet_count": observation.get("subnet_count"),
            "suggested_cidr": observation.get("next_cidr"),
        }
        if not self.config.command_topic_urn:
            self._log("warning", "command_topic_missing vpc_id=%s", vpc_id)
            return {"dispatched": False, "vpc_id": vpc_id, "detail": "COMMAND_TOPIC_URN 未配置"}

        try:
            self.api.publish_message(
                self.config.command_topic_urn,
                "VPC CIDR expansion request - %s" % vpc_id,
                _dump(payload),
            )
        except Exception as error:
            self._log("error", "command_publish_failed vpc_id=%s detail=%s", vpc_id, error)
            return {"dispatched": False, "vpc_id": vpc_id, "detail": str(error)}

        self._log("info", "command_dispatched %s", _dump(payload))
        return {"dispatched": True, "vpc_id": vpc_id, "payload": payload,
                "topic": self.config.command_topic_urn}

    def record_dispatch(self, observation, execution_id=None):
        """投递成功后写下时间戳与累计次数，作为下一轮的静默窗口依据。"""
        if not self.config.config_table:
            return {"written": False, "reason": "CONFIG_TABLE_NAME 未配置"}
        vpc_id = observation.get("vpc_id")
        stamp = _now()
        try:
            self.store.update_item(
                self.config.config_table,
                {"vpc_id": vpc_id},
                update_expression=(
                    "SET last_dispatch_at = :at, last_dispatch_execution = :exec, "
                    "dispatch_count = if_not_exists(dispatch_count, :zero) + :one"),
                expression_attribute_values={
                    ":at": stamp,
                    ":exec": execution_id,
                    ":zero": 0,
                    ":one": 1,
                },
            )
        except Exception as error:
            # 已经投出去的指令不会因为记账失败而撤回；下一轮可能重投一次，
            # 由扩容函数侧的 VPC 级锁保证不会产生两次真实扩容
            self._log("warning", "dispatch_record_failed vpc_id=%s detail=%s", vpc_id, error)
            return {"written": False, "error": str(error)}
        return {"written": True, "at": stamp}

    def _log(self, level, message, *args):
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message, *args)


# ----------------------------------------------------------------------
def seconds_since(raw, now=None):
    """把配置表里的投递时间换算成「距今多少秒」；解析不了返回 ``None``。"""
    if raw is None:
        return None
    now = now if now is not None else _now()
    if isinstance(raw, (int, float)):
        return max(float(now) - float(raw), 0.0)
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(float(now) - float(text), 0.0)
    except ValueError:
        pass
    try:
        parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(now - parsed.timestamp(), 0.0)


def _dump(mapping):
    import json

    return json.dumps(mapping, default=str, ensure_ascii=False)


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now():
    import time

    return int(time.time())
