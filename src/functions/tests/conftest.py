"""离线单测的公共装配：把三个源码目录接进导入路径，并固定本地替身。

源码按函数代码包惯例是平铺导入（``import net_plan`` 这类），所以测试期把
``shared`` / ``expansion`` / ``monitor`` 三个目录直接挂到 ``sys.path``，
与运行时 zip 根目录的形态一致。两个函数各自的入口都叫 ``index.py``，
因此用别名显式加载，避免模块名互相覆盖。

云端交互一律由本目录的替身接管：不联网、不取真实服务端点、
凭据只是形如 ``<HW_ACCESS_KEY>`` 的占位符。
"""

import importlib.util
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FUNCTIONS_DIR = os.path.dirname(TESTS_DIR)
REPO_ROOT = os.path.dirname(os.path.dirname(FUNCTIONS_DIR))

for _subdir in ("shared", "expansion", "monitor", "tests"):
    _path = os.path.join(FUNCTIONS_DIR, _subdir) if _subdir != "tests" else TESTS_DIR
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _load_by_alias(alias, relative_path):
    """按文件路径加载模块并登记别名（用于两个同名 ``index.py``）。"""
    path = os.path.join(FUNCTIONS_DIR, *relative_path.split("/"))
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def expansion_entry():
    """扩容函数入口（别名加载，避开与巡检入口的模块名冲突）。"""
    return _load_by_alias("vpcidr_expansion_entry", "expansion/index.py")


@pytest.fixture(scope="session")
def monitor_entry():
    """巡检函数入口。"""
    return _load_by_alias("vpcidr_monitor_entry", "monitor/index.py")


@pytest.fixture(scope="session")
def infra_locals():
    """``infra/locals.tf`` 原文：代码常量与部署栈下发值的一致性校验用。"""
    path = os.path.join(REPO_ROOT, "infra", "locals.tf")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def infra_variables():
    """``infra/variables.tf`` 原文。"""
    path = os.path.join(REPO_ROOT, "infra", "variables.tf")
    with open(path, encoding="utf-8") as handle:
        return handle.read()
