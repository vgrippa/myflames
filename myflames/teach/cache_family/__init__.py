"""Cache-family teach lessons."""
from __future__ import annotations

from . import lru, buffer_pool_warmup

LESSONS = {
    "lru": {
        "title": "InnoDB buffer pool — midpoint-insertion LRU",
        "summary": "Why MySQL's LRU is scan-resistant — young/old sublists, innodb_old_blocks_time.",
        "render": lru.render,
    },
    "buffer_pool_warmup": {
        "title": "InnoDB buffer pool — cold-start vs warm, dump/load cure",
        "summary": "Why repeat queries are fast, why restarts hurt, and what ib_buffer_pool does about it.",
        "render": buffer_pool_warmup.render,
    },
}
