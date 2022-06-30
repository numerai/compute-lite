import boto3
import os
import urllib.request
import shutil
import sys
import tempfile
import time
import zipfile
import collections
import boto3
import botocore.config
from botocore.exceptions import ClientError
from ncl.codebuild_helpers import start_build, logs_for_build, wait_for_build


def deploy(lambda_handler_path, pickled_model_path, model_id, model_name):
    aws_account_id = boto3.client('sts').get_caller_identity().get('Account')
    bucket_name = maybe_create_bucket(aws_account_id)

    # TODO: only run these steps if requirements.txt file changes
    zip_file_key = maybe_create_zip_file(model_id, bucket_name)
    # TODO: need ability to not use default repo? feature not needed til later tho
    ecr = maybe_create_ecr_repo()

    cb_project_name = maybe_create_codebuild_project(
        aws_account_id,
        bucket_name,
        zip_file_key,
        ecr['repositoryName']
    )

    maybe_build_container(ecr['repositoryName'], cb_project_name, log=True)

    maybe_create_lambda_function(model_name, ecr, bucket_name, aws_account_id)


def maybe_create_bucket(aws_account_id):
    # use aws account id to create unique bucket name
    bucket_name = f'numerai-compute-{aws_account_id}'

    # create_bucket is idempotent, so it will create or return the existing bucket
    # if this step fails, an exception will be raised
    boto3.client('s3').create_bucket(Bucket=bucket_name)
    return bucket_name


def maybe_create_zip_file(model_id, bucket_name):
    # ideally we would only do this step if the requirements.txt changes. but until then
    # this will just run every time
    orig_dir = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as td:

            key = f"codebuild-sagemaker-container-{model_id}.zip"
            os.chdir(td)

            # TODO: need way to support user modified Dockerfile?

            # download dockerfile and buildspec from git
            dockerfile_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/Dockerfile'
            buildspec_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/buildspec.yml'
            entrysh_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/entry.sh'

            urllib.request.urlretrieve(dockerfile_url, 'Dockerfile')
            urllib.request.urlretrieve(buildspec_url, 'buildspec.yml')
            urllib.request.urlretrieve(entrysh_url, 'entry.sh')

            with tempfile.TemporaryFile() as tmp:
                with zipfile.ZipFile(tmp, "w") as zip:
                    for dirname, _, filelist in os.walk("."):
                        for file in filelist:
                            if file == 'Dockerfile' or file == 'buildspec.yml' or file == 'entry.sh':
                                print(f"{dirname}/{file}")
                                zip.write(f"{dirname}/{file}")

                    for dirname, _, filelist in os.walk(orig_dir):
                        for file in filelist:
                            if file == 'requirements.txt' or file == 'lambda_handler.py':
                                print(f"{dirname}/{file}")
                                zip.write(f"{dirname}/{file}", file)
                tmp.seek(0)
                s3 = boto3.session.Session().client("s3")
                s3.upload_fileobj(tmp, bucket_name, key)
                print(f'Uploaded codebuild zip file: s3://{bucket_name}/{key}')
    finally:
        os.chdir(orig_dir)
    return key


def maybe_create_ecr_repo():
    repo_name = 'numerai-compute-lambda-image'

    client = boto3.client('ecr')
    try:
        ecr_resp = client.create_repository(
            repositoryName=repo_name
        )
        print('created repository')
        print(ecr_resp)
    except Exception as ex:
        print(f'Repository already exists: {repo_name}. Retrieving..')
        ecr_resp = client.describe_repositories(repositoryNames=[repo_name])
        ecr_resp['repository'] = ecr_resp['repositories'][0]
        print(ecr_resp)

    # TODO: would be nice to dataclass this response
    return ecr_resp['repository']


