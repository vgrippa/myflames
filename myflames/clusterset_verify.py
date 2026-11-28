# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""SQL evidence after a reviewed ClusterSet operation.

Shell's exit status is deliberately not an input. A successful process can
leave unreachable members, caught cleanup errors, or stale metadata. The
caller combines these observations with the actual Shell result.

Ordinary topology verification uses cluster.read_status's bounded, consented
per-cluster reads. Removal and dissolve additionally retain the *before*
inventory: disappearing from metadata does not prove a server was cleaned up.
Only the supplied saved-profile opener can open a remote server; metadata
addresses are matching hints, never credentials or new connection settings.
"""
import json

from .connector import ConnectorError
from .connection_groups import safe_problem


class _Evidence(object):
    def __init__(self):
        self.facts = []
        self.missing = []
        self.mismatches = []

    def add(self, server, fact, expected, actual, known=True, matches=None):
        status = ("missing" if not known else "verified" if
                  (actual == expected if matches is None else matches) else "mismatch")
        self.facts.append({"server": server, "fact": fact, "expected": expected,
                           "actual": actual, "status": status})
        label = "%s: %s" % (server, fact)
        if status == "missing" and label not in self.missing:
            self.missing.append(label)
        elif status == "mismatch" and label not in self.mismatches:
            self.mismatches.append(label)

    def unavailable(self, server, fact, reason):
        self.add(server, fact, "read successfully", reason, known=False)

    def result(self, after):
        if self.mismatches:
            summary = "Check " + "; ".join(self.mismatches)
            if self.missing:
                summary += ". Not read: " + "; ".join(self.missing)
        elif self.missing:
            summary = "Not read: " + "; ".join(self.missing)
        else:
            summary = "All expected SQL facts were verified."
        return {"facts": self.facts, "missing": self.missing, "mismatches": self.mismatches,
                "summary": summary, "after": after}


def _members(item):
    return (item or {}).get("members") or (item or {}).get("instances") or []


def _label(item):
    route = item.get("route") or {}
    return route.get("profile_name") or route.get("profile_id") or item.get("name") or "Unknown cluster"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _endpoint(value):
    from .cluster import _split_address
    host, port = _split_address(value or "")
    return ((host or "").lower(), port or 3306)


def _route_context(opener, current):
    """Change the displayed local profile without extending login consent."""
    def open_saved(profile):
        return opener(profile)
    for name in ("group_scope", "check", "consent", "prepare"):
        if hasattr(opener, name):
            setattr(open_saved, name, getattr(opener, name))
    open_saved.current_profile = current
    return open_saved


def _read_after(conn, before, options, evidence):
    """Prefer a current primary's metadata, even if the caller was removed."""
    from . import cluster
    after = None
    try:
        after = cluster.read_status(conn, **options)
    except (ConnectorError, ValueError, OSError) as exc:
        evidence.unavailable("This connection", "post-action ClusterSet read", safe_problem(exc))
    if (after or {}).get("clusterset"):
        return after
    # A removed member may no longer have any ClusterSet metadata. Use a
    # previously known route into a surviving cluster from the same group.
    old = before.get("clusterset") or {}
    eligible = {m["server_uuid"].lower() for c in old.get("clusters") or ()
                if c.get("role") == "PRIMARY" for m in _members(c)
                if isinstance(m.get("server_uuid"), str) and cluster._UUID.fullmatch(m["server_uuid"])}
    opener = options.get("open_profile")
    if opener is not None:
        profiles = options.get("profiles") or ()
        for profile in profiles:
            uuid = (profile.get("topology_identity") or {}).get("server_uuid")
            if not isinstance(uuid, str) or uuid.lower() not in eligible:
                continue
            try:
                with opener(profile) as peer:
                    reader = cluster._ClusterSetReader(peer)
                    rows = reader.rows("SELECT @@server_uuid")
                    if (not rows or not rows[0] or not isinstance(rows[0][0], str) or
                            rows[0][0].lower() != uuid.lower()):
                        continue
                    peer_options = dict(options, current_profile_id=profile["id"],
                                        open_profile=_route_context(opener, profile))
                    # grouped_read's explicit current is a store snapshot,
                    # not an invented endpoint or an extension of consent.
                    peer_options["current_profile"] = profile
                    candidate = cluster.read_status(peer, **peer_options)
                    candidate_uuid = (candidate.get("server") or {}).get("server_uuid")
                    if (isinstance(candidate_uuid, str) and candidate_uuid.lower() == uuid.lower() and
                            candidate.get("clusterset")):
                        return candidate
            except (ConnectorError, ValueError, OSError):
                continue
    return after


