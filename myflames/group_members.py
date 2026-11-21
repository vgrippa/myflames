"""Saved member credentials and consent, shared by group discovery and reads."""
from contextlib import contextmanager

from . import connection_groups
from .connector import ConnectorError


@contextmanager
def open_group_member(store, group_name, profile, active_guard, options_override=None):
    """Yield (connection, options) with both members' consent checked at login.

    The caller supplies a store snapshot, never request-supplied credentials
    or endpoints. The connector calls the guard again after transport setup.
    """
    from .connector import MySQLConnection
    from .live import connection_options
    from .workspace_store import _same_endpoint
    from .ui import _reraise_redacted

    value = {k: profile.get(k) for k in ("host", "port", "user", "database", "ssl_mode", "ssl_ca",
                                        "ssh_host", "ssh_user", "ssh_port")
             if profile.get(k) not in (None, "")}
    value["profile_id"] = profile["id"]
    value["group_name"] = group_name
    filled = store.connection_with_saved_password(value)
    options = connection_options(filled)
    if options_override:
        options.update(options_override)
    password = options.get("password")

    def login_guard():
        active_guard()
        current = store.get("profile", profile["id"])
        if (not isinstance(current, dict) or (current.get("group") or "") != group_name
                or not _same_endpoint(current, profile)
                or current.get("group_generation") != profile.get("group_generation")):
            raise connection_groups.ProfileReadError(
                "This saved connection is outside the current connection group.")

    try:
        conn = MySQLConnection(**options)
        conn._login_guard = login_guard
        conn._group_login_started = False
        with conn:
            yield conn, options
    except (ValueError, ConnectorError) as exc:
        _reraise_redacted(exc, password)
