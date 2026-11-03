"""
Preservation tests for the eco-scheduler-review bugfix spec.

Property 2: Preservation - Seleção por tag e transições de estado inalteradas.

Methodology: OBSERVATION-FIRST. The assertions below were written AFTER
observing the behavior of the UNCORRECTED handler.py for inputs that do NOT
satisfy `isBugCondition` (design.md). They capture the baseline behavior that
the later fix (tasks 3.x) must preserve, and are therefore expected to PASS
against the unfixed code (task 2) and keep passing after the fix (task 3.12).

Scope (inputs where isBugCondition is FALSE):
  - ECS/RDS/EC2 resources carrying a SINGLE `ecoFriendly=true` tag (selection is
    already correct today; multi-tag RDS is C_tags_rds and is out of scope here).
  - Resources WITHOUT `ecoFriendly=true` (already ignored).
  - RDS read replicas / instances WITH read replicas (already skipped).
  - Aurora engines treated as clusters (describe_db_instances vs
    describe_db_clusters separation preserved).
  - RDS/cluster state transitions: available->stop on sleep, stopped->start on
    wakeup, starting/stopping only logged.
  - EC2 `ecoFriendly=true` stopped on sleep, started on wakeup.

These stay within non-bug inputs: single pages (no C_pagination), no injected
boto3 failures (no C_errors), REGION present (no C_region).

Property-based testing (Hypothesis) generates many non-defective inputs
(single eco tags, varied states) to give strong preservation guarantees.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**
"""

import contextlib
import importlib
import io
import os
import sys
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_ecs, mock_rds, mock_ec2

REPO_ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"


def _fresh_handler():
    """Import a fresh handler bound to the active (mocked) AWS clients.

    handler.py reads region=os.environ['REGION'] at module scope, so REGION
    must be present for the import to succeed on the UNCORRECTED code.
    """
    os.environ["REGION"] = REGION
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    if "handler" in sys.modules:
        del sys.modules["handler"]
    sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("handler")


# Generators constrained to the non-bug input space -------------------------

# A non-eco tag: any key/value pair that is NOT ecoFriendly=true. We keep it a
# SINGLE tag so RDS never enters the multi-tag C_tags_rds bug condition.
_plain_keys = st.sampled_from(["env", "team", "owner", "project", "stage", "app"])
_plain_values = st.sampled_from(["prod", "dev", "staging", "qa", "team-a", "false"])


def _non_eco_tag():
    return st.builds(lambda k, v: {"Key": k, "Value": v}, _plain_keys, _plain_values)


# ---------------------------------------------------------------------------
# 3.1 / 3.6 / 3.2 - EC2: eco=true stopped on sleep & started on wakeup;
# non-eco instances are ignored.
# ---------------------------------------------------------------------------
@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(non_eco=_non_eco_tag())
@mock_ec2
def test_preservation_ec2_selection_and_transitions(non_eco):
    """Validates: Requirements 3.1, 3.2, 3.6"""
    ec2 = boto3.client("ec2", region_name=REGION)
    images = ec2.describe_images()["Images"]
    image_id = images[0]["ImageId"] if images else "ami-12345678"

    eco_id = ec2.run_instances(
        ImageId=image_id, MinCount=1, MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance",
                            "Tags": [{"Key": "ecoFriendly", "Value": "true"}]}],
    )["Instances"][0]["InstanceId"]
    plain_id = ec2.run_instances(
        ImageId=image_id, MinCount=1, MaxCount=1,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [non_eco]}],
    )["Instances"][0]["InstanceId"]

    handler = _fresh_handler()

    def states():
        return {i["InstanceId"]: i["State"]["Name"]
                for r in ec2.describe_instances()["Reservations"]
                for i in r["Instances"]}

    handler.sleep_ec2_all()
    after_sleep = states()
    assert after_sleep[eco_id] == "stopped", (
        f"eco EC2 instance should be stopped on sleep, got {after_sleep[eco_id]!r}")
    assert after_sleep[plain_id] == "running", (
        f"non-eco EC2 instance must be ignored on sleep, got {after_sleep[plain_id]!r}")

    handler.wakeup_ec2_all()
    after_wake = states()
    assert after_wake[eco_id] == "running", (
        f"eco EC2 instance should be started on wakeup, got {after_wake[eco_id]!r}")
    assert after_wake[plain_id] == "running", (
        f"non-eco EC2 instance must stay ignored, got {after_wake[plain_id]!r}")