def maybe_create_codebuild_project(aws_account_id, bucket_name, zip_file_key, repo_name):
    role_name = 'codebuild-numerai-container-role'
    assume_role_policy_doc = '''{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "codebuild.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    '''
    description = 'Codebuild role created for Numerai Compute'
    codebuild_role = create_or_get_role(role_name, assume_role_policy_doc, description)

    cb_project_name = f"build-{repo_name}"

    policy_name = 'codebuild-numerai-container-policy'
    policy_document = f'''{{
    "Version": "2012-10-17",
    "Statement": [
        {{
            "Effect": "Allow",
            "Action": [
                "codebuild:UpdateProjectVisibility",
                "codebuild:StopBuild",
                "ecr:DescribeImageReplicationStatus",
                "ecr:ListTagsForResource",
                "ecr:ListImages",
                "ecr:BatchGetRepositoryScanningConfiguration",
                "codebuild:RetryBuild",
                "codebuild:UpdateProject",
                "codebuild:StopBuildBatch",
                "codebuild:CreateReport",
                "logs:CreateLogStream",
                "codebuild:UpdateReport",
                "codebuild:BatchPutCodeCoverages",
                "ecr:TagResource",
                "ecr:DescribeRepositories",
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetLifecyclePolicy",
                "codebuild:DeleteBuildBatch",
                "codebuild:RetryBuildBatch",
                "ecr:DescribeImageScanFindings",
                "ecr:GetLifecyclePolicyPreview",
                "ecr:GetDownloadUrlForLayer",
                "logs:CreateLogGroup",
                "logs:PutLogEvents",
                "codebuild:CreateProject",
                "s3:GetObject",
                "codebuild:CreateReportGroup",
                "ecr:UntagResource",
                "codebuild:StartBuildBatch",
                "ecr:BatchGetImage",
                "ecr:DescribeImages",
                "codebuild:StartBuild",
                "codebuild:BatchPutTestCases",
                "s3:GetObjectVersion",
                "ecr:GetRepositoryPolicy",
                "ecr:GetAuthorizationToken",
                "ecr:CreateRepository"
            ],
            "Resource": [
                "arn:aws:s3:::numerai-compute-984109184174/codebuild-sagemaker-container-102052af-a3f4-44ea-b4e4-8d419d3ee4e2.zip",
                "arn:aws:s3:::numerai-compute-984109184174/codebuild-sagemaker-container-102052af-a3f4-44ea-b4e4-8d419d3ee4e2.zip/*",
                "arn:aws:codebuild:us-east-1:984109184174:report-group/create-numerai-container-numerai-compute-*",
                "arn:aws:ecr:us-east-1:984109184174:repository/*",
                "arn:aws:logs:us-east-1:984109184174:log-group:/aws/codebuild/{cb_project_name}",
                "arn:aws:logs:us-east-1:984109184174:log-group:/aws/codebuild/{cb_project_name}:*"
            ]
        }},
        {{
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::numerai-compute-984109184174"
            ],
            "Action": [
                "s3:ListBucket",
                "s3:GetBucketAcl",
                "s3:GetBucketLocation"
            ]
        }},
        {{
            "Effect": "Allow",
            "Action": [
                "ecr:GetRegistryPolicy",
                "ecr:DescribeImageScanFindings",
                "ecr:GetLifecyclePolicyPreview",
                "ecr:GetDownloadUrlForLayer",
                "ecr:DescribeRegistry",
                "ecr:DescribePullThroughCacheRules",
                "ecr:DescribeImageReplicationStatus",
                "ecr:GetAuthorizationToken",
                "ecr:ListTagsForResource",
                "ecr:ListImages",
                "ecr:BatchGetRepositoryScanningConfiguration",
                "ecr:GetRegistryScanningConfiguration",
                "ecr:UntagResource",
                "ecr:BatchGetImage",
                "ecr:DescribeImages",
                "ecr:TagResource",
                "ecr:DescribeRepositories",
                "ecr:BatchCheckLayerAvailability",
                "ecr:GetRepositoryPolicy",
                "ecr:GetLifecyclePolicy",
                "ecr:CreateRepository"
            ],
            "Resource": "arn:aws:ecr:us-east-1:984109184174:repository/*"
        }},
        {{
            "Effect": "Allow",
            "Action": [
                "ecr:*"
            ],
            "Resource": "*"
        }},
        {{
            "Effect": "Allow",
            "Action": [
                "lambda:*"
            ],
            "Resource": "*"
        }}
    ]
}}
    '''
    maybe_create_policy_and_attach_role(policy_name, policy_document, aws_account_id, codebuild_role)

    session = boto3.session.Session()
    region = session.region_name
    client = session.client("codebuild")
    codebuild_zipfile = f'{bucket_name}/{zip_file_key}'

    base_image = 'public.ecr.aws/lambda/python:3.9'

    args = {
        "name": cb_project_name,
        "description": f"Build the container {repo_name} for running notebooks in SageMaker",
        "source": {"type": "S3", "location": codebuild_zipfile},
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": {
            "type": "LINUX_CONTAINER",
            "image": "aws/codebuild/standard:4.0",
            "computeType": "BUILD_GENERAL1_SMALL",
            "environmentVariables": [
                {"name": "AWS_DEFAULT_REGION", "value": region},
                {"name": "AWS_ACCOUNT_ID", "value": aws_account_id},
                {"name": "IMAGE_REPO_NAME", "value": repo_name},
                {"name": "IMAGE_TAG", "value": "latest"},
                {"name": "BASE_IMAGE", "value": base_image},
            ],
            "privilegedMode": True,
        },
        "serviceRole": codebuild_role['Arn'],
    }

    try:
        response = client.create_project(**args)
    except Exception as ex:
        print('Unable to create project, trying delete and recreate..')
        client.delete_project(name=cb_project_name)
        response = client.create_project(**args)
        print('Project recreated')

    return cb_project_name


