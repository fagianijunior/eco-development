"""
Exploration tests for the eco-scheduler-review bugfix spec.

Property 1: Bug Condition - Correção abrangente do agendador eco.

CRITICAL: These tests encode the EXPECTED (correct) behavior described by the
design's `isBugCondition` / Property 1. They are expected to FAIL against the
UNFIXED handler.py / serverless.yml / README.md. The failures surface the
counterexamples that prove each defect exists. They will later validate the fix.

Do NOT fix handler.py or weaken these assertions to make them pass.

Covered cases (design `isBugCondition` sub-conditions):
  C_tags_rds   (1.2, 1.3) - RDS multi-tag eligibility decided tag-by-tag
  C_tags_casing(1.4)      - divergent tag casing ECS (key/value) vs RDS (Key/Value)
  C_pagination (1.5)      - list/describe calls only process the first page
  C_desiredCount(1.6)     - wakeup hardcodes desiredCount=1 instead of original
  C_errors     (1.8)      - a boto3 failure aborts processing of other resources
  C_region     (1.11)     - module-level os.environ['REGION'] raises KeyError
  C_schedule   (1.7)      - cron(UTC) does not match intended America/Fortaleza
  C_naming     (1.1)      - README references a function absent from serverless.yml

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.11**
"""

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_ecs, mock_rds, mock_ec2

REPO_ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"


def _fresh_handler():
    """Import a fresh copy of handler.py with REGION set in the environment.

    handler.py reads region=os.environ['REGION'] at module scope, so REGION
    must be present for the import itself to succeed. Re-importing gives each
    test a clean module bound to the active (moto-mocked) AWS clients.
    """
    os.environ["REGION"] = REGION
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    if "handler" in sys.modules:
        del sys.modules["handler"]
    sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("handler")


# ---------------------------------------------------------------------------
# C_tags_rds (1.2, 1.3): RDS instance with multiple tags, one ecoFriendly=true
# Expected: stopped exactly once, WITHOUT emitting "not part of autoshutdown"
# for the other tags.
# ---------------------------------------------------------------------------
@mock_rds
def test_c_tags_rds_multi_tag_eligible(capsys):
    """Validates: Requirements 1.2, 1.3"""
    rds = boto3.client("rds", region_name=REGION)
    rds.create_db_instance(
        DBInstanceIdentifier="multi-tag-db",
        DBInstanceClass="db.t3.micro",
        Engine="postgres",
        AllocatedStorage=20,
        MasterUsername="admin",
        MasterUserPassword="password123",
        Tags=[
            {"Key": "env", "Value": "prod"},
            {"Key": "ecoFriendly", "Value": "true"},
        ],
    )

    handler = _fresh_handler()
    handler.sleep_rds_all()

    out = capsys.readouterr().out

    # The resource IS eligible (ecoFriendly=true present) so it must be stopped.
    instances = boto3.client("rds", region_name=REGION).describe_db_instances()
    status = instances["DBInstances"][0]["DBInstanceStatus"]
    assert status == "stopped", (
        f"eligible RDS instance should be stopped, got status={status!r}"
    )

    # And it must NOT be mislabeled as 'not part of autoshutdown' because of the
    # 'env' tag evaluated inside the per-tag loop.
    assert "not part of auto" not in out.lower(), (
        "eligible RDS instance was also classified as 'not part of autoshutdown' "
        f"due to tag-by-tag evaluation. Output was:\n{out}"
    )


# ---------------------------------------------------------------------------
# C_tags_casing (1.4): RDS-style tags (Key/Value) passed through the ECS tag
# check (handler.filter_by_tags uses lowercase tag["key"]/tag["value"]).
# Expected: a common helper normalizes casing and recognizes ecoFriendly=true.
# ---------------------------------------------------------------------------
def test_c_tags_casing_rds_tags_through_ecs_check():
    """Validates: Requirements 1.4"""
    handler = _fresh_handler()
    rds_style_tags = [
        {"Key": "ecoFriendly", "Value": "true"},
    ]
    # A common, casing-tolerant tag helper must recognize RDS-style tags as a
    # match. The current filter_by_tags reads tag["key"]/tag["value"] and will
    # raise KeyError on RDS-style (Key/Value) tags.
    try:
        result = handler.filter_by_tags(rds_style_tags)
    except KeyError as exc:
        pytest.fail(
            "filter_by_tags raised KeyError on RDS-style (Key/Value) tags due to "
            f"divergent casing (expected a casing-tolerant helper). Missing key: {exc}"
        )
    assert result is True, (
        "common tag helper should recognize ecoFriendly=true regardless of casing"
    )


