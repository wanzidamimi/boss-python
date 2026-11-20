"""职位详情采集去重：RedisBloom 日桶 + MySQL 二次确认。

为什么这样设计
--------------
1. RedisBloom 的 EXPIRE 是 key 级别的，不能给单个元素设 TTL。
   因此按天分桶：boss:detail:bf:YYYYMMDD，每个桶 TTL = BOSS_BLOOM_TTL_DAYS 天。
   查询时检查最近 TTL_DAYS 个桶；超过窗口的桶自然过期，详情会被重新采集。
2. 布隆成员必须用跨会话稳定的 encrypt_job_id（列表里的 encryptJobId），
   不能用 lid —— lid 带搜索会话前缀，每次搜索都不同。
3. 布隆只做"提前跳过"：阳性还要查 MySQL job_details 确认，误判不会漏采；
   Redis 不可用时 fail-open（全部照抓），只损失加速效果。
"""

import logging
import os
from datetime import datetime, timedelta

LOG = logging.getLogger(__name__)

DEFAULT_PREFIX = "boss:detail:bf:"
DEFAULT_TTL_DAYS = 1
DEFAULT_ERROR_RATE = 0.01
DEFAULT_CAPACITY = 100_000


class RedisBloomBackend:
    """最小化的 RedisBloom 封装。"""

    def __init__(self, url: str, error_rate: float = DEFAULT_ERROR_RATE,
                 capacity: int = DEFAULT_CAPACITY):
        import redis

        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        self.error_rate = float(error_rate)
        self.capacity = int(capacity)
        self._reserved: set[str] = set()

    def _ensure(self, key: str) -> None:
        if key in self._reserved:
            return
        try:
            self.client.execute_command(
                "BF.RESERVE", key, self.error_rate, self.capacity
            )
        except Exception as exc:
            # 已存在是正常情况；其它错误向上抛，由调用方 fail-open
            if "exists" not in str(exc).lower():
                raise
        self._reserved.add(key)

    def exists_many(self, key: str, members: list[str]) -> list[bool]:
        if not members:
            return []
        try:
            result = self.client.execute_command("BF.MEXISTS", key, *members)
            return [bool(item) for item in result]
        except Exception as exc:
            if "not found" in str(exc).lower():
                return [False] * len(members)
            raise

    def add_many(self, key: str, members: list[str]) -> None:
        if not members:
            return
        # 必须先 RESERVE：BF.MADD 在 key 不存在时会用默认容量 100 自动建表，
        # 容量太小会导致误判率迅速升高。
        self._ensure(key)
        self.client.execute_command("BF.MADD", key, *members)

    def expire(self, key: str, seconds: int) -> None:
        self.client.expire(key, int(seconds))

    def ping(self) -> bool:
        return bool(self.client.ping())


class DetailDedupe:
    """详情去重器；backend 可注入，便于单测。"""

    def __init__(self, backend=None, ttl_days: int = DEFAULT_TTL_DAYS,
                 prefix: str = DEFAULT_PREFIX, today_provider=None,
                 enabled: bool = True):
        self.backend = backend if enabled else None
        self.ttl_days = max(1, int(ttl_days))
        self.prefix = prefix
        self.enabled = bool(enabled and backend is not None)
        self._today = today_provider or datetime.now
        self._degraded = False

    @classmethod
    def from_env(cls, today_provider=None) -> "DetailDedupe":
        enabled = os.getenv("BOSS_BLOOM_ENABLED", "1").lower() not in (
            "0", "false", "no",
        )
        ttl_days = int(os.getenv("BOSS_BLOOM_TTL_DAYS", str(DEFAULT_TTL_DAYS)))
        prefix = os.getenv("BOSS_BLOOM_PREFIX", DEFAULT_PREFIX)
        if not enabled:
            return cls(backend=None, ttl_days=ttl_days, prefix=prefix,
                       today_provider=today_provider, enabled=False)

        backend = None
        try:
            backend = RedisBloomBackend(
                os.getenv("BOSS_REDIS_URL", "redis://127.0.0.1:6380/0"),
                float(os.getenv("BOSS_BLOOM_ERROR_RATE", str(DEFAULT_ERROR_RATE))),
                int(os.getenv("BOSS_BLOOM_CAPACITY", str(DEFAULT_CAPACITY))),
            )
            backend.ping()
        except Exception as exc:
            LOG.warning("布隆过滤器不可用（将降级为全部采集）: %s", exc)
            backend = None
        return cls(backend=backend, ttl_days=ttl_days, prefix=prefix,
                   today_provider=today_provider,
                   enabled=backend is not None)

    # ------------------------------------------------------------------
    def bucket_keys(self, today: datetime | None = None) -> list[str]:
        today = today or self._today()
        return [
            f"{self.prefix}{(today - timedelta(days=offset)).strftime('%Y%m%d')}"
            for offset in range(self.ttl_days)
        ]

    def _degrade(self, exc: Exception) -> None:
        if not self._degraded:
            self._degraded = True
            LOG.warning("布隆过滤器不可用，本次降级为全部采集: %s", exc)

    def seen(self, encrypt_id: str) -> bool:
        """布隆是否认为该职位采过（可能误判）。"""
        if not self.enabled or not encrypt_id:
            return False
        try:
            for key in self.bucket_keys():
                result = self.backend.exists_many(key, [encrypt_id])
                if result and result[0]:
                    return True
            return False
        except Exception as exc:
            self._degrade(exc)
            self.enabled = False
            return False

    def mark(self, encrypt_id: str) -> None:
        self.mark_many([encrypt_id])

    def mark_many(self, encrypt_ids: list[str]) -> None:
        ids = [item for item in encrypt_ids if item]
        if not self.enabled or not ids:
            return
        try:
            key = self.bucket_keys()[0]
            self.backend.add_many(key, ids)
            self.backend.expire(key, self.ttl_days * 86400)
        except Exception as exc:
            self._degrade(exc)
            self.enabled = False

    def should_fetch(self, encrypt_id: str, confirm=None) -> bool:
        """是否应该抓详情。

        - 布隆阴性：一定没采过 -> 抓
        - 布隆阳性：需要 confirm(encrypt_id) 返回 True（MySQL 确认已采）才跳过
        - 没有 confirm 或 confirm 异常：抓（绝不因不确定而漏采）
        """
        if not encrypt_id:
            return True
        if not self.enabled:
            return True
        if not self.seen(encrypt_id):
            return True
        if confirm is None:
            return True
        try:
            return not bool(confirm(encrypt_id))
        except Exception as exc:
            LOG.warning("详情去重确认失败，按未采集处理: %s", exc)
            return True
