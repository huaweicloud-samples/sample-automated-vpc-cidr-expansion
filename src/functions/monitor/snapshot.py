"""每轮巡检的快照对象：把这轮看到了什么、判定了什么落一份人读的文件。

与扩容侧的留痕对象同放一个私有桶，键为
``<SNAPSHOT_PREFIX>/<日期>/<执行编号>.json``；桶与端点由 ``OBS_BUCKET`` /
``OBS_ENDPOINT`` 下发，清理策略由部署栈交付的生命周期规则负责，
本模块只追加对象，不读也不改桶配置。

对象存储客户端来自函数运行时的内置 ``obs`` 模块（不打进代码包）。
内置模块缺失、写入失败或返回非 2xx 都只记日志：快照是给运维看的旁路，
不能因为它把一轮已经完成观测与投递的执行判成失败。
"""

import json
from datetime import datetime, timezone

#: 快照对象在桶内的前缀（与扩容留痕前缀分开放置，便于分别设置生命周期）
SNAPSHOT_PREFIX = "cidr-monitor-snapshot"


def object_key(execution_id, when=None):
    """按天分目录，同一轮一个对象，重复执行编号直接覆盖不了（编号唯一）。"""
    moment = when or datetime.now(timezone.utc)
    return "%s/%s/%s.json" % (
        SNAPSHOT_PREFIX, moment.strftime("%Y-%m-%d"), _safe(execution_id))


def build_body(summary, config):
    """快照正文：本轮结论 + 可安全落盘的配置视图（不含任何凭据）。"""
    body = dict(summary)
    body["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body["runtime_config"] = config.describe()
    return json.dumps(body, indent=2, sort_keys=True, default=str, ensure_ascii=False)


class SnapshotWriter(object):
    """巡检快照的写入入口。"""

    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger

    def write(self, summary, credentials, execution_id=None):
        bucket = self.config.obs_bucket
        endpoint = self.config.obs_endpoint
        if not bucket or not endpoint:
            return {"written": False, "detail": "未配置 OBS_BUCKET / OBS_ENDPOINT"}

        body = build_body(summary, self.config)
        key = object_key(execution_id or summary.get("execution_id"))
        try:
            client = self._client(credentials, endpoint)
        except ImportError as error:
            self._log("warning", "snapshot_skipped obs_module_missing detail=%s", error)
            return {"written": False, "key": key, "detail": "运行时缺少 obs 模块"}
        except Exception as error:
            self._log("warning", "snapshot_client_failed detail=%s", error)
            return {"written": False, "key": key, "detail": str(error)}

        status = None
        try:
            response = client.putObject(
                bucketName=bucket,
                objectKey=key,
                data=body.encode("utf-8"),
                metadata={"Content-Type": "application/json; charset=utf-8"},
            )
            status = _status_of(response)
        except Exception as error:
            self._log("warning", "snapshot_put_failed key=%s detail=%s", key, error)
            return {"written": False, "key": key, "detail": str(error)}
        finally:
            _close(client, self.logger)

        if status is not None and status >= 300:
            self._log("warning", "snapshot_bad_status key=%s status=%s", key, status)
            return {"written": False, "key": key, "detail": "HTTP %s" % status}
        self._log("info", "snapshot_written bucket=%s key=%s", bucket, key)
        return {"written": True, "key": key, "bucket": bucket, "status": status}

    # ------------------------------------------------------------------
    def _client(self, credentials, endpoint):
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
    """``OBS_ENDPOINT`` 下发为带协议的地址，这里还原成客户端需要的二元组。"""
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


def _safe(value):
    """对象键里不能出现 ``/`` 与空格；执行编号缺失时用时间戳兜底。"""
    text = str(value or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    for character in ("/", " ", ":", "*"):
        text = text.replace(character, "-")
    return text[:120]
