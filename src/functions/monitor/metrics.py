"""把观测结果写成监控服务的自定义指标。

指标名、命名空间与维度名三者必须与 ``infra/locals.tf`` 的大盘图件完全一致，
否则大盘取不到数：

===============  ==========================  ========
本模块常量         大盘图件标题                 单位
===============  ==========================  ========
``UtilizationPercentage``  地址利用率   ``Percent``
``AvailableIPs``           可用地址数   ``Count``
``TotalCapacity``          地址总容量   ``Count``
``SubnetCount``            子网数量     ``Count``
===============  ==========================  ========

维度只有一个：维度名取 ``CES_DIMENSION_NAME``（默认 ``VpcId``），维度值是 VPC 标识。
被纳管 VPC 在运行期由标签发现，部署期无法枚举其 ID，
所以大盘按「该维度下全部资源」展示，函数侧则逐个 VPC 上报。

利用率算不出来时（子网余量读不到）只跳过该条序列，其余三条照报，
这样大盘上「无数据」与「数据为 0」是两件事，排障时不会被混为一谈。
"""

#: 与 locals.tf 的 ``local.metric_names`` 逐字一致，改名等于把大盘打空
METRIC_UTILIZATION = "UtilizationPercentage"
METRIC_AVAILABLE = "AvailableIPs"
METRIC_CAPACITY = "TotalCapacity"
METRIC_SUBNETS = "SubnetCount"

UNIT_PERCENT = "Percent"
UNIT_COUNT = "Count"

#: 单次请求携带的指标条数上限：分块发送，避免一轮纳管对象偏多时被服务端截断
BATCH_SIZE = 50


def build_samples(observation, config, timestamp_ms=None):
    """一个 VPC 的四条指标（缺失值不生成条目）。"""
    vpc_id = observation.get("vpc_id")
    if not vpc_id:
        return []
    dimensions = [{"name": config.metric_dimension, "value": str(vpc_id)}]
    stamp = int(timestamp_ms if timestamp_ms is not None else _now_ms())
    candidates = (
        (METRIC_UTILIZATION, observation.get("utilization"), UNIT_PERCENT),
        (METRIC_AVAILABLE, observation.get("available_ips"), UNIT_COUNT),
        (METRIC_CAPACITY, observation.get("total_capacity"), UNIT_COUNT),
        (METRIC_SUBNETS, observation.get("subnet_count"), UNIT_COUNT),
    )
    samples = []
    for name, value, unit in candidates:
        if value is None:
            continue
        samples.append({
            "namespace": config.metric_namespace,
            "metric_name": name,
            "dimensions": dimensions,
            "value": float(value),
            "unit": unit,
            "timestamp": stamp,
        })
    return samples


def report(api, observations, config, logger=None, timestamp_ms=None):
    """逐 VPC 组装并按块上报。

    :returns: ``{"samples": n, "batches": m, "failed_batches": k}``
    """
    samples = []
    for observation in observations:
        samples.extend(build_samples(observation, config, timestamp_ms))

    reported = {"samples": len(samples), "batches": 0, "failed_batches": 0}
    for start in range(0, len(samples), BATCH_SIZE):
        chunk = samples[start:start + BATCH_SIZE]
        reported["batches"] += 1
        try:
            api.report_metric_batch(chunk)
        except Exception as error:
            # 指标是大盘的输入，不是扩容链路的依赖：写失败只让大盘缺一块，
            # 不能据此把已经算好的判定与已经投递的指令改判为本轮失败
            reported["failed_batches"] += 1
            _log(logger, "warning", "metric_batch_failed size=%d detail=%s", len(chunk), error)
    _log(logger, "info", "metric_reported %s", reported)
    return reported


def _now_ms():
    import time

    return int(time.time() * 1000)


def _log(logger, level, message, *args):
    if logger is not None:
        getattr(logger, level, logger.info)(message, *args)
