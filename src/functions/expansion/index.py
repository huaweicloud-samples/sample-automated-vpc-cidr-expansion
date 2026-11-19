"""扩容函数的调用入口：``index.main_handler``（``var.expansion_handler`` 的默认值）。

职责边界：本模块只做「装配 + 收尾」，业务序列在 :mod:`expansion_flow`。
收尾顺序固定为「放锁 → 发失败通知 → 抛出」，
并把「什么算调用失败」说清楚：

* **取锁失败**（同一 VPC 已有扩容在跑）不是故障：返回 ``SKIPPED_LOCK_BUSY``，
  不发通知也不抛异常，交给下一个巡检周期；
* **目标 VPC 不符**：返回 ``SKIPPED_TARGET_MISMATCH``——指令主题被复用或
  人工投递错对象时的护栏，因为本函数的层级规划只对着 ``TARGET_VPC_ID`` 设计；
* **其它任何异常**：放锁 → 投失败通知 → 原样再抛出。
  再抛出是必需的：只有函数执行失败，
  ``huaweicloud_fgs_async_invoke_configuration.expansion`` 的 ``on_failure``
  才会把消息投进死信主题，这是运维在控制台看到这次失败的唯一平台机制。

凭据只来自调用上下文的委托临时三元组；日志、通知与留痕都不含任何密钥，
配置视图也走 :func:`runtime_conf.RuntimeConfig.describe` 这条「不含凭据」的通道。
"""

import json
import logging

from expansion_flow import (NOTIFIED_STATUSES, STATUS_FAILED, STATUS_SKIPPED_LOCK,
                            STATUS_SKIPPED_TARGET, ExpansionRunner, ensure_tables)
from expansion_lock import ExpansionLock
from handler_errors import LockUnavailable
from hw_cloud_api import CloudApi
from invocation import function_name, parse_event, resolve_credentials
from notify_text import failure_message, subject, success_message
from runtime_conf import RuntimeConfig
from state_store import StateStore

LOGGER_NAME = "vpc-cidr-expansion"


def main_handler(event, context=None):
    """FunctionGraph 入口。

    :returns: 结构化结果字典（状态、消费的网段、创建的子网、联动与留痕情况）
    :raises ExpansionError: 本次执行判定失败；已放锁、已投失败通知，
                            并由平台的异步配置投递死信
    """
    return ExpansionInvocation(event, context).run()