# ---------------------------------------------------------------------------
# 3.1 / 3.2 / 3.5 - RDS single-tag instance: available->stop on sleep,
# stopped->start on wakeup; non-eco instances ignored.
# ---------------------------------------------------------------------------
@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(non_eco=_non_eco_tag(),
       engine=st.sampled_from(["postgres", "mysql", "mariadb"]))
@mock_rds
def test_preservation_rds_single_tag_selection(non_eco, engine):
    """Validates: Requirements 3.1, 3.2, 3.5"""
    rds = boto3.client("rds", region_name=REGION)
    rds.create_db_instance(
        DBInstanceIdentifier="eco-db", DBInstanceClass="db.t3.micro",
        Engine=engine, AllocatedStorage=20, MasterUsername="admin",
        MasterUserPassword="password123",
        Tags=[{"Key": "ecoFriendly", "Value": "true"}],
    )
    rds.create_db_instance(
        DBInstanceIdentifier="plain-db", DBInstanceClass="db.t3.micro",
        Engine=engine, AllocatedStorage=20, MasterUsername="admin",
        MasterUserPassword="password123",
        Tags=[non_eco],
    )

    handler = _fresh_handler()

    def statuses():
        return {i["DBInstanceIdentifier"]: i["DBInstanceStatus"]
                for i in rds.describe_db_instances()["DBInstances"]}

    handler.sleep_rds_all()
    after_sleep = statuses()
    assert after_sleep["eco-db"] == "stopped", (
        f"eligible RDS instance (available) should be stopped on sleep, "
        f"got {after_sleep['eco-db']!r}")
    assert after_sleep["plain-db"] == "available", (
        f"non-eco RDS instance must be ignored on sleep, "
        f"got {after_sleep['plain-db']!r}")

    handler.wakeup_rds_all()
    after_wake = statuses()
    assert after_wake["eco-db"] == "available", (
        f"eligible RDS instance (stopped) should be started on wakeup, "
        f"got {after_wake['eco-db']!r}")
    assert after_wake["plain-db"] == "available", (
        f"non-eco RDS instance must stay ignored on wakeup, "
        f"got {after_wake['plain-db']!r}")


