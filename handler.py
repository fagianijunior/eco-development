import boto3
import os
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_region():
    """Resolve the AWS region resiliently.

    Reads REGION, falling back to AWS_REGION. Returns None when neither is set
    so boto3 falls back to the runtime default region. Resolved lazily (not at
    module scope) so importing this module never raises KeyError when REGION is
    undefined.
    """
    return os.environ.get("REGION") or os.environ.get("AWS_REGION")

def tag_matches(tags, wanted_key, wanted_value):
    """Return True if any tag in the list matches wanted_key AND wanted_value.

    Normalizes the tag casing so the same helper works for every service:
    ECS tags use lowercase ``key``/``value`` while RDS/EC2 use ``Key``/``Value``.
    The whole list is inspected and a single eligibility decision is returned.
    """
    for tag in tags:
        key = tag.get("Key", tag.get("key"))
        value = tag.get("Value", tag.get("value"))
        if key == wanted_key and value == wanted_value:
            return True
    return False

def filter_by_tags(tags):
    return tag_matches(tags, "ecoFriendly", "true")

# Tag key used to persist a service's desiredCount before scaling it to 0 on
# sleep, so wakeup can restore the original value instead of assuming a fixed 1.
ECO_ORIGINAL_DESIRED_COUNT_TAG = "ecoOriginalDesiredCount"
# Safe fallback applied when the original-count tag is absent on wakeup (e.g. the
# service was created while already asleep, or the sleep tagging failed). 1 keeps
# the service running without guessing a larger, potentially costly, scale.
ECO_WAKEUP_FALLBACK_DESIRED_COUNT = 1

def sleep_ecs_all():
    ecs             = boto3.client("ecs", region_name=get_region())
    # Paginate list_clusters so clusters beyond the first page are covered.
    clusterArns     = []
    for page in ecs.get_paginator("list_clusters").paginate():
        clusterArns.extend(page["clusterArns"])
    clustersInfo    = ecs.describe_clusters(
                        clusters=clusterArns,
                        include=['TAGS']
                      )

    for clusterInfo in clustersInfo["clusters"]: 
        tags = clusterInfo["tags"]

        if tag_matches(tags, "ecoFriendly", "true"):
            # Paginate list_services to process every service in the cluster.
            for svc_page in ecs.get_paginator("list_services").paginate(
                cluster=clusterInfo["clusterArn"]
            ):
                for service in svc_page["serviceArns"]:
                    # Isolate failures per service: a boto3 error on one service
                    # is logged and the loop moves on, so the remaining services
                    # (and the later RDS/EC2 cycles) are still processed.
                    try:
                        logger.info('Stopping ECS service: %s', service)

                        # Read the current desiredCount and persist it as a tag on the
                        # service so wakeup can restore the original value instead of
                        # assuming a fixed count of 1.
                        describe = ecs.describe_services(
                            cluster=clusterInfo["clusterArn"],
                            services=[service]
                        )
                        serviceDescriptions = describe["services"]
                        originalDesiredCount = serviceDescriptions[0]["desiredCount"]
                        serviceArn = serviceDescriptions[0]["serviceArn"]

                        ecs.tag_resource(
                            resourceArn=serviceArn,
                            tags=[{
                                "key": ECO_ORIGINAL_DESIRED_COUNT_TAG,
                                "value": str(originalDesiredCount)
                            }]
                        )
                        logger.info(
                            'Persisted original desiredCount=%s for ECS service %s',
                            originalDesiredCount, service
                        )

                        ecs.update_service(
                            cluster=clusterInfo["clusterArn"],
                            service=service,
                            desiredCount=0
                        )
                    except Exception:
                        logger.error(
                            'Failed to stop ECS service %s; continuing with the '
                            'next resource', service, exc_info=True
                        )

