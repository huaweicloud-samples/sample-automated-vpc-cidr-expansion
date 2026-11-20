"""纯计算：网段几何、空闲块分配、子网命名序号与利用率口径。

这一层不碰网络也不碰状态存储，因此可以脱离云端单测。
``calculate_cidr_size`` / ``get_free_cidrs`` / ``allocate_from_free`` /
``generate_next_names`` 这几组纯函数是链路的算术核心，口径统一按华为云的定义。

两个必须留意的口径：

* **地址利用率**（用于二次校验阈值）：
  ``已用 / (子网网段地址总数 - 每个子网保留地址数)``。
  华为云每个子网保留 3 个地址（网段地址、网关地址、广播地址），
  巡检函数与扩容函数必须用同一个常数，否则同一条链路里会出现两个「利用率」。
  事件里带了巡检算好的数值时优先用事件值，本模块只在缺失时兜底。
* **空闲地址**（用于判断是否需要附加新网段）：
  按网段地址总数计，不扣保留位——判断「装不装得下」用整体空间更保守也更准确。
"""

import ipaddress
import math
import re

#: 每个子网不可用的地址数：网段地址 + 网关地址 + 广播地址
RESERVED_IPS_PER_SUBNET = 3

#: 华为云子网前缀长度上限（掩码长度不能大于 28）
MAX_SUBNET_PREFIX = 28

_NAME_PATTERN = re.compile(r"(\d+)(?!.*\d)")


def network(cidr):
    """把 CIDR 解析成 IPv4 网段；非法输入返回 ``None`` 而不是抛异常。"""
    try:
        return ipaddress.IPv4Network(str(cidr), strict=True)
    except (ValueError, TypeError):
        return None


def total_addresses(cidr):
    net = network(cidr)
    return int(net.num_addresses) if net else 0


def usable_addresses(cidr):
    """子网可用地址数（扣除保留地址）。"""
    return max(total_addresses(cidr) - RESERVED_IPS_PER_SUBNET, 0)


def gateway_ip(cidr):
    """子网网关：网段的第一个可用主机地址（与部署栈里 ``cidrhost(cidr, 1)`` 一致）。"""
    net = network(cidr)
    if net is None or net.num_addresses < 2:
        return None
    return str(ipaddress.IPv4Address(int(net.network_address) + 1))


def summarize_state(cidrs, subnets):
    """把 VPC 网段与子网现状汇成一个状态视图。

    :param cidrs: 主网段 + 已生效的附加网段
    :param subnets: 子网列表，元素含 ``cidr`` 与可选的 ``available_ip_address_count``
    """
    nets = [network(cidr) for cidr in cidrs]
    nets = [net for net in nets if net is not None]
    subnet_nets = [network(item.get("cidr")) for item in subnets]
    subnet_nets = [net for net in subnet_nets if net is not None]

    capacity = 0
    available = 0
    availability_known = True
    for item in subnets:
        net = network(item.get("cidr"))
        if net is None:
            continue
        capacity += max(int(net.num_addresses) - RESERVED_IPS_PER_SUBNET, 0)
        remaining = item.get("available_ip_address_count")
        if remaining is None:
            availability_known = False
        else:
            available += int(remaining)

    allocated = sum(int(net.num_addresses) for net in subnet_nets)
    gross = sum(int(net.num_addresses) for net in nets)
    utilization = None
    if capacity > 0 and availability_known:
        utilization = round((capacity - available) * 100.0 / capacity, 2)
        if utilization < 0:
            utilization = 0.0

    return {
        "cidr_blocks": [str(net) for net in nets],
        "cidr_count": len(nets),
        "subnet_count": len(subnets),
        "total_capacity": capacity,
        "available_ips": available if availability_known else None,
        "utilization": utilization,
        "availability_known": availability_known,
        "allocated_addresses": allocated,
        "unallocated_addresses": max(gross - allocated, 0),
        "availability_zones": sorted({item.get("availability_zone")
                                      for item in subnets if item.get("availability_zone")}),
    }


def overlaps_any(cidr, others):
    """判断网段是否与给定集合中的任一网段重叠（用于消费规划网段前的安全校验）。"""
    target = network(cidr)
    if target is None:
        return True  # 解析不了的网段一律视为不安全
    for other in others or []:
        net = network(other)
        if net is None:
            continue
        if target.overlaps(net):
            return True
    return False


def covers(destination, cidr):
    """路由目的地址 ``destination`` 是否覆盖网段 ``cidr``。

    华为云路由表里的 ``destination`` 可能是裸 IP（按 /32 解析）或 CIDR，
    这里只做「目的网段是目标网段的父网段」判定，用于附加网段生效后的路由覆盖校验。
    """
    route = network(destination)
    target = network(cidr)
    if route is None or target is None:
        return False
    try:
        return route.supernet_of(target)
    except ValueError:
        return False