class ExpansionInvocation(object):
    """一次调用的装配与收尾。"""

    def __init__(self, event, context=None):
        self.event = event
        self.context = context
        self.logger = get_logger(function_name(context))
        self.config = None
        self.request = None
        self.credentials = None
        self.api = None
        self.store = None
        self.lock = None
        self.result = None
        self.error = None

    # ------------------------------------------------------------------
    def run(self):
        try:
            self._bootstrap()
            if not self._target_matches():
                return self.result
            self._acquire()
            self.result = self._execute()
        except LockUnavailable as error:
            self.result = self._lock_busy(error)
        except Exception as error:  # 含 ExpansionError 与未预期异常：一律走失败收尾
            self.error = error

        self._finish()
        if self.error is not None:
            raise self.error
        return self.result

    # ------------------------------------------------------------------
    def _bootstrap(self):
        """读配置、解事件、取凭据、构造服务端点集合。"""
        self.config = RuntimeConfig.load()
        self.request = parse_event(self.event, self.context)
        self.credentials = resolve_credentials(
            self.context, project_id_hint=self.config.project_id)
        if self.credentials.source != "agency-context":
            self.logger.warning("expansion_local_credentials 凭据来自本地联调配置，"
                                "仅用于本地联调（值不打印）")

        self.api = CloudApi(self.credentials, self.config.region,
                            self.config.project_id or self.credentials.project_id, self.logger)
        self.logger.info("expansion_begin %s", json.dumps({
            "execution_id": self.request.execution_id,
            "vpc_id": self.request.vpc_id,
            "reason": self.request.reason,
            "utilization": self.request.utilization,
            "envelope": self.request.envelope,
            "credential_source": self.credentials.source,
            "config": self.config.describe(),
        }, default=str, ensure_ascii=False))

    def _target_matches(self):
        expected = self.config.target_vpc_id
        if not expected or expected == self.request.vpc_id:
            return True
        self.result = {
            "status": STATUS_SKIPPED_TARGET,
            "vpc_id": self.request.vpc_id,
            "expected_vpc_id": expected,
            "execution_id": self.request.execution_id,
            "reason": self.request.reason,
            "lock": {"released": False, "reason": "not_acquired"},
            "detail": "指令指向的 VPC 与本函数绑定的 TARGET_VPC_ID 不一致，未做任何改动",
        }
        self.logger.warning("expansion_target_mismatch vpc_id=%s expected=%s",
                            self.request.vpc_id, expected)
        return False

    def _acquire(self):
        """建表兜底 + 取 VPC 级锁。"""
        self.store = StateStore(
            self.config.ddb_endpoint, self.config.ddb_username, self.config.ddb_password,
            self.config.region, self.logger)
        for table in ensure_tables(self.store, self.config, self.logger):
            if table.get("created"):
                self.logger.info("state_table_created table=%s", table.get("table"))
        self.lock = ExpansionLock(
            self.store, self.config.lock_table, self.request.vpc_id,
            self.request.execution_id, self.config.lock_ttl_seconds, self.logger)
        self.lock.acquire()

    def _execute(self):
        runner = ExpansionRunner(self.config, self.api, self.store, logger=self.logger,
                                 function=function_name(self.context))
        return runner.run(self.request, self.credentials)

    def _lock_busy(self, error):
        self.logger.warning("expansion_lock_busy %s", error)
        return {
            "status": STATUS_SKIPPED_LOCK,
            "vpc_id": self.request.vpc_id if self.request else None,
            "execution_id": self.request.execution_id if self.request else None,
            "reason": getattr(error, "message", str(error)),
            "lock_holder": (getattr(error, "fields", None) or {}).get("lock_holder"),
            "detail": "同一 VPC 已有扩容在执行，本次不动任何资源",
        }

    # ------------------------------------------------------------------
    def _finish(self):
        """放锁 → 通知：先释放资源再对外发声。"""
        # 失败也可能带回了现场：:mod:`expansion_flow` 在「子网全灭」时把结果挂在异常上，
        # 这份结果里有已附加的网段与已创建的子网，是排障最需要的信息
        result = self.result or getattr(self.error, "result", None)
        if result is None:
            # 失败得较早（配置/凭据/取锁之前）：尽量把已知现场拼出来
            result = {
                "status": STATUS_FAILED,
                "vpc_id": self.request.vpc_id if self.request else None,
                "execution_id": self.request.execution_id if self.request else None,
                "reason": getattr(self.request, "reason", None),
            }
        result["lock"] = self._release()
        self.result = result
        self._notify(result)

    def _release(self):
        if self.lock is None:
            return {"released": False, "reason": "not_acquired"}
        try:
            return self.lock.release()
        except Exception as error:  # 放锁本身设计上不抛，这里只兜住意外
            self.logger.error("lock_release_raised detail=%s", error)
            return {"released": False, "reason": "release_raised", "detail": str(error)}

    def _notify(self, result):
        """把结果投到通知主题；通知失败不改变执行结论。"""
        status = STATUS_FAILED if self.error is not None else result.get("status")
        if self.error is None and status not in NOTIFIED_STATUSES:
            self.logger.info("notification_skipped status=%s", status)
            return
        topic = self.config.notification_topic_urn if self.config else None
        if not topic or self.api is None:
            self.logger.warning("notification_unavailable 主题未配置或凭据/客户端未就绪")
            return

        if self.error is not None:
            text = failure_message(self.error, self.request, self.config, result)
            self.logger.error("expansion_failed %s", text.strip())
        else:
            text = success_message(result, self.config)
        try:
            self.api.publish_message(
                topic,
                subject(status, self.config.account_id, self.config.region),
                text)
            self.logger.info("notification_sent status=%s topic=%s", status, topic)
        except Exception as error:
            # 通知是旁路：它失败不能把一次已完成的扩容（或一次真实故障）改写结论
            self.logger.error("notification_failed status=%s detail=%s", status, error)


# ----------------------------------------------------------------------
def get_logger(tag=None):
    """日志写标准错误，由日志服务按函数的日志流采集。"""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return _Prefixed(logger, tag) if tag else logger


class _Prefixed(object):
    """给每条日志带上函数名，便于多个函数共用日志流时区分来源。"""

    def __init__(self, logger, prefix):
        self._logger = logger
        self._prefix = "function=%s" % prefix

    def _emit(self, level, message, *args):
        # 前缀拼进消息串再交给 logging：直接把 "%s %s" 与业务参数一起传，
        # 会让每条日志都触发 "not all arguments converted" 的 logging error
        getattr(self._logger, level)("%s %s" % (self._prefix, message), *args)

    def debug(self, message, *args):
        self._emit("debug", message, *args)

    def info(self, message, *args):
        self._emit("info", message, *args)

    def warning(self, message, *args):
        self._emit("warning", message, *args)

    def error(self, message, *args):
        self._emit("error", message, *args)
