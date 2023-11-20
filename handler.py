import boto3
import os

region=os.environ['REGION']

def filter_by_tags(tags):
    stag = False
    eco  = False
    
    for tag in tags:
        if tag["key"] == "Environment":
            if tag["value"] == "staging":
                stag = True

        if tag["key"] == "ecoFriendly":
            if tag["value"] == "true":
                eco = True

    return all([stag, eco])

def sleep_ecs_all():
    ecs             = boto3.client("ecs", region_name=region)
    clusterArns     = ecs.list_clusters()["clusterArns"]
    clustersInfo    = ecs.describe_clusters(
                        clusters=clusterArns,
                        include=['TAGS']
                      )

    for clusterInfo in clustersInfo["clusters"]: 
        tags = clusterInfo["tags"]

        if filter_by_tags(tags):
            services = ecs.list_services(cluster=clusterInfo["clusterArn"])
            for service in services["serviceArns"]:

                ecs.update_service(
                    cluster=clusterInfo["clusterArn"],
                    service=service,
                    desiredCount=0
                )

def sleep_rds_all():
    rds = boto3.client('rds', region_name=region)
    response = rds.describe_db_instances()
    v_readReplica=[]
    for i in response['DBInstances']:
        readReplica=i['ReadReplicaDBInstanceIdentifiers']
        v_readReplica.extend(readReplica)
    
    for i in response['DBInstances']:
        #The if condition below filters aurora clusters from single instance databases as boto3 commands defer to stop the aurora clusters.
        if i['Engine'] not in ['aurora-mysql','aurora-postgresql']:
            #The if condition below filters Read replicas.
            if i['DBInstanceIdentifier'] not in v_readReplica and len(i['ReadReplicaDBInstanceIdentifiers']) == 0:
                arn=i['DBInstanceArn']
                resp2=rds.list_tags_for_resource(ResourceName=arn)
                #check if the RDS instance is part of the Auto-Shutdown group.
                if 0==len(resp2['TagList']):
                    print('DB Instance {0} is not part of autoshutdown'.format(i['DBInstanceIdentifier']))
                else:
                    for tag in resp2['TagList']:
                        #If the tags match, then stop the instances by validating the current status.
                        if tag['Key']=="ecoFriendly" and tag['Value']=="true":
                            if i['DBInstanceStatus'] == 'available':
                                rds.stop_db_instance(DBInstanceIdentifier = i['DBInstanceIdentifier'])
                                print('stopping DB instance {0}'.format(i['DBInstanceIdentifier']))
                            elif i['DBInstanceStatus'] == 'stopped':
                                print('DB Instance {0} is already stopped'.format(i['DBInstanceIdentifier']))
                            elif i['DBInstanceStatus']=='starting':
                                print('DB Instance {0} is in starting state. Please stop the cluster after starting is complete'.format(i['DBInstanceIdentifier']))
                            elif i['DBInstanceStatus']=='stopping':
                                print('DB Instance {0} is already in stopping state.'.format(i['DBInstanceIdentifier']))
                        elif tag['Key']!="ecoFriendly" and tag['Value']!="true":
                            print('DB instance {0} is not part of autoshutdown'.format(i['DBInstanceIdentifier']))
                        elif len(tag['Key']) == 0 or len(tag['Value']) == 0:
                            print('DB Instance {0} is not part of auroShutdown'.format(i['DBInstanceIdentifier']))
            elif i['DBInstanceIdentifier'] in v_readReplica:
                print('DB Instance {0} is a Read Replica. Cannot shutdown a Read Replica instance'.format(i['DBInstanceIdentifier']))
            else:
                print('DB Instance {0} has a read replica. Cannot shutdown a database with Read Replica'.format(i['DBInstanceIdentifier']))

    response=rds.describe_db_clusters()
    for i in response['DBClusters']:
        cluarn=i['DBClusterArn']
        resp2=rds.list_tags_for_resource(ResourceName=cluarn)
        if 0==len(resp2['TagList']):
            print('DB Cluster {0} is not part of autoshutdown'.format(i['DBClusterIdentifier']))
        else:
            for tag in resp2['TagList']:
                if tag['Key']=="ecoFriendly" and tag['Value']=="true":
                    if i['Status'] == 'available':
                        rds.stop_db_cluster(DBClusterIdentifier=i['DBClusterIdentifier'])
                        print('stopping DB cluster {0}'.format(i['DBClusterIdentifier']))
                    elif i['Status'] == 'stopped':
                        print('DB Cluster {0} is already stopped'.format(i['DBClusterIdentifier']))
                    elif i['Status']=='starting':
                        print('DB Cluster {0} is in starting state. Please stop the cluster after starting is complete'.format(i['DBClusterIdentifier']))
                    elif i['Status']=='stopping':
                        print('DB Cluster {0} is already in stopping state.'.format(i['DBClusterIdentifier']))
                elif tag['Key'] != "ecoFriendly" and tag['Value'] != "true":
                    print('DB Cluster {0} is not part of autoshutdown'.format(i['DBClusterIdentifier']))
                else:
                    print('DB Instance {0} is not part of auroShutdown'.format(i['DBClusterIdentifier']))

def sleep_ec2_all():
    ec2 = boto3.client('ec2', region_name=region)
    # Filter EC2 instances by tags
    instances = ec2.describe_instances(Filters=[
        {
            'Name': 'tag:Environment',
            'Values': ['staging']
        }, {
            'Name': 'tag:ecoFriendly',
            'Values': ['true']
        }
    ])
    
    for reservation in instances['Reservations']:
        for instance in reservation['Instances']:
            instanceId=instance['InstanceId']
            ec2.stop_instances(InstanceIds=[instanceId])


