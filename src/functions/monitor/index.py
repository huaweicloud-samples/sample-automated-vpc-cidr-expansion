"""巡检函数的调用入口：``index.main_handler``（``var.monitor_handler`` 的默认值）。

一轮巡检做四件事，全部只读云资源，唯一的写动作是投递一条扩容指令：

1. 按标签发现被纳管的 VPC（:mod:`inspector`）；
2. 逐个算出地址利用率、可用地址、总容量与子网数，并把观测值写回配置表；
3. 把四条序列上报到监控服务的自定义命名空间（:mod:`metrics`，大盘从这里取数）；
4. 对达到触发线的 VPC 投递一条扩容指令（:mod:`dispatcher`），并在静默窗口内不重复投。

失败口径与扩容函数不同，这里是刻意分开的：

* **发现阶段失败**（VPC 列表读不到）：整轮没有任何结论，原样抛出。
  只有函数执行失败，平台的异步调用配置才会把事件投进死信主题，
  这是运维看到「巡检停了」的唯一信号，不能被静默吞掉。
* **单个 VPC 失败**：只影响它自己，其余对象照常观测、上报与判定；
  一轮里有多少个失败会出现在返回结果与快照里。
* **观测写回 / 指标上报 / 快照落盘失败**：都不改变本轮结论，
  它们是旁路与展示面，不是判定依据。

凭据只来自调用上下文的委托临时三元组；日志、指标与快照都不含任何密钥，
配置视图走 :func:`monitor_conf.MonitorConfig.describe` 这条「不含凭据」的通道。
"""

import json
import logging

from dispatcher import CommandDispatcher, decide
from hw_cloud_api import CloudApi
from inspector import discover, observe
from invocation import execution_id, function_name, resolve_credentials
from metrics import report as report_metrics
from monitor_conf import MonitorConfig
from snapshot import SnapshotWriter
from state_store import StateStore

LOGGER_NAME = "vpc-cidr-monitor"


def main_handler(event, context=None):
    """FunctionGraph 入口。

    :returns: 本轮结论（纳管数量、判定分布、上报与投递情况）
    :raises Exception: 发现阶段失败；此时本轮没有任何结论，
                       由平台的异步配置投递死信
    """
    return MonitorInvocation(event, context).run()


class MonitorInvocation(object):
    """一轮巡检的装配与执行。"""

    def __init__(self, event, context=None):
        self.event = event
        self.context = context
        self.logger = get_logger(function_name(context))
        self.config = None
        self.credentials = None
        self.api = None
        self.dispatcher = None
        self.snapshot = None
        self.execution = execution_id(context)
        self.trigger = _trigger_of(event)

    # ------------------------------------------------------------------
    def run(self):
        self._bootstrap()
        monitored = discover(self.api, self.config, self.logger)

        if not monitored:
            summary = self._empty_summary(monitored)
            self._finish(summary)
            self.logger.info("monitor_round %s", json.dumps(summary, default=str, ensure_ascii=False))
            return summary

        self.dispatcher.ensure_table()
        decisions = []
        for vpc in monitored:
            observation = observe(self.api, vpc, self.config, self.logger)
            self.dispatcher.record_observation(observation)
            since = self.dispatcher.seconds_since_dispatch(observation.get("vpc_id"))
            decisions.append(decide(observation, self.config, since))

        summary = self._summarize(self._empty_summary(monitored), decisions)
        summary["metrics_reported"] = report_metrics(
            self.api, [item.observation for item in decisions], self.config, self.logger)
        summary["dispatches"] = self._dispatch_all(decisions)

        self._finish(summary)
        self.logger.info("monitor_round %s", json.dumps(summary, default=str, ensure_ascii=False))
        return summary

    # ------------------------------------------------------------------
    def _bootstrap(self):
        self.config = MonitorConfig.load()
        self.credentials = resolve_credentials(
            self.context, project_id_hint=self.config.project_id)
        if self.credentials.source != "agency-context":
            self.logger.warning("monitor_local_credentials 凭据来自本地联调配置，"
                                "仅用于本地跑通逻辑（值不打印）")
        self.api = CloudApi(self.credentials, self.config.region,
                            self.config.project_id or self.credentials.project_id, self.logger)
        store = StateStore(self.config.ddb_endpoint, self.config.ddb_username,
                           self.config.ddb_password, self.config.region, self.logger)
        self.dispatcher = CommandDispatcher(self.api, store, self.config, self.logger)
        self.snapshot = SnapshotWriter(self.config, self.logger)
        self.logger.info("monitor_begin %s", json.dumps({
            "execution_id": self.execution,
            "trigger": self.trigger,
            "credential_source": self.credentials.source,
            "config": self.config.describe(),
        }, default=str, ensure_ascii=False))

    def _empty_summary(self, monitored):
        summary = {
            "execution_id": self.execution,
            "trigger": self.trigger,
            "monitored_count": len(monitored),
            "observed": 0,
            "observe_failed": 0,
            "metrics_reported": None,
            "decisions": [],
            "dispatches": [],
        }
        if not monitored:
            summary["detail"] = "没有命中纳管标签的 VPC，本轮不做任何判定"
        return summary

    def _summarize(self, summary, decisions):
        counts = {}
        for item in decisions:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        summary.update({
            "observed": sum(1 for item in decisions if item.observation.get("state")),
            "observe_failed": sum(1 for item in decisions if item.observation.get("error")),
            "decision_counts": counts,
            "decisions": [item.to_dict() for item in decisions],
            "threshold": self.config.utilization_threshold,
        })
        return summary

    def _dispatch_all(self, decisions):
        dispatched = []
        for item in decisions:
            if not item.should_dispatch:
                continue
            result = self.dispatcher.dispatch(item.observation, self.execution)
            if result.get("dispatched"):
                self.dispatcher.record_dispatch(item.observation, self.execution)
            dispatched.append({
                "vpc_id": result.get("vpc_id"),
                "dispatched": bool(result.get("dispatched")),
                "detail": result.get("detail"),
            })
        return dispatched

    def _finish(self, summary):
        """快照落盘：失败只记日志，不影响本轮结论。"""
        try:
            summary["snapshot"] = self.snapshot.write(summary, self.credentials, self.execution)
        except Exception as error:
            self.logger.warning("snapshot_failed detail=%s", error)
            summary["snapshot"] = {"written": False, "detail": str(error)}


# ----------------------------------------------------------------------
def _decide(observation, config, seconds_since_dispatch):
    """延迟导入判定函数：让入口模块的依赖清单保持在一处可见。"""
    from dispatcher import decide

    return decide(observation, config, seconds_since_dispatch)


def _trigger_of(event):
    """定时触发器与直接调用都归一成同一个来源标识，只用于日志与快照。"""
    if isinstance(event, dict):
        for field in ("trigger_type", "TriggerSource", "source"):
            if event.get(field):
                return str(event[field])
        if event.get("timer"):
            return "TIMER"
    return "scheduled"


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
