"""在测试模块导入前固定 CLI 版本，防止导入阶段访问外部网络。"""

from unittest.mock import patch

import httpx


_cli_version_patch = patch("httpx.get", return_value=httpx.Response(
    200, json={"tag_name": "v1.4.12"}, request=httpx.Request("GET", "https://github.test")
))


def pytest_configure(config):
    _cli_version_patch.start()


def pytest_unconfigure(config):
    _cli_version_patch.stop()