def _verify_membership(evidence, plan, before, after):
    base, _, suffix = plan["action"].partition(":")
    old = before.get("clusterset") or {}
    current = (after or {}).get("clusterset") or {}
    name = old.get("name") or "ClusterSet"
    if not current:
        evidence.unavailable(name, "current primary metadata", "No ClusterSet view could be read")
        return
    evidence.add(name, "ClusterSet identity", old.get("name"), current.get("name"))
    evidence.add(name, "metadata read from current primary cluster", False, current.get("from_local_copy"),
                 known=current.get("from_local_copy") is False)
    target = (plan.get("expect", {}).get("clusterset_target") or {}).get("cluster_name") or suffix
    parameters = plan.get("parameters") or {}
    if base == "create_replica_cluster":
        target = parameters.get("name")
    old_clusters = {c.get("name"): c for c in old.get("clusters") or ()}
    clusters = {c.get("name"): c for c in current.get("clusters") or ()}
    expected_names = set(old_clusters)
    if base in ("remove_cluster", "remove_cluster_keep"):
        expected_names.discard(target)
    elif base == "create_replica_cluster":
        expected_names.add(target)
    evidence.add(name, "member clusters", sorted(expected_names, key=str), sorted(clusters, key=str))
    if base in ("set_primary_cluster", "force_primary_cluster", "rejoin_cluster", "remove_cluster",
                "remove_cluster_keep", "create_replica_cluster"):
        old_view = _number((plan.get("expect") or {}).get("clusterset_view_id", old.get("view_id")))
        view = _number(current.get("view_id"))
        if base == "force_primary_cluster":
            valid = old_view is not None and view is not None and old_view >= 0 and view >= 0
            old_hi = old_view >> 32 if valid else None
            hi = view >> 32 if valid else None
            evidence.add(name, "membership view high counter advanced",
                         "view_id high counter greater than %s" % old_hi, hi,
                         known=valid, matches=valid and hi > old_hi)
        evidence.add(name, "membership view advanced", "view_id greater than %s" % old_view, view,
                     known=old_view is not None and view is not None,
                     matches=old_view is not None and view is not None and view > old_view)
    expected_primary = target if base in ("set_primary_cluster", "force_primary_cluster") else old.get("primary_cluster")
    evidence.add(name, "primary cluster", expected_primary, current.get("primary_cluster"))
    invalidated = set(c.get("name") for c in old_clusters.values() if c.get("invalidated"))
    invalidated.update(parameters.get("invalidateReplicaClusters") or ())
    if base == "force_primary_cluster":
        invalidated.add(old.get("primary_cluster"))
    if base in ("rejoin_cluster", "remove_cluster", "remove_cluster_keep"):
        invalidated.discard(target)
    evidence.add(name, "invalidated clusters", sorted(n for n in invalidated if n in expected_names),
                 sorted(c.get("name") for c in clusters.values() if c.get("invalidated")))
    old_ids = getattr(before, "_clusterset_ids", {})
    new_ids = getattr(after, "_clusterset_ids", {})
    old_groups = getattr(before, "_clusterset_groups", {})
    new_groups = getattr(after, "_clusterset_groups", {})
    for cname in sorted(expected_names & set(old_clusters), key=str):
        if cname in old_ids:
            evidence.add(cname, "cluster identity", old_ids[cname], new_ids.get(cname), known=cname in new_ids)
        if cname in old_groups:
            evidence.add(cname, "group identity", old_groups[cname], new_groups.get(cname),
                         known=new_groups.get(cname) is not None)
    for cname in sorted(expected_names, key=str):
        item = clusters.get(cname)
        if item is None or cname in invalidated:
            continue
        evidence.add(cname, "cluster role", "PRIMARY" if cname == expected_primary else "REPLICA", item.get("role"))
    if base == "create_replica_cluster":
        candidate = (plan.get("expect") or {}).get("replica_candidate") or {}
        item = clusters.get(target) or {}
        matches = [m for m in _members(item) if m.get("server_uuid") == candidate.get("server_uuid")]
        evidence.add(target or name, "new member ONLINE", "ONLINE",
                     matches[0].get("state") if len(matches) == 1 else None,
                     known=bool(matches) and matches[0].get("state") is not None)


