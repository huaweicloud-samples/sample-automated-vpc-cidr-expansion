"""发现被纳管的 VPC，并把「地址还够不够用」算成一个可判定的观测结果。

三段职责，全部只读：

1. :func:`discover` —— 列出项目内 VPC，按标签 ``MONITOR_TAG_KEY=MONITOR_TAG_VALUE``
   筛出纳管对象。标签筛选在本地完成：列表接口返回的标签形态不唯一
   （字典或 ``key/value`` 数组），交回服务端过滤反而更容易漏。
2. :func:`observe` —— 逐个 VPC 读网段与子网现状，用 :func:`net_plan.summarize_state`
   算出利用率、总容量、可用地址与子网数。该口径与扩容函数的二次校验共用同一模块，
   保证同一条链路里不会出现两个「利用率」。
3. :func:`plan_view` —— 结合部署栈下发的地址规划，给出「下一个可消费的网段」
   与「规划是否已用尽」，作为是否投递扩容指令的前置判断。

单个 VPC 读失败只影响它自己：返回带 ``error`` 的观测结果，本轮其余对象照常处理。
"""

import net_plan


def discover(api, config, logger=None):
    """返回打了纳管标签的 VPC 列表（元素为 ``list_vpcs`` 的字段视图）。"""
    try:
        everything = api.list_vpcs()
    except Exception as error:  # 列表读不到就没有任何观测，交由入口判定本轮失败
        _log(logger, "error", "monitor_vpc_list_failed detail=%s", error)
        raise

    monitored = []
    for vpc in everything:
        if matches_tag(vpc, config.tag_key, config.tag_value):
            monitored.append(vpc)
    _log(logger, "info", "monitor_discovered total=%d monitored=%d filter=%s=%s",
         len(everything), len(monitored), config.tag_key, config.tag_value)
    return monitored


def matches_tag(vpc, key, value):
    """标签匹配：键存在且值相等（值按字符串比较，大小写敏感）。"""
    tags = vpc.get("tags") if isinstance(vpc, dict) else None
    if not isinstance(tags, dict):
        return False
    if key not in tags:
        return False
    return str(tags.get(key)) == str(value)


def observe(api, vpc, config, logger=None):
    """把一个 VPC 的现状算成观测结果。

    :returns: ``{"vpc_id","vpc_name","state","utilization",...,"error"}``；
              读取失败时 ``error`` 非空、``state`` 为 ``None``
    """
    vpc_id = vpc.get("id")
    observation = {
        "vpc_id": vpc_id,
        "vpc_name": vpc.get("name"),
        "vpc_status": vpc.get("status"),
        "state": None,
        "utilization": None,
        "total_capacity": None,
        "available_ips": None,
        "subnet_count": None,
        "cidr_count": None,
        "next_cidr": None,
        "plan_exhausted": None,
        "error": None,
    }
    if not vpc_id:
        observation["error"] = "列表响应缺少 VPC 标识"
        return observation

    try:
        detail = api.show_vpc(vpc_id)
        subnets = api.list_subnets(vpc_id)
    except Exception as error:
        observation["error"] = str(error)
        _log(logger, "warning", "monitor_observe_failed vpc_id=%s detail=%s", vpc_id, error)
        return observation

    state = net_plan.summarize_state(cidrs_of(detail), subnets)
    observation.update({
        "vpc_name": detail.get("name") or observation["vpc_name"],
        "vpc_status": detail.get("status") or observation["vpc_status"],
        "state": state,
        "utilization": state.get("utilization"),
        "total_capacity": state.get("total_capacity"),
        "available_ips": state.get("available_ips"),
        "subnet_count": state.get("subnet_count"),
        "cidr_count": state.get("cidr_count"),
    })
    observation.update(plan_view(detail, subnets, config))

    if state.get("availability_known") is False:
        _log(logger, "warning",
             "monitor_availability_unknown vpc_id=%s 说明：子网余量读不到，本轮不投指令",
             vpc_id)
    return observation


def cidrs_of(vpc_detail):
    """主网段 + 已生效的附加网段，顺序固定，便于留痕比对。"""
    blocks = []
    main = vpc_detail.get("cidr")
    if main:
        blocks.append(str(main))
    for item in vpc_detail.get("extend_cidrs") or []:
        if item:
            blocks.append(str(item))
    return blocks


def plan_view(vpc_detail, subnets, config):
    """地址规划视角：下一个可消费的网段与是否已用尽。

    只做只读判断，不附加网段——附加动作属于扩容函数的职责。
    """
    picked, _skipped = net_plan.pick_plan_cidr(
        config.secondary_cidr_plan,
        vpc_detail.get("cidr"),
        vpc_detail.get("extend_cidrs") or [],
        [item.get("cidr") for item in subnets if isinstance(item, dict)],
    )
    return {"next_cidr": picked, "plan_exhausted": not bool(picked)}


def is_actionable(observation):
    """观测结果能否支撑一次判定：状态读到了，且利用率算得出来。"""
    return (observation.get("state") is not None
            and observation.get("utilization") is not None)


def _log(logger, level, message, *args):
    if logger is not None:
        getattr(logger, level, logger.info)(message, *args)