def sleep_rds_all():
    rds = boto3.client('rds', region_name=get_region())
    # Paginate describe_db_instances so instances beyond the first page are covered.
    dbInstances = []
    for page in rds.get_paginator('describe_db_instances').paginate():
        dbInstances.extend(page['DBInstances'])
    v_readReplica=[]
    for i in dbInstances:
        readReplica=i['ReadReplicaDBInstanceIdentifiers']
        v_readReplica.extend(readReplica)
    
    for i in dbInstances:
        # Isolate failures per instance: a boto3 error on one DB instance is
        # logged and the loop moves on, so the remaining instances (and the
        # clusters below) are still processed.
        try:
            #The if condition below filters aurora clusters from single instance databases as boto3 commands defer to stop the aurora clusters.
            if i['Engine'] not in ['aurora-mysql','aurora-postgresql']:
                #The if condition below filters Read replicas.
                if i['DBInstanceIdentifier'] not in v_readReplica and len(i['ReadReplicaDBInstanceIdentifiers']) == 0:
                    arn=i['DBInstanceArn']
                    resp2=rds.list_tags_for_resource(ResourceName=arn)
                    #Decide eligibility once after inspecting the whole tag list.
                    eligible = tag_matches(resp2['TagList'], "ecoFriendly", "true")
                    if eligible:
                        #If the resource is eligible, then stop the instance by validating the current status.
                        if i['DBInstanceStatus'] == 'available':
                            rds.stop_db_instance(DBInstanceIdentifier = i['DBInstanceIdentifier'])
                            logger.info('stopping DB instance %s', i['DBInstanceIdentifier'])
                        elif i['DBInstanceStatus'] == 'stopped':
                            logger.info('DB Instance %s is already stopped', i['DBInstanceIdentifier'])
                        elif i['DBInstanceStatus']=='starting':
                            logger.info('DB Instance %s is in starting state. Please stop the cluster after starting is complete', i['DBInstanceIdentifier'])
                        elif i['DBInstanceStatus']=='stopping':
                            logger.info('DB Instance %s is already in stopping state.', i['DBInstanceIdentifier'])
                    else:
                        logger.warning('DB Instance %s is not part of autoshutdown', i['DBInstanceIdentifier'])
                elif i['DBInstanceIdentifier'] in v_readReplica:
                    logger.warning('DB Instance %s is a Read Replica. Cannot shutdown a Read Replica instance', i['DBInstanceIdentifier'])
                else:
                    logger.warning('DB Instance %s has a read replica. Cannot shutdown a database with Read Replica', i['DBInstanceIdentifier'])
        except Exception:
            logger.error(
                'Failed to process DB instance %s during sleep; continuing with '
                'the next resource', i.get('DBInstanceIdentifier'), exc_info=True
            )

    # Paginate describe_db_clusters so clusters beyond the first page are covered.
    dbClusters = []
    for page in rds.get_paginator('describe_db_clusters').paginate():
        dbClusters.extend(page['DBClusters'])
    for i in dbClusters:
        # Isolate failures per cluster so one failing cluster does not abort the
        # rest of the RDS cycle (or the later ECS/EC2 cycles).
        try:
            cluarn=i['DBClusterArn']
            resp2=rds.list_tags_for_resource(ResourceName=cluarn)
            #Decide eligibility once after inspecting the whole tag list.
            eligible = tag_matches(resp2['TagList'], "ecoFriendly", "true")
            if eligible:
                if i['Status'] == 'available':
                    rds.stop_db_cluster(DBClusterIdentifier=i['DBClusterIdentifier'])
                    logger.info('stopping DB cluster %s', i['DBClusterIdentifier'])
                elif i['Status'] == 'stopped':
                    logger.info('DB Cluster %s is already stopped', i['DBClusterIdentifier'])
                elif i['Status']=='starting':
                    logger.info('DB Cluster %s is in starting state. Please stop the cluster after starting is complete', i['DBClusterIdentifier'])
                elif i['Status']=='stopping':
                    logger.info('DB Cluster %s is already in stopping state.', i['DBClusterIdentifier'])
            else:
                logger.warning('DB Cluster %s is not part of autoshutdown', i['DBClusterIdentifier'])
        except Exception:
            logger.error(
                'Failed to process DB cluster %s during sleep; continuing with '
                'the next resource', i.get('DBClusterIdentifier'), exc_info=True
            )