def create_or_get_role(role_name, assume_role_policy_document, description):
    try:
        iam_response = boto3.client('iam').create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=assume_role_policy_document,
            Description=description,
        )
    except Exception as ex:
        print(f'Unable to create role {role_name}, trying to retrieve..')
        iam_response = boto3.client('iam').get_role(RoleName=role_name)

    # TODO: would be cool to dataclass this
    return iam_response['Role']


def maybe_create_policy_and_attach_role(policy_name, policy_document, aws_account_id, role):
    try:
        policy = boto3.client('iam').create_policy(
            PolicyName=policy_name,
            PolicyDocument=policy_document
        )
    except Exception as ex:
        print(f'Unable to create policy, deleting and recreating..')
        policy_arn = f'arn:aws:iam::{aws_account_id}:policy/{policy_name}'
        try:
            boto3.client('iam').detach_role_policy(
                RoleName=role['RoleName'],
                PolicyArn=policy_arn
            )
        except Exception as ex:
            print(f'Policy already detached. deleting..')

        # if policy has mutliple versions, you gotta delete those
        # before deleting the policy
        policy_versions = boto3.client('iam').list_policy_versions(
            PolicyArn=policy_arn
        )
        for pv in policy_versions['Versions']:
            if pv['IsDefaultVersion']:
                continue
            boto3.client('iam').delete_policy_version(
                PolicyArn=policy_arn,
                VersionId=pv['VersionId']
            )

        boto3.client('iam').delete_policy(
            PolicyArn=policy_arn
        )
        print(f'deleted {policy_arn}')
        policy = boto3.client('iam').create_policy(
            PolicyName=policy_name,
            PolicyDocument=policy_document
        )

    # attach role policy is idempotent, thx jeff
    boto3.client('iam').attach_role_policy(
        RoleName=role['RoleName'],
        PolicyArn=policy['Policy']['Arn']
    )
    return True


def maybe_build_container(repo_name, cb_project_name, log=True):
    try:
        id = start_build(repo_name, cb_project_name)
        if log:
            logs_for_build(id, wait=True)
        else:
            wait_for_build(id)
    except Exception as ex:
        raise ex
    finally:
        print('delete project lol')