def _verify_live(evidence, before, after):
    """Verify raw primary SRO, replica member action and channels, keeping unread facts unknown."""
    current = (after or {}).get("clusterset") or {}
    clusters = current.get("clusters") or ()
    primary = next((c for c in clusters if c.get("role") == "PRIMARY" and not c.get("invalidated")), {})
    primary_member = primary.get("primary") or {}
    primary_uuid = primary_member.get("server_uuid")
    sources = {_endpoint(primary_member.get(key)) for key in ("address", "endpoint") if primary_member.get(key)}
    for item in clusters:
        label = _label(item)
        if item.get("read") not in ("route", "this_connection"):
            members = _members(item)
            labels = [m.get("label") or m.get("address") or m.get("server_uuid") for m in members]
            evidence.unavailable(label, "live state of %s" % ", ".join(str(x) for x in labels if x),
                                 item.get("read_error") or "No saved route answered")
            continue
        if item.get("invalidated"):
            # Invalidation is not a fencing operation. Record what was read
            # without promising that an isolated old primary stopped writes.
            evidence.add(label, "invalidated cluster live state inspected", "read", "read")
            continue
        evidence.add(label, "Group Replication quorum", True, item.get("quorum"), known=item.get("quorum") is not None)
        member = item.get("primary") or {}
        evidence.add(label, "cluster primary ONLINE", "ONLINE", member.get("state"),
                     known=bool(member) or (item.get("quorum") is not None and not item.get("read_error")))
        expected_super_read_only = item.get("role") == "REPLICA"
        evidence.add(label, "primary super_read_only", expected_super_read_only, item.get("super_read_only"),
                     known=isinstance(item.get("super_read_only"), bool))
        if item.get("role") == "PRIMARY":
            channel = item.get("channel")
            if not isinstance(channel, dict) or not isinstance(channel.get("present"), bool):
                evidence.unavailable(label, "primary clusterset_replication channel absent or stopped",
                                     (channel or {}).get("read_error") or
                                     "The primary cluster's clusterset_replication channel was not read")
            elif channel["present"] is False:
                evidence.add(label, "primary clusterset_replication channel absent or stopped",
                             "absent or stopped", "absent", matches=True)
            else:
                if channel.get("read_error"):
                    evidence.unavailable(label, "complete primary ClusterSet channel read", channel["read_error"])
                receiver, applier = channel.get("receiver_state"), channel.get("applier_state")
                worker_errors = [w for w in channel.get("workers") or () if w.get("last_error_number")]
                worker_active = [w for w in channel.get("workers") or () if w.get("state") not in (None, "OFF")]
                active_or_error = (receiver in ("ON", "CONNECTING", "ERROR") or applier in ("ON", "ERROR") or
                                   bool(channel.get("last_error_number")) or bool(worker_errors) or bool(worker_active))
                known = active_or_error or (
                    receiver is not None and applier is not None and not channel.get("read_error"))
                stopped = not active_or_error and known and receiver == "OFF" and applier == "OFF"
                actual = ("stopped" if stopped else
                          "receiver %s, applier %s%s" % (
                              receiver or "unknown", applier or "unknown",
                              ", receiver error %s" % channel["last_error_number"]
                              if channel.get("last_error_number") else
                              (", worker error %s" % worker_errors[0].get("last_error_number")
                               if worker_errors else
                               (", active applier worker" if worker_active and applier != "ON" else ""))))
                evidence.add(label, "primary clusterset_replication channel absent or stopped",
                             "absent or stopped", actual, known=known, matches=stopped)
            continue
        if item.get("role") != "REPLICA":
            continue
        action_enabled = item.get("disable_super_read_only_if_primary")
        evidence.add(label, "mysql_disable_super_read_only_if_primary enabled", False, action_enabled,
                     known=isinstance(action_enabled, bool))
        channel = item.get("channel") or {}
        if channel.get("read_error"):
            evidence.unavailable(label, "complete ClusterSet channel read", channel["read_error"])
        evidence.add(label, "clusterset_replication present", True, channel.get("present"),
                     known=isinstance(channel.get("present"), bool))
        for field, fact in (("receiver_state", "receiver ON"), ("applier_state", "applier ON")):
            evidence.add(label, "clusterset_replication " + fact, "ON", channel.get(field),
                         known=channel.get(field) is not None)
        error = channel.get("last_error_number")
        evidence.add(label, "clusterset_replication receiver error", 0, error, known=error is not None)
        for worker in channel.get("workers") or ():
            evidence.add(label, "applier worker %s error" % worker.get("worker_id"), 0,
                         worker.get("last_error_number"), known=worker.get("last_error_number") is not None)
        source_uuid = channel.get("source_uuid")
        evidence.add(label, "clusterset_replication source UUID", primary_uuid, source_uuid,
                     known=bool(primary_uuid) and isinstance(source_uuid, str),
                     matches=bool(primary_uuid) and isinstance(source_uuid, str) and
                             source_uuid.lower() == primary_uuid.lower())
        source = channel.get("source")
        evidence.add(label, "clusterset_replication source", sorted("%s:%s" % s for s in sources), source,
                     known=bool(sources) and source is not None, matches=_endpoint(source) in sources)