# ---------------------------------------------------------------------------
# 3.4 - Aurora engines are treated as clusters (describe_db_clusters), not as
# isolated instances. A single-tag Aurora cluster is stopped on sleep and
# started on wakeup.
# ---------------------------------------------------------------------------
@settings(max_examples=10, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(engine=st.sampled_from(["aurora-mysql", "aurora-postgresql"]))
@mock_rds
def test_preservation_rds_aurora_treated_as_cluster(engine):
    """Validates: Requirements 3.4, 3.5"""
    rds = boto3.client("rds", region_name=REGION)
    rds.create_db_cluster(
        DBClusterIdentifier="aurora-cl", Engine=engine,
        MasterUsername="admin", MasterUserPassword="password123",
        Tags=[{"Key": "ecoFriendly", "Value": "true"}],
    )

    handler = _fresh_handler()

    handler.sleep_rds_all()
    status = rds.describe_db_clusters()["DBClusters"][0]["Status"]
    assert status == "stopped", (
        f"eligible Aurora cluster should be stopped on sleep (treated via "
        f"describe_db_clusters), got {status!r}")

    handler.wakeup_rds_all()
    status = rds.describe_db_clusters()["DBClusters"][0]["Status"]
    assert status == "available", (
        f"eligible Aurora cluster should be started on wakeup, got {status!r}")


# ---------------------------------------------------------------------------
# Stubbed-RDS helpers for state-transition and read-replica properties where
# moto cannot place an instance into starting/stopping.
# ---------------------------------------------------------------------------
def _db_instance(ident, status, engine="postgres", read_replicas=None):
    return {
        "DBInstanceIdentifier": ident,
        "DBInstanceStatus": status,
        "Engine": engine,
        "DBInstanceArn": f"arn:aws:rds:us-east-1:123456789012:db:{ident}",
        "ReadReplicaDBInstanceIdentifiers": list(read_replicas or []),
    }


def _run_rds_with_stub(operation, instances, tags_by_arn):
    """Drive sleep_rds_all/wakeup_rds_all against a fully stubbed RDS client.

    Returns the list of (action, identifier) calls the handler issued so we can
    assert exactly which state transitions fired. Only the non-bug code paths
    are exercised: single-tag instances, no clusters.
    """
    handler = _fresh_handler()
    rds = boto3.client("rds", region_name=REGION)
    stub = Stubber(rds)

    stub.add_response("describe_db_instances", {"DBInstances": instances})

    v_replica = set()
    for i in instances:
        v_replica.update(i["ReadReplicaDBInstanceIdentifiers"])

    expected_calls = []
    for i in instances:
        if i["Engine"] in ("aurora-mysql", "aurora-postgresql"):
            continue
        if i["DBInstanceIdentifier"] in v_replica or \
                len(i["ReadReplicaDBInstanceIdentifiers"]) != 0:
            continue  # read replicas / primaries-with-replicas are skipped
        arn = i["DBInstanceArn"]
        tags = tags_by_arn.get(arn, [])
        stub.add_response("list_tags_for_resource", {"TagList": tags},
                          {"ResourceName": arn})
        eligible = any(t.get("Key") == "ecoFriendly" and t.get("Value") == "true"
                       for t in tags)
        if not eligible:
            continue
        status = i["DBInstanceStatus"]
        if operation == "sleep" and status == "available":
            stub.add_response("stop_db_instance", {"DBInstance": i},
                              {"DBInstanceIdentifier": i["DBInstanceIdentifier"]})
            expected_calls.append(("stop", i["DBInstanceIdentifier"]))
        elif operation == "wakeup" and status == "stopped":
            stub.add_response("start_db_instance", {"DBInstance": i},
                              {"DBInstanceIdentifier": i["DBInstanceIdentifier"]})
            expected_calls.append(("start", i["DBInstanceIdentifier"]))
        # available/stopped/starting/stopping that don't match are only logged

    stub.add_response("describe_db_clusters", {"DBClusters": []})

    # Record the actual stop/start calls the handler makes.
    actual_calls = []
    real_stop = rds.stop_db_instance
    real_start = rds.start_db_instance

    def rec_stop(**kw):
        actual_calls.append(("stop", kw["DBInstanceIdentifier"]))
        return real_stop(**kw)

    def rec_start(**kw):
        actual_calls.append(("start", kw["DBInstanceIdentifier"]))
        return real_start(**kw)

    rds.stop_db_instance = rec_stop
    rds.start_db_instance = rec_start

    real_client = handler.boto3.client
    handler.boto3.client = lambda sn, *a, **k: rds if sn == "rds" \
        else real_client(sn, *a, **k)

    stub.activate()
    try:
        if operation == "sleep":
            handler.sleep_rds_all()
        else:
            handler.wakeup_rds_all()
    finally:
        handler.boto3.client = real_client
        stub.deactivate()

    return actual_calls, expected_calls


# ---------------------------------------------------------------------------
# 3.5 - RDS state transitions preserved. For a single eco-tagged instance:
#   sleep:  available -> stop; stopped/starting/stopping -> only logged
#   wakeup: stopped   -> start; available/starting/stopping -> only logged
# ---------------------------------------------------------------------------
@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(status=st.sampled_from(["available", "stopped", "starting", "stopping"]),
       operation=st.sampled_from(["sleep", "wakeup"]))
def test_preservation_rds_state_transitions(status, operation):
    """Validates: Requirements 3.5"""
    inst = _db_instance("eco-db", status)
    tags = {inst["DBInstanceArn"]: [{"Key": "ecoFriendly", "Value": "true"}]}

    actual, expected = _run_rds_with_stub(operation, [inst], tags)

    if operation == "sleep" and status == "available":
        assert actual == [("stop", "eco-db")], (
            f"available instance must be stopped on sleep, got {actual}")
    elif operation == "wakeup" and status == "stopped":
        assert actual == [("start", "eco-db")], (
            f"stopped instance must be started on wakeup, got {actual}")
    else:
        # starting/stopping (and mismatched available/stopped) are only logged.
        assert actual == [], (
            f"status={status!r} during {operation} must not trigger a state "
            f"change (only logged), got {actual}")


# ---------------------------------------------------------------------------
# 3.3 - Read replicas and instances WITH read replicas are skipped (no stop/
# start), regardless of their eco tag. The primary's replica list drives this.
# ---------------------------------------------------------------------------
@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(primary_status=st.sampled_from(["available", "stopped"]),
       operation=st.sampled_from(["sleep", "wakeup"]))
def test_preservation_rds_read_replica_skipped(primary_status, operation):
    """Validates: Requirements 3.3"""
    primary = _db_instance("primary-db", primary_status,
                           read_replicas=["replica-db"])
    replica = _db_instance("replica-db", primary_status)
    eco = [{"Key": "ecoFriendly", "Value": "true"}]
    tags = {primary["DBInstanceArn"]: eco, replica["DBInstanceArn"]: eco}

    actual, expected = _run_rds_with_stub(operation, [primary, replica], tags)

    # Both the primary (has a read replica) and the replica itself are skipped:
    # no stop/start for either, even though they carry ecoFriendly=true.
    assert actual == [], (
        f"read replica and primary-with-replica must be skipped (no state "
        f"change) during {operation}, got {actual}")


# ---------------------------------------------------------------------------
# 3.1 / 3.7 - ECS: a cluster with a single ecoFriendly=true tag has its
# service scaled to 0 on sleep (selection preserved). Non-eco clusters ignored.
# ---------------------------------------------------------------------------
@settings(max_examples=15, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(non_eco_key=_plain_keys, non_eco_value=_plain_values)
@mock_ecs
def test_preservation_ecs_tag_selection(non_eco_key, non_eco_value):
    """Validates: Requirements 3.1, 3.2, 3.7"""
    ecs = boto3.client("ecs", region_name=REGION)
    ecs.create_cluster(clusterName="eco-cluster",
                       tags=[{"key": "ecoFriendly", "value": "true"}])
    ecs.create_cluster(clusterName="plain-cluster",
                       tags=[{"key": non_eco_key, "value": non_eco_value}])

    eco_arn = ecs.describe_clusters(clusters=["eco-cluster"], include=["TAGS"])[
        "clusters"][0]["clusterArn"]
    plain_arn = ecs.describe_clusters(clusters=["plain-cluster"], include=["TAGS"])[
        "clusters"][0]["clusterArn"]

    ecs.register_task_definition(
        family="eco-task",
        containerDefinitions=[{"name": "app", "image": "busybox",
                               "memory": 128, "cpu": 64}])
    ecs.create_service(cluster=eco_arn, serviceName="eco-svc",
                       taskDefinition="eco-task", desiredCount=2)
    ecs.create_service(cluster=plain_arn, serviceName="plain-svc",
                       taskDefinition="eco-task", desiredCount=2)

    handler = _fresh_handler()
    handler.sleep_ecs_all()

    eco_count = ecs.describe_services(cluster=eco_arn, services=["eco-svc"])[
        "services"][0]["desiredCount"]
    plain_count = ecs.describe_services(cluster=plain_arn, services=["plain-svc"])[
        "services"][0]["desiredCount"]

    assert eco_count == 0, (
        f"eco ECS service should be scaled to 0 on sleep, got {eco_count}")
    assert plain_count == 2, (
        f"non-eco ECS service must be ignored on sleep, got {plain_count}")


# ---------------------------------------------------------------------------
# 3.7 - Each cycle covers RDS, ECS and EC2. sleep_time / adventure_time call
# the three per-service routines. We assert the orchestration is preserved by
# recording which routines fire.
# ---------------------------------------------------------------------------
def test_preservation_cycle_covers_rds_ecs_ec2():
    """Validates: Requirements 3.7"""
    handler = _fresh_handler()
    called = []

    orig = {name: getattr(handler, name) for name in (
        "sleep_rds_all", "sleep_ecs_all", "sleep_ec2_all",
        "wakeup_rds_all", "wakeup_ecs_all", "wakeup_ec2_all")}
    try:
        for name in orig:
            setattr(handler, name, (lambda n: (lambda *a, **k: called.append(n)))(name))

        handler.sleep_time({}, {})
        assert set(called) == {"sleep_rds_all", "sleep_ecs_all", "sleep_ec2_all"}, (
            f"sleep cycle must cover RDS, ECS and EC2, got {called}")

        called.clear()
        handler.adventure_time({}, {})
        assert set(called) == {"wakeup_rds_all", "wakeup_ecs_all", "wakeup_ec2_all"}, (
            f"wakeup cycle must cover RDS, ECS and EC2, got {called}")
    finally:
        for name, fn in orig.items():
            setattr(handler, name, fn)
