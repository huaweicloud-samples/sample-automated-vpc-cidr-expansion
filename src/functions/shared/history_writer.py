"""留痕：状态表历史行（权威台账）+ 对象存储历史快照（人读的）。

一次扩容的产物由函数自己落两处，没有额外的编排环节代劳。

两处留痕的分工：

* **表行**是权威台账：主键 ``vpc_id`` + ``timestamp``，
  巡检函数与后续排障都按它判断「这个 VPC 扩展过几次、每次消费了哪个网段」。
  写失败**不**回滚已创建的云资源——资源事实已经改变，把整次执行判成失败反而掩盖真相；
  失败会被记进结果与通知，由运维补录。
* **对象**是可读快照：同一份内容加运行期配置视图（不含任何凭据），
  键为 ``<HISTORY_OBJECT_PREFIX>/<vpc_id>/<时间>-<执行编号>.json``，
  由部署栈的生命周期规则按 ``var.history_retention_days`` 自动清理。
  对象存储客户端来自函数运行时的内置 ``obs`` 模块（不打进代码包）；
  内置模块缺失或写入失败都只记日志，不影响主链路。

桶侧只按部署栈交付的策略授权（委托主体 + ``GetObject``/``PutObject``/``ListBucket``），
本模块只追加对象，不读也不改桶配置。
"""

import json
from datetime import datetime, timezone

from state_store import key_schema

#: 历史表主键：分区键 vpc_id + 排序键 timestamp
HISTORY_KEY_SCHEMA = key_schema("vpc_id", "timestamp")