def sleep_ec2_all():
    ec2 = boto3.client('ec2', region_name=get_region())
    # Filter EC2 instances by tags, paginating so every reservation is covered.
    for page in ec2.get_paginator('describe_instances').paginate(Filters=[
        {
            'Name': 'tag:ecoFriendly',
            'Values': ['true']
        }
    ]):
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:
                instanceId=instance['InstanceId']
                # Isolate failures per instance so one failing stop does not
                # abort the remaining EC2 instances.
                try:
                    ec2.stop_instances(InstanceIds=[instanceId])
                except Exception:
                    logger.error(
                        'Failed to stop EC2 instance %s; continuing with the '
                        'next resource', instanceId, exc_info=True
                    )


def wakeup_rds_all():
    rds = boto3.client('rds', region_name=get_region())
    # Paginate describe_db_instances so instances beyond the first page are covered.
    dbInstances = []
    for page in rds.get_paginator('describe_db_instances').paginate():
        dbInstances.extend(page['DBInstances'])

    v_readReplica=[]
    for i in dbInstances:
        readReplica=i['ReadReplicaDBInstanceIdentifiers']
        v_readReplica.extend(readReplica)
    
    for i in dbInstances:
        # Isolate failures per instance so one failing DB instance does not
        # abort the remaining instances (and the clusters below).
        try:
            #The if condition below filters aurora clusters from single instance databases as boto3 commands defer to start the aurora clusters.
            if i['Engine'] not in ['aurora-mysql','aurora-postgresql']:
                #The if condition below filters Read replicas.
                if i['DBInstanceIdentifier'] not in v_readReplica and len(i['ReadReplicaDBInstanceIdentifiers']) == 0:
                    arn=i['DBInstanceArn']
                    resp2=rds.list_tags_for_resource(ResourceName=arn)
                    #Decide eligibility once after inspecting the whole tag list.
                    eligible = tag_matches(resp2['TagList'], "ecoFriendly", "true")
                    if eligible:
                        if i['DBInstanceStatus'] == 'available':
                            logger.info('%s DB instance is already available', i['DBInstanceIdentifier'])
                        elif i['DBInstanceStatus'] == 'stopped':
                            rds.start_db_instance(DBInstanceIdentifier = i['DBInstanceIdentifier'])
                            logger.info('Started DB Instance %s', i['DBInstanceIdentifier'])
                        elif i['DBInstanceStatus']=='starting':
                            logger.info('DB Instance %s is already in starting state', i['DBInstanceIdentifier'])
                        elif i['DBInstanceStatus']=='stopping':
                            logger.info('DB Instance %s is in stopping state. Please wait before starting', i['DBInstanceIdentifier'])
                    else:
                        logger.warning('DB Instance %s is not part of autoshutdown', i['DBInstanceIdentifier'])
                elif i['DBInstanceIdentifier'] in v_readReplica:
                    logger.warning('DB Instance %s is a Read Replica.', i['DBInstanceIdentifier'])
                else:
                    logger.warning('DB Instance %s has a read replica. Cannot shutdown & start a database with Read Replica', i['DBInstanceIdentifier'])
        except Exception:
            logger.error(
                'Failed to process DB instance %s during wakeup; continuing with '
                'the next resource', i.get('DBInstanceIdentifier'), exc_info=True
            )

    # Paginate describe_db_clusters so clusters beyond the first page are covered.
    dbClusters = []
    for page in rds.get_paginator('describe_db_clusters').paginate():
        dbClusters.extend(page['DBClusters'])
    for i in dbClusters:
        # Isolate failures per cluster so one failing cluster does not abort the
        # rest of the RDS wakeup cycle (or the later ECS/EC2 cycles).
        try:
            cluarn=i['DBClusterArn']
            resp2=rds.list_tags_for_resource(ResourceName=cluarn)
            #Decide eligibility once after inspecting the whole tag list.
            eligible = tag_matches(resp2['TagList'], "ecoFriendly", "true")
            if eligible:
                if i['Status'] == 'available':
                    logger.info('%s DB Cluster is already available', i['DBClusterIdentifier'])
                elif i['Status'] == 'stopped':
                    rds.start_db_cluster(DBClusterIdentifier=i['DBClusterIdentifier'])
                    logger.info('Started Cluster %s', i['DBClusterIdentifier'])
                elif i['Status']=='starting':
                    logger.info('cluster %s is already in starting state.', i['DBClusterIdentifier'])
                elif i['Status']=='stopping':
                    logger.info('cluster %s is in stopping state. Please wait before starting', i['DBClusterIdentifier'])
            else:
                logger.warning('DB Cluster %s is not part of autoshutdown', i['DBClusterIdentifier'])
        except Exception:
            logger.error(
                'Failed to process DB cluster %s during wakeup; continuing with '
                'the next resource', i.get('DBClusterIdentifier'), exc_info=True
            )

