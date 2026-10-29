"""Windows 用の代用品。SearXNG は valkey の接続エラー表示でしか pwd を使わない（valkey は使っていない）。"""
import getpass
from types import SimpleNamespace


def getpwuid(_uid):
    return SimpleNamespace(pw_name=getpass.getuser())
