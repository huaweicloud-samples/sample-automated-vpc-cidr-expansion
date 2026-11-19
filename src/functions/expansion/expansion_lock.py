"""VPC 级分布式锁：把「同一个 VPC 的扩容」串成一条队列。

部署栈已把扩容函数的并发上限压到 1（``expansion_max_instances``），
锁是第二道闸：指令主题可能被重复投递、手工投递与巡检投递并发、
异步失败重投也会叠加，只有锁能保证同一 VPC 同时只有一次扩展在跑。

三个动作的语义：

* ``check``：只读判定，锁过期即视为未占用。
* ``acquire``：条件写（``attribute_not_exists`` 或
  「非 LOCKED」或「TTL 已过期」），命中即拿到锁；条件未命中说明有人在做。
* ``release``：**只有持有者能放锁**，
  条件 ``lock_holder = 本次执行``，避免误放别人的锁。

**锁过期不依赖接口的 TTL 能力**：写入 ``lock_ttl`` 绝对时间戳，
读取时与当前秒级时间比较（GeminiDB 兼容接口不保证 DynamoDB TTL 行为一致）。
"""

import time
from datetime import datetime, timezone

from handler_errors import LockUnavailable
from state_store import ConditionNotMet

STATUS_LOCKED = "LOCKED"
STATUS_RELEASED = "RELEASED"


class ExpansionLock(object):
    """``locks`` 表上的取锁/放锁。"""

    def __init__(self, store, table_name, vpc_id, holder, ttl_seconds=900, logger=None):
        self.store = store
        self.table_name = table_name
        self.vpc_id = vpc_id
        self.holder = holder
        self.ttl_seconds = int(ttl_seconds)
        self.logger = logger
        self.acquired = False

    # ------------------------------------------------------------------
    def check(self, now=None):
        """返回 ``{"is_locked": bool, "lock_holder": str|None, "reason": str}``。"""
        now = int(now if now is not None else time.time())
        item = self.store.get_item(self.table_name, {"vpc_id": self.vpc_id})
        if not item:
            return {"is_locked": False, "lock_holder": None, "reason": "no_record"}
        if item.get("status") != STATUS_LOCKED:
            return {"is_locked": False, "lock_holder": item.get("lock_holder"),
                    "reason": "status_%s" % (item.get("status") or "unknown")}
        expires_at = _as_int(item.get("lock_ttl"))
        if expires_at is None or expires_at <= now:
            return {"is_locked": False, "lock_holder": item.get("lock_holder"),
                    "reason": "expired"}
        return {"is_locked": True, "lock_holder": item.get("lock_holder"),
                "reason": "held_until_%d" % expires_at}

    def acquire(self, now=None):
        """条件写取锁。

        :returns: 取锁成功返回 ``True``
        :raises LockUnavailable: 锁被其他执行持有
        """
        now = int(now if now is not None else time.time())
        status = self.check(now)
        if status["is_locked"] and status["lock_holder"] != self.holder:
            raise LockUnavailable(
                "同一 VPC 已有扩容在执行", vpc_id=self.vpc_id, lock_holder=status["lock_holder"])

        try:
            self.store.put_item(
                self.table_name,
                {
                    "vpc_id": self.vpc_id,
                    "lock_holder": self.holder,
                    "status": STATUS_LOCKED,
                    "lock_ttl": now + self.ttl_seconds,
                    "locked_at": _utc_now(),
                    "requested_by": "expansion-function",
                },
                condition_expression=(
                    "attribute_not_exists(vpc_id) OR #status <> :locked OR #ttl < :now"),
                expression_attribute_names={"#status": "status", "#ttl": "lock_ttl"},
                expression_attribute_values={":locked": STATUS_LOCKED, ":now": now},
            )
        except ConditionNotMet:
            # 与 check 之间存在竞态：别人抢先写了锁。这不是故障，交给下一次投递。
            raise LockUnavailable(
                "取锁条件未命中：并发执行已抢先持有锁", vpc_id=self.vpc_id)

        self.acquired = True
        self._log("info", "lock_acquired holder=%s ttl=%d", self.holder, self.ttl_seconds)
        return True

    def release(self):
        """放锁；只有持有者写得动这把锁。

        :returns: ``{"released": bool, "reason": str}``——放锁失败**不**抛异常，
                  资源已经改动，不能因为放锁失败把整次执行判成失败。
        """
        if not self.acquired:
            return {"released": False, "reason": "not_acquired"}
        try:
            self.store.update_item(
                self.table_name,
                {"vpc_id": self.vpc_id},
                update_expression="SET #status = :released, released_at = :now",
                condition_expression="lock_holder = :holder",
                expression_attribute_names={"#status": "status"},
                expression_attribute_values={
                    ":released": STATUS_RELEASED,
                    ":holder": self.holder,
                    ":now": _utc_now(),
                },
            )
        except ConditionNotMet:
            self._log("warning", "lock_release_skipped holder_mismatch=%s", self.holder)
            return {"released": False, "reason": "holder_mismatch"}
        except Exception as error:  # 状态存储不可达等：留痕后由 TTL 自愈
            self._log("error", "lock_release_failed detail=%s", error)
            return {"released": False, "reason": "state_store_error", "detail": str(error)}

        self.acquired = False
        self._log("info", "lock_released holder=%s", self.holder)
        return {"released": True, "reason": "released"}

    # ------------------------------------------------------------------
    def _log(self, level, message, *args):
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message, *args)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
