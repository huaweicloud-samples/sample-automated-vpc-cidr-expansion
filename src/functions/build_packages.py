"""函数代码包构建脚本：把源码目录拍平成 FunctionGraph 需要的 zip。

产物路径与 ``infra/variables.tf`` 里 ``var.monitor_package`` /
``var.expansion_package`` 的默认值完全一致（``src/functions/build/*.zip``），
``huaweicloud_obs_bucket_object`` 直接按这个路径取包，改一处必须同步改另一处。

打包规则（``others`` 目录不参与打包）：

* 函数目录（``src/functions/monitor``、``src/functions/expansion``）里的
  ``.py`` 文件放在 **zip 根目录**；
* 两个函数共用的模块（``src/functions/shared``）同样拍平到根目录，
  所以源码里一律是 ``import net_plan`` 这类平铺导入，函数内不存在包结构；
* ``index.py`` 必须落在根目录，入口 ``index.main_handler`` 与之对应；
* 运行时的第三方依赖不进 zip：对象存储客户端来自函数运行时的内置模块，
  HTTP 走运行时自带的 ``requests``（缺失时自动回落到标准库实现）。

用法（在仓库任意位置执行）::

    python src/functions/build_packages.py            # 两个包都构建
    python src/functions/build_packages.py monitor    # 只构建巡检函数包
    python src/functions/build_packages.py expansion  # 只构建扩容函数包

脚本只用标准库，构建完成后打印每个包的条目清单与校验值，
便于确认云端拿到的就是本地刚构建的那一份。
"""

import hashlib
import os
import sys
import zipfile

FUNCTIONS_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(FUNCTIONS_DIR, "shared")
BUILD_DIR = os.path.join(FUNCTIONS_DIR, "build")

#: 函数名 -> (源码目录名, 产物 zip 文件名)；与 locals.tf 的 zip key 一一对应
PACKAGES = {
    "monitor": ("monitor", "cidr-monitor.zip"),
    "expansion": ("expansion", "cidr-expansion.zip"),
}

#: 每个包根目录都必须有入口模块，handler 契约是 ``index.main_handler``
REQUIRED_ENTRY = "index.py"

_EXCLUDED_NAMES = {"__pycache__", ".pytest_cache"}
_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".tmp")


def collect_entries(source_dir):
    """按文件名字典序收集要打进 zip 根目录的 ``.py`` 文件。"""
    entries = []
    for name in sorted(os.listdir(source_dir)):
        if name in _EXCLUDED_NAMES or name.startswith("."):
            continue
        if name.endswith(_EXCLUDED_SUFFIXES):
            continue
        path = os.path.join(source_dir, name)
        if os.path.isfile(path) and name.endswith(".py"):
            entries.append((name, path))
    return entries


def build(function_name):
    """构建一个函数的代码包，返回 ``(zip 路径, 条目清单)``。"""
    if function_name not in PACKAGES:
        raise ValueError("未知函数名：%s（可选 %s）"
                         % (function_name, "、".join(sorted(PACKAGES))))

    source_dir_name, zip_name = PACKAGES[function_name]
    source_dir = os.path.join(FUNCTIONS_DIR, source_dir_name)
    if not os.path.isdir(source_dir):
        raise ValueError("函数源码目录不存在：%s" % source_dir)

    entries = []
    for arcname, path in collect_entries(SHARED_DIR):
        entries.append((arcname, path, "shared"))
    for arcname, path in collect_entries(source_dir):
        if any(existing[0] == arcname for existing in entries):
            raise ValueError("模块名冲突：%s 在 shared 与 %s 中同名" % (arcname, source_dir_name))
        entries.append((arcname, path, source_dir_name))

    arcnames = [item[0] for item in entries]
    if REQUIRED_ENTRY not in arcnames:
        raise ValueError("%s 缺少入口模块 %s，handler 契约无法成立"
                         % (source_dir_name, REQUIRED_ENTRY))

    if not os.path.isdir(BUILD_DIR):
        os.makedirs(BUILD_DIR)
    zip_path = os.path.join(BUILD_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, path, _origin in entries:
            archive.write(path, arcname)

    return zip_path, arcnames


def describe(zip_path):
    """打印包体大小与 SHA-256，用于确认上传的就是刚构建的这一份。"""
    size = os.path.getsize(zip_path)
    digest = hashlib.sha256()
    with open(zip_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


def main(argv):
    targets = argv[1:] or sorted(PACKAGES)
    for unknown in [item for item in targets if item not in PACKAGES]:
        print("忽略未知函数名：%s" % unknown, file=sys.stderr)
    targets = [item for item in targets if item in PACKAGES]
    if not targets:
        print("没有要构建的目标", file=sys.stderr)
        return 2

    for function_name in targets:
        zip_path, arcnames = build(function_name)
        size, checksum = describe(zip_path)
        relative = os.path.relpath(zip_path, os.path.dirname(FUNCTIONS_DIR)).replace("\\", "/")
        print("%s -> %s (%d bytes)" % (function_name, relative, size))
        print("  sha256: %s" % checksum)
        for arcname in arcnames:
            print("  - %s" % arcname)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