def _verify_routing(evidence, plan, before, after):
    base, _, router_name = plan["action"].partition(":")
    if base != "set_routing_option":
        return
    current = (after or {}).get("clusterset") or {}
    parameters = plan.get("parameters") or {}
    option, expected = parameters.get("option"), parameters.get("value")
    label = router_name or "ClusterSet routing defaults"
    if router_name:
        routers = current.get("routers")
        router = next((r for r in routers or () if r.get("name") == router_name), None)
        options = (router or {}).get("options")
    else:
        options = (current.get("routing_options") or {}).get("options")
    if not isinstance(options, dict):
        evidence.unavailable(label, "stored %s" % option, "Router options could not be read")
        return
    if option == "stats_updates_frequency":
        # Shell metadata_storage.cc:3477-3521 resets a global frequency to
        # stored 0 below metadata 2.2, and JSON_REMOVEs it at 2.2+. Per-Router
        # resets always remove the override (:3553-3575). Effective Router
        # defaults cannot prove which representation was actually stored.
        present = expected is not None
        if expected is None and not router_name:
            from .cluster import _version
            version = _version(((after or {}).get("metadata") or {}).get("schema_version"))
            if len(version) != 3 or version < (2, 1, 0):
                evidence.unavailable(label, "stored stats_updates_frequency reset",
                                     "The metadata schema version needed to verify the stored reset was not read")
                return
            present = version < (2, 2, 0)
            expected = 0 if present else None
        wanted = {"present": present, "value": expected}
        actual = {"present": option in options, "value": options.get(option)}
        matches = actual == wanted and (not present or type(actual["value"]) is type(expected))
        evidence.add(label, "stored stats_updates_frequency (Router refresh and traffic are not checked)",
                     wanted, actual, matches=matches)
        return
    if option == "guideline" and expected is not None:
        # The SQL read exposes the stored ID, not a name. Comparing a name
        # against that ID would fabricate a mismatch or successful mapping.
        evidence.unavailable(label, "stored guideline ID resolves to %s" % expected,
                             "The guideline name-to-ID mapping was not read")
        return
    if option == "target_cluster" and expected not in (None, "primary"):
        expected = getattr(before, "_clusterset_groups", {}).get(expected)
        if expected is None:
            evidence.unavailable(label, "stored target_cluster", "The selected cluster group UUID was not read")
            return
    defaults = {"target_cluster": "primary", "invalidated_cluster_policy": "drop_all",
                "use_replica_primary_as_rw": False,
                "read_only_targets": "secondaries", "tags": {}, "guideline": None}
    if isinstance(option, str) and option.startswith("tag:"):
        tags = options.get("tags", {})
        if not isinstance(tags, dict):
            evidence.unavailable(label, "stored %s" % option, "Stored tags are not an object")
            return
        actual = tags.get(option[4:])
    else:
        actual = options.get(option)
        if expected is None and not router_name:
            expected = defaults.get(option)
            if option not in options:
                actual = defaults.get(option)
    # Python equality conflates JSON booleans and numbers, also inside
    # nested tags. Canonical JSON preserves their types and ignores object
    # key order without changing the order of array values.
    matches = (json.dumps(expected, sort_keys=True, allow_nan=False) ==
               json.dumps(actual, sort_keys=True, allow_nan=False))
    evidence.add(label, "stored %s (Router refresh and traffic are not checked)" % option,
                 expected, actual, matches=matches)