# ---------------------------------------------------------------------------
# C_pagination (1.5): two pages of ECS services must all be processed.
# moto does not paginate automatically for small counts, so we simulate a
# paginated backend with botocore Stubber to assert all pages are consumed.
# ---------------------------------------------------------------------------
def test_c_pagination_ecs_services_two_pages():
    """Validates: Requirements 1.5"""
    from botocore.stub import Stubber

    handler = _fresh_handler()
    ecs = boto3.client("ecs", region_name=REGION)
    # Point the handler module at our stubbed client.
    original_client = boto3.client
    cluster_arn = "arn:aws:ecs:us-east-1:123456789012:cluster/eco"

    stubber = Stubber(ecs)

    stubber.add_response(
        "list_clusters",
        {"clusterArns": [cluster_arn]},
    )
    stubber.add_response(
        "describe_clusters",
        {
            "clusters": [
                {
                    "clusterArn": cluster_arn,
                    "clusterName": "eco",
                    "tags": [{"key": "ecoFriendly", "value": "true"}],
                }
            ]
        },
    )
    # list_services returns TWO pages via nextToken. The paginator processes
    # each page as it is fetched, so per-service calls (describe_services +
    # tag_resource for the original desiredCount) are interleaved between the
    # list_services pages. update_service is recorded directly (not stubbed).
    svc_page1 = "arn:aws:ecs:us-east-1:123456789012:service/eco/svc-1"
    svc_page2 = "arn:aws:ecs:us-east-1:123456789012:service/eco/svc-2"
    stubber.add_response(
        "list_services",
        {"serviceArns": [svc_page1], "nextToken": "TOKEN-PAGE-2"},
    )
    # svc-1 processing: read current desiredCount, then persist it as a tag.
    stubber.add_response(
        "describe_services",
        {"services": [{"serviceArn": svc_page1, "desiredCount": 2}]},
    )
    stubber.add_response("tag_resource", {})
    stubber.add_response(
        "list_services",
        {"serviceArns": [svc_page2]},
    )
    # svc-2 processing: read current desiredCount, then persist it as a tag.
    stubber.add_response(
        "describe_services",
        {"services": [{"serviceArn": svc_page2, "desiredCount": 3}]},
    )
    stubber.add_response("tag_resource", {})

    updated_services = []

    def _recording_update(**kwargs):
        updated_services.append(kwargs.get("service"))
        return {}

    def fake_client(service_name, *args, **kwargs):
        if service_name == "ecs":
            return ecs
        return original_client(service_name, *args, **kwargs)

    handler.boto3.client = fake_client
    # Record which services got update_service.
    ecs.update_service = _recording_update

    stubber.activate()
    try:
        handler.sleep_ecs_all()
    finally:
        handler.boto3.client = original_client

    assert svc_page1 in updated_services and svc_page2 in updated_services, (
        "services beyond the first page were ignored (no pagination). "
        f"Processed only: {updated_services}"
    )


# ---------------------------------------------------------------------------
# C_desiredCount (1.6): service with desiredCount=4 -> sleep -> wakeup must
# return to 4, not 1.
# ---------------------------------------------------------------------------
@mock_ecs
def test_c_desired_count_restored_on_wakeup():
    """Validates: Requirements 1.6"""
    ecs = boto3.client("ecs", region_name=REGION)
    ecs.create_cluster(
        clusterName="eco-cluster",
        tags=[{"key": "ecoFriendly", "value": "true"}],
    )
    cluster_arn = ecs.describe_clusters(clusters=["eco-cluster"], include=["TAGS"])[
        "clusters"
    ][0]["clusterArn"]

    ecs.register_task_definition(
        family="eco-task",
        containerDefinitions=[
            {"name": "app", "image": "busybox", "memory": 128, "cpu": 64}
        ],
    )
    ecs.create_service(
        cluster=cluster_arn,
        serviceName="eco-svc",
        taskDefinition="eco-task",
        desiredCount=4,
    )

    handler = _fresh_handler()

    handler.sleep_ecs_all()
    slept = ecs.describe_services(cluster=cluster_arn, services=["eco-svc"])[
        "services"
    ][0]["desiredCount"]
    assert slept == 0, f"sleep should scale service to 0, got {slept}"

    handler.wakeup_ecs_all()
    woke = ecs.describe_services(cluster=cluster_arn, services=["eco-svc"])[
        "services"
    ][0]["desiredCount"]

    assert woke == 4, (
        f"wakeup should restore original desiredCount=4, got {woke} "
        "(wakeup_ecs_all hardcodes desiredCount=1)"
    )


