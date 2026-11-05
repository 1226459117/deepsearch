"""离线单测替身:仅实现 window.py 用到的 Redis 子集。"""

from __future__ import annotations


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}

    # ---- STRING ----
    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    def incrby(self, key: str, amount: int) -> int:
        current = int(self.strings.get(key, 0)) + amount
        self.strings[key] = str(current)
        return current

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.strings.pop(key, None)
            self.lists.pop(key, None)

    # ---- LIST ----
    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key, [])
        if stop == -1:
            return items[start:]
        return items[start : stop + 1]

    def ltrim(self, key: str, start: int, stop: int) -> None:
        assert stop == -1, "FakeRedis 仅支持 ltrim(key, start, -1)"
        self.lists[key] = self.lists.get(key, [])[start:]