def _read_rows(reader, evidence, label, fact, sql):
    try:
        return reader.rows(sql)
    except (ConnectorError, ValueError, OSError) as exc:
        evidence.unavailable(label, fact, safe_problem(exc))
        return None


def _inspect_cleanup(reader, evidence, label, uuid, item, before, keep, dissolve):
    from . import cluster
    previous = next((m for m in _members(item) if m.get("server_uuid") == uuid), {})
    read_replica = previous.get("instance_type") == "read-replica"
    # Performance Schema disabled returns empty tables. That cannot be used
    # as evidence that a channel or a group has been removed.
    rows = _read_rows(reader, evidence, label, "performance_schema enabled",
                      "SELECT @@performance_schema")
    ps_on = bool(rows and cluster._on(rows[0][0]) is True)
    if not ps_on:
        evidence.unavailable(label, "replication cleanup", "Performance Schema was not read as enabled")
    schema = _read_rows(reader, evidence, label, "metadata schema", cluster._MD_SCHEMA_SQL)
    if schema is not None and (keep or dissolve):
        evidence.add(label, "metadata schema present", keep, bool(schema))
    if keep and schema:
        metadata = _read_rows(reader, evidence, label, "standalone cluster metadata",
                              cluster._CLUSTERSET_SNAPSHOT_QUERIES[3][1])
        if metadata is not None:
            metadata = cluster._decode_metadata_names(metadata, (1,))
            wanted_group = getattr(before, "_clusterset_groups", {}).get(item.get("name"))
            groups = [r[3] for r in metadata if len(r) > 3 and r[1] == item.get("name")]
            evidence.add(label, "standalone cluster group identity", wanted_group,
                         groups[0] if len(groups) == 1 else None, known=wanted_group is not None)
    # removeCluster removes the target's membership and may leave a frozen
    # metadata schema on a dissolved instance. Only full ClusterSet dissolve
    # promises schema removal (factsheet / cluster_set_impl.cc:1319-1350).
    if not dissolve and schema is not None:
        membership = (_read_rows(reader, evidence, label, "removed ClusterSet membership",
                                 cluster._CLUSTERSET_SNAPSHOT_QUERIES[1][1])
                      if schema else [])
        if membership is not None:
            membership = cluster._decode_metadata_names(membership, (2, 5))
            stale = [r[2] for r in membership if len(r) > 5 and (keep or r[2] == item.get("name")) and
                     r[5] == (before.get("clusterset") or {}).get("name")]
            evidence.add(label, "old ClusterSet membership absent" if keep else "target ClusterSet membership absent", [], stale)
    if not ps_on:
        return
    group_rows = _read_rows(reader, evidence, label, "Group Replication members",
                            "SELECT MEMBER_ID, MEMBER_STATE, MEMBER_ROLE FROM performance_schema.replication_group_members")
    own = []
    if group_rows is not None:
        active = [r[0] for r in group_rows if len(r) > 1 and r[1] not in (None, "OFFLINE")]
        own = [r for r in group_rows if len(r) > 1 and r[0] == uuid]
        if keep and not read_replica:
            evidence.add(label, "kept member ONLINE", "ONLINE", own[0][1] if len(own) == 1 else None)
        elif not keep:
            evidence.add(label, "Group Replication stopped", [], active)
    fence = _read_rows(reader, evidence, label, "super_read_only", "SELECT @@global.super_read_only")
    if fence is not None:
        value = cluster._on(fence[0][0]) if fence and fence[0] else None
        if keep and not read_replica and len(own) == 1 and len(own[0]) > 2:
            # removeCluster(..., dissolve:false) unfences only the standalone
            # cluster's primary (cluster_set_impl.cc:1597).
            evidence.add(label, "kept member super_read_only", own[0][2] != "PRIMARY", value,
                         known=value is not None and own[0][2] in ("PRIMARY", "SECONDARY"))
        elif not keep and not read_replica and previous.get("state") == "ONLINE":
            # Normal STOP GROUP_REPLICATION enables SRO; removeCluster also
            # fences its reachable group members before leaving the group.
            evidence.add(label, "dissolved member super_read_only", True, value, known=value is not None)
        else:
            # Offline members and read replicas can retain their prior state.
            # An observed value is not a claim that Shell changed it.
            evidence.add(label, "super_read_only observed (prior state may be retained)", "read", value,
                         known=value is not None, matches=True)
    if keep and read_replica:
        for table in ("replication_connection_status", "replication_applier_status"):
            rows = _read_rows(reader, evidence, label, "kept read replica " + table,
                              "SELECT SERVICE_STATE FROM performance_schema.%s "
                              "WHERE CHANNEL_NAME = 'read_replica_replication'" % table)
            if rows is not None:
                evidence.add(label, "kept read replica %s ON" % table, ["ON"], [r[0] for r in rows if r])
    names = "('clusterset_replication')" if keep else "('clusterset_replication', 'read_replica_replication')"
    for table in ("replication_connection_configuration", "replication_connection_status", "replication_applier_status",
                  "replication_asynchronous_connection_failover_managed"):
        rows = _read_rows(reader, evidence, label, table,
                          "SELECT CHANNEL_NAME FROM performance_schema.%s WHERE CHANNEL_NAME IN %s" % (table, names))
        if rows is not None:
            evidence.add(label, "%s managed channels removed" % table, [], [r[0] for r in rows if r])
    if not keep and not read_replica:
        rows = _read_rows(reader, evidence, label, "persisted replication startup settings",
                          "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.persisted_variables "
                          "WHERE VARIABLE_NAME IN ('group_replication_start_on_boot', 'skip_replica_start')")
        if rows is not None:
            values = {str(r[0]).lower(): r[1] for r in rows if len(r) > 1}
            # leave_cluster persists group_replication_start_on_boot=false
            # (common/provision.cc:439). Full dissolve additionally persists
            # skip_replica_start=false (cluster_set/dissolve.cc:571-573).
            restart = values.get("group_replication_start_on_boot")
            evidence.add(label, "persisted group restart disabled", False,
                         cluster._on(restart) if restart is not None else "absent")
            if dissolve:
                skip = values.get("skip_replica_start")
                evidence.add(label, "persisted skip_replica_start disabled", False,
                             cluster._on(skip) if skip is not None else "absent")


