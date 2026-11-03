# Task 1 — Exploration Test Counterexamples (UNFIXED code)

Property 1: Bug Condition. All 8 cases FAILED against the unfixed code, which is
the expected (success) outcome for an exploration test: each failure is a
counterexample proving the defect described by `isBugCondition` exists.

Run: `devenv shell -- python -m pytest tests/test_bug_condition.py -v`
Result: **8 failed** (expected).

| Case | Req | Counterexample (proves the bug) | Root cause in source |
|------|-----|---------------------------------|----------------------|
| C_tags_rds | 1.2, 1.3 | RDS `multi-tag-db` with `[env=prod, ecoFriendly=true]` is also printed as `DB instance multi-tag-db is not part of autoshutdown` while being eligible | `sleep_rds_all`/`wakeup_rds_all` decide eligibility inside the `for tag in TagList` loop; the `elif tag['Key']!="ecoFriendly" and tag['Value']!="true"` branch fires for the `env` tag |
| C_tags_casing | 1.4 | `filter_by_tags([{Key:ecoFriendly, Value:true}])` raises `KeyError: 'key'` | `filter_by_tags` reads lowercase `tag["key"]`/`tag["value"]` (ECS casing) while RDS/EC2 use `Key`/`Value`; no common casing-tolerant helper |
| C_pagination | 1.5 | Two-page `list_services` (`nextToken`) processes only `svc-1`; `svc-2` ignored | `sleep_ecs_all` reads `list_services(...)["serviceArns"]` once, no `get_paginator`/`NextToken` loop |
| C_desiredCount | 1.6 | Service created at `desiredCount=4` → sleep (0) → wakeup returns `1`, not `4` | `wakeup_ecs_all` hardcodes `desiredCount=1`; original count never persisted |
| C_errors | 1.8 | `stop_instances` failing on instance #1 aborts the run; instance #2 never stopped | No `try/except` around per-resource boto3 calls in `sleep_ec2_all` (and others) |
| C_region | 1.11 | Importing `handler` without `REGION` → `KeyError: 'REGION'` at module load (`handler.py:4`) | `region = os.environ['REGION']` at module scope; no `.get()` / `AWS_REGION` / runtime-default fallback |
| C_schedule | 1.7 | `adventure_time` cron hour is `7` UTC; intended 07:00 America/Fortaleza = `10` UTC (sleep `23` local = `02` UTC) | `serverless.yml` uses `cron(0 7 ...)` / `cron(0 23 ...)`; EventBridge cron is UTC and ignores `TZ=America/Fortaleza` |
| C_naming | 1.1 | README `--function wakeup_time` not in registered functions `[adventure_time, sleep_time]` | `README.md` documents `wakeup_time`, which does not exist in `serverless.yml` |

These counterexamples confirm the root-cause analysis in `design.md`. Do not fix
the code against this document; the fix is implemented in later tasks (3.x), and
this same suite must PASS afterwards (task 3.11).