def wakeup_ec2_all():
    ec2 = boto3.client('ec2', region_name=get_region())
    # Filter EC2 instances by tags, paginating so every reservation is covered.
    for page in ec2.get_paginator('describe_instances').paginate(Filters=[
        {
            'Name': 'tag:ecoFriendly',
            'Values': ['true']
        }
    ]):
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:
                instanceId=instance['InstanceId']
                # Isolate failures per instance so one failing start does not
                # abort the remaining EC2 instances.
                try:
                    ec2.start_instances(InstanceIds=[instanceId])
                except Exception:
                    logger.error(
                        'Failed to start EC2 instance %s; continuing with the '
                        'next resource', instanceId, exc_info=True
                    )

def wakeup_ecs_all():
    ecs             = boto3.client("ecs", region_name=get_region())
    # Paginate list_clusters so clusters beyond the first page are covered.
    clusterArns     = []
    for page in ecs.get_paginator("list_clusters").paginate():
        clusterArns.extend(page["clusterArns"])
    clustersInfo    = ecs.describe_clusters(
                        clusters=clusterArns,
                        include=['TAGS']
                      )

    for clusterInfo in clustersInfo["clusters"]: 
        tags = clusterInfo["tags"]

        if tag_matches(tags, "ecoFriendly", "true"):
            # Paginate list_services to process every service in the cluster.
            for svc_page in ecs.get_paginator("list_services").paginate(
                cluster=clusterInfo["clusterArn"]
            ):
                for service in svc_page["serviceArns"]:
                    # Isolate failures per service: a boto3 error on one service
                    # is logged and the loop moves on, so the remaining services
                    # (and the later RDS/EC2 cycles) are still processed.
                    try:
                        logger.info('Starting ECS service: %s', service)

                        # Restore the desiredCount persisted on sleep. Read it from the
                        # service tag written by sleep_ecs_all. When the tag is absent
                        # (service created while asleep, or sleep tagging failed), fall
                        # back to a safe desiredCount of 1 rather than guessing a larger
                        # value.
                        serviceArn = ecs.describe_services(
                            cluster=clusterInfo["clusterArn"],
                            services=[service]
                        )["services"][0]["serviceArn"]

                        tagList = ecs.list_tags_for_resource(
                            resourceArn=serviceArn
                        )["tags"]

                        desiredCount = ECO_WAKEUP_FALLBACK_DESIRED_COUNT
                        for tag in tagList:
                            key = tag.get("key", tag.get("Key"))
                            value = tag.get("value", tag.get("Value"))
                            if key == ECO_ORIGINAL_DESIRED_COUNT_TAG:
                                try:
                                    desiredCount = int(value)
                                except (TypeError, ValueError):
                                    logger.warning(
                                        'Invalid %s tag value %r on ECS service %s; '
                                        'using fallback desiredCount=%s',
                                        ECO_ORIGINAL_DESIRED_COUNT_TAG, value, service,
                                        ECO_WAKEUP_FALLBACK_DESIRED_COUNT
                                    )
                                break
                        else:
                            logger.warning(
                                'No %s tag on ECS service %s; using fallback '
                                'desiredCount=%s',
                                ECO_ORIGINAL_DESIRED_COUNT_TAG, service,
                                ECO_WAKEUP_FALLBACK_DESIRED_COUNT
                            )

                        logger.info(
                            'Restoring ECS service %s to desiredCount=%s',
                            service, desiredCount
                        )

                        ecs.update_service(
                            cluster=clusterInfo["clusterArn"],
                            service=service,
                            desiredCount=desiredCount
                        )
                    except Exception:
                        logger.error(
                            'Failed to start ECS service %s; continuing with the '
                            'next resource', service, exc_info=True
                        )

def sleep_time(event, context):
    sleep_rds_all()
    sleep_ecs_all()
    sleep_ec2_all()

def adventure_time(event, context):
    wakeup_rds_all()
    wakeup_ecs_all()
    wakeup_ec2_all()