# ---------------------------------------------------------------------------
# C_errors (1.8): a boto3 failure on one resource must not abort processing of
# the remaining resources.
# ---------------------------------------------------------------------------
@mock_ec2
def test_c_errors_isolated_per_resource():
    """Validates: Requirements 1.8"""
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images()["Images"]
    image_id = ami[0]["ImageId"] if ami else "ami-12345678"

    run1 = ec2.run_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "ecoFriendly", "Value": "true"}],
            }
        ],
    )
    run2 = ec2.run_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "ecoFriendly", "Value": "true"}],
            }
        ],
    )
    id1 = run1["Instances"][0]["InstanceId"]
    id2 = run2["Instances"][0]["InstanceId"]

    handler = _fresh_handler()

    # Wrap the real client so stop_instances raises for the first instance only.
    real_client = handler.boto3.client
    stopped = []

    class FailingEC2:
        def __init__(self, inner):
            self._inner = inner

        def describe_instances(self, *a, **k):
            return self._inner.describe_instances(*a, **k)

        def stop_instances(self, InstanceIds):
            if id1 in InstanceIds:
                raise RuntimeError("simulated boto3 failure for %s" % id1)
            stopped.extend(InstanceIds)
            return self._inner.stop_instances(InstanceIds=InstanceIds)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_client(service_name, *args, **kwargs):
        inner = real_client(service_name, *args, **kwargs)
        if service_name == "ec2":
            return FailingEC2(inner)
        return inner

    handler.boto3.client = fake_client
    try:
        # The exception on id1 must NOT prevent id2 from being stopped.
        handler.sleep_ec2_all()
    except RuntimeError as exc:
        handler.boto3.client = real_client
        pytest.fail(
            "a boto3 failure on one EC2 instance aborted the whole run "
            f"(no per-resource error isolation): {exc}"
        )
    finally:
        handler.boto3.client = real_client

    assert id2 in stopped, (
        "the second EC2 instance was not processed after the first one failed "
        f"(expected per-resource isolation). stopped={stopped}"
    )


# ---------------------------------------------------------------------------
# C_region (1.11): importing handler.py without REGION must not raise KeyError.
# handler.py does region=os.environ['REGION'] at module scope. We import in a
# clean subprocess with REGION removed to observe the module-load failure.
# ---------------------------------------------------------------------------
def test_c_region_missing_env_var_no_keyerror():
    """Validates: Requirements 1.11"""
    env = {k: v for k, v in os.environ.items() if k not in ("REGION",)}
    # Remove AWS_REGION too so the only resilient fallback is runtime default.
    env.pop("AWS_REGION", None)

    code = "import importlib; importlib.import_module('handler'); print('IMPORT_OK')"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0 and "IMPORT_OK" in proc.stdout, (
        "importing handler without REGION raised an error "
        "(module-level os.environ['REGION'] should fall back to the runtime "
        f"default). stderr:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# C_schedule (1.7): cron expression (UTC) must correspond to the intended local
# time for America/Fortaleza (UTC-3, no DST).
# ---------------------------------------------------------------------------
def _parse_cron_hour(schedule_expr):
    m = re.search(r"cron\(\s*(\d+)\s+(\d+)\s", schedule_expr)
    assert m, f"could not parse cron expression: {schedule_expr!r}"
    minute = int(m.group(1))
    hour = int(m.group(2))
    return minute, hour


def _schedules_from_serverless():
    with open(REPO_ROOT / "serverless.yml") as fh:
        data = yaml.safe_load(fh)
    result = {}
    for fname, fdef in (data.get("functions") or {}).items():
        for event in fdef.get("events", []) or []:
            sched = event.get("schedule")
            if sched:
                result[fname] = sched
    return result


def test_c_schedule_fortaleza_offset():
    """Validates: Requirements 1.7"""
    # America/Fortaleza is fixed UTC-3 (no DST). A local time L maps to UTC L+3.
    # Intended local times: adventure_time (wakeup) 07:00 local -> 10:00 UTC,
    # sleep_time 23:00 local -> 02:00 UTC (next day).
    FORTALEZA_OFFSET = 3  # hours to ADD to local time to get UTC
    intended_local = {
        "adventure_time": 7,
        "sleep_time": 23,
    }

    schedules = _schedules_from_serverless()
    for fname, intended in intended_local.items():
        assert fname in schedules, f"{fname} has no schedule in serverless.yml"
        _, utc_hour = _parse_cron_hour(schedules[fname])
        expected_utc = (intended + FORTALEZA_OFFSET) % 24
        assert utc_hour == expected_utc, (
            f"{fname}: cron hour {utc_hour} UTC does not match intended local "
            f"{intended}:00 America/Fortaleza (expected {expected_utc} UTC). "
            "The cron runs in UTC regardless of TZ=America/Fortaleza."
        )


# ---------------------------------------------------------------------------
# C_naming (1.1): every function name referenced in README.md must exist in
# serverless.yml (fails with wakeup_time, which is not registered).
# ---------------------------------------------------------------------------
def test_c_naming_readme_functions_registered():
    """Validates: Requirements 1.1"""
    with open(REPO_ROOT / "serverless.yml") as fh:
        data = yaml.safe_load(fh)
    registered = set((data.get("functions") or {}).keys())

    readme = (REPO_ROOT / "README.md").read_text()
    # Function names referenced via `--function <name>`.
    referenced = set(re.findall(r"--function\s+([A-Za-z0-9_]+)", readme))

    assert referenced, "no --function references found in README (sanity check)"
    missing = referenced - registered
    assert not missing, (
        f"README references function(s) not registered in serverless.yml: "
        f"{sorted(missing)} (registered: {sorted(registered)})"
    )
