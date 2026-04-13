"""Cache-family teach lessons."""
from __future__ import annotations

from . import lru

LESSONS = {
    "lru": {
        "title": "InnoDB buffer pool — midpoint-insertion LRU",
        "summary": "Why MySQL's LRU is scan-resistant — young/old sublists, innodb_old_blocks_time.",
        "render": lru.render,
    }
}
