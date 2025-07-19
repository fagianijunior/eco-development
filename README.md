# test localy
exporte the AWS CREDENTIALS

serverless invoke local --function sleep_time # Shutdown 
serverless invoke local --function wakeup_time # Wakeup


# Deploy using AWS Profile
AWS_SDK_LOAD_CONFIG=1 sls deploy --aws-profile <PROFILE>