def wakeup_rds_all():
    rds = boto3.client('rds', region_name=region)
    response = rds.describe_db_instances()

    v_readReplica=[]
    for i in response['DBInstances']:
        readReplica=i['ReadReplicaDBInstanceIdentifiers']
        v_readReplica.extend(readReplica)
    
    for i in response['DBInstances']:
        #The if condition below filters aurora clusters from single instance databases as boto3 commands defer to start the aurora clusters.
        if i['Engine'] not in ['aurora-mysql','aurora-postgresql']:
            #The if condition below filters Read replicas.
            if i['DBInstanceIdentifier'] not in v_readReplica and len(i['ReadReplicaDBInstanceIdentifiers']) == 0:
                arn=i['DBInstanceArn']
                resp2=rds.list_tags_for_resource(ResourceName=arn)
                #check if the RDS instance is part of the Auto-Shutdown group.
                if 0==len(resp2['TagList']):
                    print('DB Instance {0} is not part of autoshutdown'.format(i['DBInstanceIdentifier']))
                else:
                    for tag in resp2['TagList']:
                        if tag['Key']=="ecoFriendly" and tag['Value']=="true":
                            if i['DBInstanceStatus'] == 'available':
                                print('{0} DB instance is already available'.format(i['DBInstanceIdentifier']))
                            elif i['DBInstanceStatus'] == 'stopped':
                                rds.start_db_instance(DBInstanceIdentifier = i['DBInstanceIdentifier'])
                                print('Started DB Instance {0}'.format(i['DBInstanceIdentifier']))
                            elif i['DBInstanceStatus']=='starting':
                                print('DB Instance {0} is already in starting state'.format(i['DBInstanceIdentifier']))
                            elif i['DBInstanceStatus']=='stopping':
                                print('DB Instance {0} is in stopping state. Please wait before starting'.format(i['DBInstanceIdentifier']))
                        elif tag['Key']!="ecoFriendly" and tag['Value']!="true":
                            print('DB instance {0} is not part of autoshutdown'.format(i['DBInstanceIdentifier']))
                        elif len(tag['Key']) == 0 or len(tag['Value']) == 0:
                            print('DB Instance {0} is not part of autoShutdown'.format(i['DBInstanceIdentifier']))
            elif i['DBInstanceIdentifier'] in v_readReplica:
                print('DB Instance {0} is a Read Replica.'.format(i['DBInstanceIdentifier']))
            else:
                print('DB Instance {0} has a read replica. Cannot shutdown & start a database with Read Replica'.format(i['DBInstanceIdentifier']))

    response=rds.describe_db_clusters()
    for i in response['DBClusters']:
        cluarn=i['DBClusterArn']
        resp2=rds.list_tags_for_resource(ResourceName=cluarn)
        if 0==len(resp2['TagList']):
            print('DB Cluster {0} is not part of autoshutdown'.format(i['DBClusterIdentifier']))
        else:
            for tag in resp2['TagList']:
                if tag['Key']=="ecoFriendly" and tag['Value']=="true":
                    if i['Status'] == 'available':
                        print('{0} DB Cluster is already available'.format(i['DBClusterIdentifier']))
                    elif i['Status'] == 'stopped':
                        rds.start_db_cluster(DBClusterIdentifier=i['DBClusterIdentifier'])
                        print('Started Cluster {0}'.format(i['DBClusterIdentifier']))
                    elif i['Status']=='starting':
                        print('cluster {0} is already in starting state.'.format(i['DBClusterIdentifier']))
                    elif i['Status']=='stopping':
                        print('cluster {0} is in stopping state. Please wait before starting'.format(i['DBClusterIdentifier']))
                elif tag['Key'] != "ecoFriendly" and tag['Value'] != "true":
                    print('DB Cluster {0} is not part of autoshutdown'.format(i['DBClusterIdentifier']))
                else:
                    print('DB Instance {0} is not part of autoShutdown'.format(i['DBClusterIdentifier']))

def wakeup_ec2_all():
    ec2 = boto3.client('ec2', region_name=region)
    # Filter EC2 instances by tags
    instances = ec2.describe_instances(Filters=[
        {
            'Name': 'tag:Environment',
            'Values': ['staging']
        }, {
            'Name': 'tag:ecoFriendly',
            'Values': ['true']
        }
    ])
    
    for reservation in instances['Reservations']:
        for instance in reservation['Instances']:
            instanceId=instance['InstanceId']
            ec2.start_instances(InstanceIds=[instanceId])

def wakeup_ecs_all():
    ecs             = boto3.client("ecs", region_name=region)
    clusterArns     = ecs.list_clusters()["clusterArns"]
    clustersInfo    = ecs.describe_clusters(
                        clusters=clusterArns,
                        include=['TAGS']
                      )

    for clusterInfo in clustersInfo["clusters"]: 
        tags = clusterInfo["tags"]

        if filter_by_tags(tags):
            services = ecs.list_services(cluster=clusterInfo["clusterArn"])
            for service in services["serviceArns"]:

                ecs.update_service(
                    cluster=clusterInfo["clusterArn"],
                    service=service,
                    desiredCount=1
                )

def sleep_time(event, context):
    sleep_rds_all()
    sleep_ecs_all()
    sleep_ec2_all()

def wakeup_time(event, context):
    wakeup_rds_all()
    wakeup_ecs_all()
    wakeup_ec2_all()