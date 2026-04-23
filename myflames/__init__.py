# MySQL & MariaDB EXPLAIN Flame Graphs - Python
__version__ = "1.4.0"


def render_lesson(name: str) -> str:
    """Render a `teach` lesson by name. Lazy import avoids pulling teach/
    into every CLI invocation that doesn't use it."""
    from .teach import render_lesson as _impl
    return _impl(name)
