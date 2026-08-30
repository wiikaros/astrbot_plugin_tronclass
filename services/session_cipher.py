"""Session 凭据加密（Fernet 对称加密）。

背景（审查 H4）：登录 session cookies（含 role_token JWT）此前明文存入 KV，
任何能读取 KV 存储的实体可直接复用用户凭据。本模块提供对称加密封装：

- 密钥文件懒生成：`data/plugin_data/astrbot_plugin_tronclass/.session_key`
  （不存在时自动生成，权限 0600）；
- 加密失败**绝不**静默明文落盘（抛异常由调用方处理）；
- 密钥文件与 KV 数据同生命周期：删除密钥文件后旧数据将无法解密。
"""

import json
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from astrbot.api import logger


class SessionCipher:
    """基于 Fernet 的 Session 数据加解密。"""

    def __init__(self, key_path: Path):
        self._key_path = key_path
        self._fernet: Optional[Fernet] = None

    def _ensure_fernet(self) -> Fernet:
        """确保密钥文件存在并初始化 Fernet（懒加载，幂等）。"""
        if self._fernet is not None:
            return self._fernet
        if not self._key_path.exists():
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            self._key_path.write_bytes(key)
            try:
                self._key_path.chmod(0o600)
            except OSError as e:
                logger.warning(f"无法设置密钥文件权限 0600: {e}")
        self._fernet = Fernet(self._key_path.read_bytes())
        return self._fernet

    def encrypt_dict(self, data: dict) -> str:
        """加密 dict → 密文字符串。加密失败抛异常（不静默明文）。"""
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return self._ensure_fernet().encrypt(payload).decode("utf-8")

    def decrypt_dict(self, token: str) -> dict:
        """密文字符串 → dict。密钥不匹配/数据被篡改时抛 InvalidToken。"""
        payload = self._ensure_fernet().decrypt(token.encode("utf-8"))
        return json.loads(payload)

    def decrypt_dict_or_none(self, token: str) -> Optional[dict]:
        """解密失败返回 None（不抛异常），供读取路径容错。"""
        try:
            return self.decrypt_dict(token)
        except (InvalidToken, ValueError, json.JSONDecodeError) as e:
            logger.error(f"Session 解密失败（密钥不匹配或数据损坏）: {type(e).__name__}")
            return None