def pick_plan_cidr(plan, main_cidr, attached_cidrs, subnet_cidrs):
    """按规划顺序挑出第一个「未附加且不与任何现有地址空间重叠」的网段。

    :returns: ``(cidr, skipped)``；``skipped`` 记录被跳过的条目与原因，便于留痕
    """
    skipped = []
    used = [main_cidr] + list(attached_cidrs or [])
    attached = {str(item) for item in (attached_cidrs or [])}
    for cidr in plan or []:
        cidr = str(cidr).strip()
        if cidr in attached:
            skipped.append({"cidr": cidr, "reason": "already_associated"})
            continue
        if network(cidr) is None:
            skipped.append({"cidr": cidr, "reason": "invalid_format"})
            continue
        if overlaps_any(cidr, used):
            skipped.append({"cidr": cidr, "reason": "overlaps_vpc_cidrs"})
            continue
        if overlaps_any(cidr, subnet_cidrs):
            skipped.append({"cidr": cidr, "reason": "overlaps_existing_subnets"})
            continue
        return cidr, skipped
    return None, skipped


def free_blocks(cidrs, subnet_cidrs):
    """算出 VPC 地址空间里尚未被子网占用的块（按起始地址排序）。"""
    blocks = []
    for cidr in cidrs or []:
        net = network(cidr)
        if net is not None:
            blocks.append(net)
    for used_cidr in subnet_cidrs or []:
        used = network(used_cidr)
        if used is None:
            continue
        remaining = []
        for block in blocks:
            if block.overlaps(used):
                remaining.extend(_exclude(block, used))
            else:
                remaining.append(block)
        blocks = remaining
    return sorted(blocks)


def allocate_from_free(blocks, prefix):
    """从空闲块里切出一个 ``/prefix`` 网段。

    :returns: ``(cidr 或 None, 更新后的空闲块列表)``——调用方在同一个列表上连续分配，
              保证同一轮里创建的多个子网不会互相重叠。
    """
    if prefix > MAX_SUBNET_PREFIX:
        return None, blocks
    for index, block in enumerate(blocks):
        if block.prefixlen <= prefix:
            candidate = list(block.subnets(new_prefix=prefix))[0]
            remaining = blocks[:index] + blocks[index + 1:]
            remaining.extend(block.address_exclude(candidate))
            return str(candidate), sorted(remaining)
    return None, blocks


def required_netmask(addresses):
    """能装下 ``addresses`` 个地址的最小前缀长度。"""
    if addresses <= 1:
        return 32
    return 32 - int(math.ceil(math.log(addresses, 2)))


# ----------------------------------------------------------------------
# 命名序号
# ----------------------------------------------------------------------
def split_name(name):
    """拆出「前缀 + 序号 + 后缀」；没有序号时序号为 ``None``。"""
    match = _NAME_PATTERN.search(name or "")
    if not match:
        return name, None, ""
    return name[:match.start()], match.group(), name[match.end():]


def next_names(sample_name, existing_names, count):
    """延续已有命名序号生成新名字：``…-public-2`` → ``…-public-3``。

    样例名不带数字时退化为追加 ``-1/-2`` 序号。
    """
    prefix, sample_number, suffix = split_name(sample_name)
    if sample_number is None:
        return ["%s-%d" % (sample_name, index + 1) for index in range(count)]

    pad = len(sample_number)
    numbers = []
    for name in existing_names or []:
        _, existing_number, _ = split_name(name)
        if existing_number:
            numbers.append(int(existing_number))
    highest = max(numbers) if numbers else int(sample_number) - 1
    return ["%s%s%s" % (prefix, str(highest + 1 + index).zfill(pad), suffix)
            for index in range(count)]


def matching_names(sample_name, subnets):
    """同一层级的现有子网名（按样例名的前缀匹配）。"""
    prefix, number, _ = split_name(sample_name)
    needle = prefix if number else sample_name
    return [item.get("name") for item in subnets or []
            if item.get("name") and needle and str(item["name"]).startswith(needle)]


def name_is_within_limit(name, limit=64):
    return bool(name) and len(str(name)) <= limit


def _exclude(block, used):
    """``address_exclude`` 的兜底：``used`` 覆盖整个 ``block`` 时补集为空。

    只有「``used`` 是 ``block`` 的子网」时 ``address_exclude`` 才可用；``used`` 与
    ``block`` 相等或更大时它会抛异常。此时补集一定是空的——把 ``block`` 原样放回
    等于把一个已被子网占满的地址段继续当成空闲空间，后续分配会切出重叠子网。
    """
    try:
        return list(block.address_exclude(used))
    except (ValueError, ArithmeticError):
        return [item for item in block.subnets()
                if item.prefixlen >= used.prefixlen and not item.subnet_of(used)]
