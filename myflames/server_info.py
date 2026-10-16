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