def build_record(result, request, plan, config=None, started_at=None):
    """把一次执行的结果整理成历史行。

    字段是显式列举的：``new_cidr`` / ``secondary_cidr_added`` / ``netmask`` /
    ``subnets_created`` / ``status`` / ``execution_id``；
    华为云侧特有的地址组联动与路由校验作为附加字段。
    """
    timestamp = started_at or _utc_now()
    subnets = result.get("subnets_created") or []
    return {
        "vpc_id": result.get("vpc_id") or request.vpc_id,
        "timestamp": timestamp,
        "execution_id": result.get("execution_id") or request.execution_id,
        "status": result.get("status"),
        "reason": request.reason,
        "trigger_utilization": request.utilization,
        "utilization": result.get("utilization"),
        "mode": result.get("mode"),
        "new_cidr": result.get("new_cidr"),
        "secondary_cidr_added": bool(result.get("secondary_cidr_added")),
        "netmask": result.get("netmask"),
        "cidr_count_after": result.get("cidr_count_after"),
        "subnets_created": [s.get("id") for s in subnets if s.get("id")],
        "subnet_details": subnets,
        "subnets_failed": result.get("subnets_failed") or [],
        "skipped_plan": result.get("skipped_plan") or [],
        "address_group": result.get("address_group"),
        "route_check": result.get("route_check"),
        "state_before": result.get("state_before"),
        "state_after": result.get("state_after"),
        "tiers": [tier.to_dict() for tier in (plan.get("tiers") or [])],
        "warning_threshold": plan.get("warning_threshold"),
        "plan_source": plan.get("source"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "function": result.get("function"),
        "recorded_at": _utc_now(),
    }


def object_key(record, config):
    """历史对象键：前缀 / VPC / 时间-执行编号.json（时间里的 ``:`` 换成 ``-``）。"""
    prefix = (getattr(config, "history_prefix", None) or "expansion-history").strip("/")
    stamp = str(record.get("timestamp") or _utc_now()).replace(":", "-")
    return "%s/%s/%s-%s.json" % (prefix, record.get("vpc_id"), stamp, record.get("execution_id"))


def snapshot(record, config=None):
    """历史对象正文：表行 + 可安全落盘的配置视图（不含凭据）。"""
    body = dict(record)
    if config is not None:
        body["runtime_config"] = config.describe()
    return json.dumps(body, indent=2, sort_keys=True, default=str, ensure_ascii=False)


class HistoryWriter(object):
    """历史表与历史对象的写入入口。"""

    def __init__(self, store, table_name, config=None, logger=None):
        self.store = store
        self.table_name = table_name
        self.config = config
        self.logger = logger

    # ------------------------------------------------------------------
    def ensure(self):
        """历史表按需创建（部署栈只交付实例与网络，不声明表资源）。"""
        try:
            return self.store.ensure_table(self.table_name, HISTORY_KEY_SCHEMA)
        except Exception as error:  # 建表失败不阻断扩容：资源改动优先
            self._log("error", "history_table_ensure_failed detail=%s", error)
            return {"table": self.table_name, "created": False, "error": str(error)}

    def write(self, record):
        """写表行。

        :returns: ``True`` 表示台账已落地；失败返回 ``False`` 并记日志，不抛异常
        """
        try:
            self.store.put_item(self.table_name, record)
        except Exception as error:
            self._log("error", "history_table_write_failed detail=%s", error)
            return False
        self._log("info", "history_table_written table=%s vpc_id=%s status=%s",
                  self.table_name, record.get("vpc_id"), record.get("status"))
        return True

    def write_object(self, record, credentials):
        """写 OBS 历史快照；任何异常都只记日志。

        :returns: ``{"written": bool, "key": str|None, "detail": str|None}``
        """
        bucket = getattr(self.config, "obs_bucket", None)
        endpoint = getattr(self.config, "obs_endpoint", None)
        if not bucket or not endpoint:
            return {"written": False, "key": None, "detail": "未配置 OBS_BUCKET / OBS_ENDPOINT"}

        key = object_key(record, self.config)
        body = snapshot(record, self.config)
        try:
            client = self._obs_client(credentials, endpoint)
        except ImportError as error:
            self._log("warning", "history_object_skipped obs_module_missing detail=%s", error)
            return {"written": False, "key": key, "detail": "运行时缺少 obs 模块"}
        except Exception as error:
            self._log("warning", "history_object_client_failed detail=%s", error)
            return {"written": False, "key": key, "detail": str(error)}

        try:
            response = client.putObject(
                bucketName=bucket,
                objectKey=key,
                data=body.encode("utf-8"),
                metadata={"Content-Type": "application/json; charset=utf-8"},
            )
            status = _status_of(response)
        except Exception as error:
            self._log("warning", "history_object_put_failed detail=%s", error)
            return {"written": False, "key": key, "detail": str(error)}
        finally:
            _close(client, self.logger)

        if status is not None and status >= 300:
            self._log("warning", "history_object_bad_status key=%s status=%s", key, status)
            return {"written": False, "key": key, "detail": "HTTP %s" % status}
        self._log("info", "history_object_written bucket=%s key=%s", bucket, key)
        return {"written": True, "key": key, "detail": None, "bucket": bucket, "status": status}

    # ------------------------------------------------------------------
    def _obs_client(self, credentials, endpoint):
        """用本次调用的委托临时凭据构造客户端（不含任何长期密钥）。"""
        from obs import ObsClient

        is_secure, server = _split_endpoint(endpoint)
        return ObsClient(
            access_key_id=credentials.access_key,
            secret_access_key=credentials.secret_key,
            security_token=credentials.security_token,
            is_secure=is_secure,
            server=server,
        )

    def _log(self, level, message, *args):
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message, *args)


def _split_endpoint(endpoint):
    """``OBS_ENDPOINT`` 由部署栈下发为带协议的地址，这里还原成 SDK 需要的二元组。"""
    value = str(endpoint).strip().rstrip("/")
    if value.startswith("https://"):
        return True, value[len("https://"):]
    if value.startswith("http://"):
        return False, value[len("http://"):]
    return True, value


def _status_of(response):
    for attribute in ("status", "http_status"):
        value = getattr(response, attribute, None)
        if isinstance(value, int):
            return value
    common = getattr(response, "common", None)
    if isinstance(common, dict):
        value = common.get("status_code")
        if isinstance(value, int):
            return value
    return None


def _close(client, logger):
    try:
        client.close()
    except Exception as error:  # 关闭失败无所谓，连接由运行时回收
        if logger is not None:
            logger.debug("obs_client_close_failed detail=%s", error)


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
