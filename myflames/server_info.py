"""Pure server identity helpers, below connectors and performance collectors."""
import re


def detect_flavor(version, version_comment=""):
    """MariaDB identifies itself in either version field, ignoring case."""
    return ("mariadb" if "mariadb" in (str(version or "") + " " +
                                      str(version_comment or "")).lower() else "mysql")


def parse_version_tuple(version, leading=False, prefer_mariadb=False, components=3,
                        default=(0, 0, 0)):
    """Parse a version with the caller's existing input contract.

    Server variables require a leading version; tool banners and privilege
    metadata may contain a prefix. MariaDB tools can print a client version
    before their actual distribution version. Capture's gate uses just the
    leading major/minor and treats an unreadable version separately.
    """
    if not leading and isinstance(version, (tuple, list)):
        return tuple(version)[:components]
    # Leading callers historically accept strings (or an empty falsey value),
    # while the other callers accept values convertible to strings.
    text = (version or "") if leading else str(version)
    pattern = r"(\d+)" + r"\.(\d+)" * (components - 1)
    match = None
    if prefer_mariadb:
        match = re.search(pattern + r"[^\s,]*-MariaDB", text, re.I)
    if match is None:
        match = re.match(r"\s*" + pattern, text) if leading else re.search(pattern, text)
    return tuple(int(part) for part in match.groups()) if match else default


def server_version_tuple(text):
    """Compatibility adapter for leading three-part server variables."""
    return parse_version_tuple(text, leading=True)


def tool_version_tuple(text):
    """Compatibility adapter for server strings and external tool banners."""
    return parse_version_tuple(str(text or ""), prefer_mariadb=True)


#: Oldest releases myflames documents support for (README, AGENTS.md).
SUPPORTED_MINIMUM = {"mysql": (8, 4), "mariadb": (10, 11)}


def unsupported_version_note(version, version_comment=""):
    """One stderr line for a server below the documented floor, else None.

    MySQL below 8.4 and MariaDB below 10.11 can still answer many reads, but
    plan formats, Performance Schema columns and variables differ, so results
    may be missing or wrong. An unreadable version returns None. A leading
    "5.5.5-" (the handshake version some MariaDB builds send for old
    replication clients) is ignored.
    """
    text = re.sub(r"^\s*5\.5\.5-", "", str(version or ""))
    engine = detect_flavor(text, version_comment)
    found = parse_version_tuple(text, leading=True, components=2, default=None)
    if found is None or found >= SUPPORTED_MINIMUM[engine]:
        return None
    name = "MariaDB" if engine == "mariadb" else "MySQL"
    floor = SUPPORTED_MINIMUM[engine]
    return ("myflames supports %s %d.%d or later; this server is %s %s, so some results "
            "may be missing or wrong." % (name, floor[0], floor[1], name, text.strip().split("-")[0]))