def maybe_create_lambda_function(model_name, ecr, bucket_name, aws_account_id):
    lambda_role_name = 'numerai-compute-lambda-execution-role'
    assume_role_policy_document = '''{
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": "arn:aws:iam::074996771758:root"
                    },
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {
                            "sts:ExternalId": "compute-test-012E168ACb9fdc90a4ED9fd6cA2834D9bF5b579e"
                        }
                    }
                },
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "lambda.amazonaws.com"
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        }
    '''
    description = 'Lambda execution role created for Numerai Compute'
    lambda_role = create_or_get_role(lambda_role_name, assume_role_policy_document, description)

    function_name = f'numerai-compute-{model_name}-submit'

    lambda_policy_doc = f'''{{
                "Version": "2012-10-17",
                "Statement": [
                    {{
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogStream",
                            "logs:PutLogEvents"
                        ],
                        "Resource": "arn:aws:logs:us-east-1:{aws_account_id}:log-group:/aws/lambda/{function_name}:*"
                    }},
                    {{
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetLifecycleConfiguration",
                            "s3:GetBucketTagging",
                            "s3:GetInventoryConfiguration",
                            "s3:GetObjectVersionTagging",
                            "s3:ListBucketVersions",
                            "s3:GetBucketLogging",
                            "s3:ListBucket",
                            "s3:GetAccelerateConfiguration",
                            "s3:GetObjectVersionAttributes",
                            "s3:GetBucketPolicy",
                            "s3:GetObjectVersionTorrent",
                            "s3:GetObjectAcl",
                            "s3:GetEncryptionConfiguration",
                            "s3:GetBucketObjectLockConfiguration",
                            "s3:GetIntelligentTieringConfiguration",
                            "s3:GetBucketRequestPayment",
                            "s3:GetObjectVersionAcl",
                            "s3:GetObjectTagging",
                            "s3:GetMetricsConfiguration",
                            "s3:GetBucketOwnershipControls",
                            "s3:GetBucketPublicAccessBlock",
                            "s3:GetBucketPolicyStatus",
                            "s3:ListBucketMultipartUploads",
                            "s3:GetObjectRetention",
                            "s3:GetBucketWebsite",
                            "s3:GetObjectAttributes",
                            "s3:GetBucketVersioning",
                            "s3:GetBucketAcl",
                            "s3:GetObjectLegalHold",
                            "s3:GetBucketNotification",
                            "logs:CreateLogGroup",
                            "s3:GetReplicationConfiguration",
                            "s3:ListMultipartUploadParts",
                            "s3:GetObject",
                            "s3:GetObjectTorrent",
                            "s3:GetBucketCORS",
                            "s3:GetAnalyticsConfiguration",
                            "s3:GetObjectVersionForReplication",
                            "s3:GetBucketLocation",
                            "s3:GetObjectVersion"
                        ],
                        "Resource": [
                            "arn:aws:logs:us-east-1:{aws_account_id}:*",
                            "arn:aws:s3:::{bucket_name}",
                            "arn:aws:s3:::{bucket_name}/*"
                        ]
                    }},
                    {{
                        "Effect": "Allow",
                        "Action": [
                            "s3:ListStorageLensConfigurations",
                            "s3:ListAccessPointsForObjectLambda",
                            "s3:GetAccessPoint",
                            "s3:GetAccountPublicAccessBlock",
                            "s3:ListAllMyBuckets",
                            "s3:ListAccessPoints",
                            "s3:ListJobs",
                            "s3:ListMultiRegionAccessPoints"
                        ],
                        "Resource": "*"
                    }}
                ]
            }}
    '''
    lambda_policy_name = 'numerai-compute-lambda-execution-policy'
    maybe_create_policy_and_attach_role(lambda_policy_name, lambda_policy_doc, aws_account_id, lambda_role)

    client = boto3.client('lambda')

    repo_uri = ecr['repositoryUri']
    image_uri = f'{repo_uri}:latest'

    function_name = f'numerai-compute-{model_name}-submit'

    try:
        resp = client.create_function(
            FunctionName=function_name,
            PackageType='Image',
            Code={
                'ImageUri': image_uri
            },
            Role=lambda_role['Arn'],
            MemorySize=512,
            Timeout=300
        )
    except Exception as ex:
        print('Unable to create function, trying update to latest ECR image..')
        resp = client.update_function_code(
            FunctionName=function_name,
            ImageUri=image_uri
        )
        print('Function updated')
    return resp