def _verify_cleanup(conn, evidence, plan, before, options):
    """Inspect every pre-action member through a freshly matched saved route."""
    from . import cluster
    base, _, suffix = plan["action"].partition(":")
    keep = base == "remove_cluster_keep"
    target = (plan.get("expect", {}).get("clusterset_target") or {}).get("cluster_name") or suffix
    clusters = (before.get("clusterset") or {}).get("clusters") or ()
    if base != "dissolve_clusterset":
        clusters = [c for c in clusters if c.get("name") == target]
    wanted = {}
    for item in clusters:
        for member in _members(item):
            uuid = member.get("server_uuid")
            if uuid:
                wanted[uuid] = (item, member)
            else:
                evidence.unavailable(member.get("label") or member.get("address") or item.get("name"),
                                     "pre-action member UUID", "An instance without a UUID cannot be verified after cleanup")
    if not wanted:
        evidence.unavailable(target or "ClusterSet", "pre-action instance inventory", "No member UUIDs were reviewed")
        return
    remaining = set(wanted)
    own = (before.get("server") or {}).get("server_uuid")
    failures = {}

    def inspect(peer, label, expected_uuid=None):
        reader = cluster._ClusterSetReader(peer)
        try:
            rows = reader.rows("SELECT @@server_uuid")
            uuid = rows[0][0] if rows and rows[0] else None
        except (ConnectorError, ValueError, OSError) as exc:
            if expected_uuid:
                failures[expected_uuid] = safe_problem(exc)
            return
        if expected_uuid is not None and uuid != expected_uuid:
            failures[expected_uuid] = "The saved route now reports another server UUID"
            evidence.add(label, "route server UUID", expected_uuid, uuid)
            return
        if uuid not in remaining:
            if expected_uuid in remaining:
                failures[expected_uuid] = "The saved route now reports another server UUID"
            return
        item, member = wanted[uuid]
        remaining.remove(uuid)
        evidence.add(label, "route server UUID", uuid, uuid)
        _inspect_cleanup(reader, evidence, label, uuid, item, before, keep, base == "dissolve_clusterset")

    if own in wanted:
        inspect(conn, wanted[own][1].get("label") or wanted[own][1].get("address") or "This connection", own)
    opener = options.get("open_profile")
    if opener is not None:
        endpoints = {}
        for uuid, (_, member) in wanted.items():
            for key in ("address", "endpoint"):
                if member.get(key):
                    endpoints.setdefault(_endpoint(member[key]), set()).add(uuid)
        for profile in options.get("profiles") or ():
            uuid = (profile.get("topology_identity") or {}).get("server_uuid")
            if uuid not in remaining:
                address = ((profile.get("host") or "").strip("[]").lower(), _number(profile.get("port")) or 3306)
                matches = endpoints.get(address, set()) & remaining
                uuid = next(iter(matches)) if len(matches) == 1 else None
            if uuid is None or uuid not in remaining:
                continue
            label = profile.get("name") or profile.get("id") or wanted[uuid][1].get("address") or uuid
            try:
                with opener(profile) as peer:
                    inspect(peer, label, uuid)
            except (ConnectorError, ValueError, OSError) as exc:
                failures[uuid] = safe_problem(exc)
    for uuid in sorted(remaining):
        item, member = wanted[uuid]
        label = member.get("label") or member.get("address") or uuid
        evidence.unavailable(label, "%s cleanup" % item.get("name"), failures.get(uuid) or
                             "No saved connection in this group was verified as a route to this member")


def verify(conn, plan, before_report, read_options=None):
    """Return facts, missing reads and contradictory facts after one action.

    ``read_options`` is the same group-scoped keyword dictionary accepted by
    cluster.read_status. This function never retries Shell or changes SQL.
    ``after`` retains the SQL report for the action sheet's existing effects.
    """
    evidence = _Evidence()
    options = dict(read_options or {})
    base = plan["action"].partition(":")[0]
    after = None
    if base != "dissolve_clusterset":
        after = _read_after(conn, before_report, options, evidence)
        if base != "check_clusterset":
            _verify_membership(evidence, plan, before_report, after)
            # A routing change promises stored configuration, not quorum,
            # fencing or healthy channels. Check promises a diagnostic
            # report, validated from raw Shell output by ShellWriter/finish.
            if base != "set_routing_option":
                _verify_live(evidence, before_report, after)
            _verify_routing(evidence, plan, before_report, after)
    if base in ("remove_cluster", "remove_cluster_keep", "dissolve_clusterset"):
        _verify_cleanup(conn, evidence, plan, before_report, options)
    return evidence.result(after)
