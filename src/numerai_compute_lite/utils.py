import boto3
import botocore.config
import json
import logging
import os
import tempfile
import urllib.request
import collections
import pathlib
import requests
import sys
import time
import zipfile
from pathlib import Path
from platform import python_version
from typing import Dict, Optional

logger = logging.getLogger(__name__)

API_TOURNAMENT_URL = 'https://api-tournament.numer.ai'

def raw_query(query: str, variables: Dict = None,
              authorization: bool = False,
              retries: int = 3, delay: int = 5, backoff: int = 2):
    """Send a raw request to the Numerai's GraphQL API.

    This function allows to build your own queries and fetch results from
    Numerai's GraphQL API. Checkout
    https://medium.com/numerai/getting-started-with-numerais-new-tournament-api-77396e895e72
    for an introduction and https://api-tournament.numer.ai/ for the
    documentation.

    Args:
        query (str): your query
        variables (dict, optional): dict of variables
        authorization (bool, optional): does the request require
            authorization, defaults to `False`
        retries (int): for 5XX errors, how often should numerapi retry
        delay (int): in case of retries, how many seconds to wait between tries
        backoff (int): in case of retries, multiplier to increase the delay between retries

    Returns:
        dict: Result of the request

    Raises:
        ValueError: if something went wrong with the requests. For example,
            this could be a wrongly formatted query or a problem at
            Numerai's end. Have a look at the error messages, in most cases
            the problem is obvious.

    Example:
        >>> query = '''query($tournament: Int!)
                       {rounds(tournament: $tournament number: 0)
                        {number}}'''
        >>> args = {'tournament': 1}
        >>> NumerAPI().raw_query(query, args)
        {'data': {'rounds': [{'number': 104}]}}
    """
    body = {'query': query,
            'variables': variables}
    headers = {'Content-type': 'application/json',
               'Accept': 'application/json'}
    if authorization:
        public_id = os.environ.get('NUMERAI_PUBLIC_ID')
        secret_key = os.environ.get('NUMERAI_SECRET_KEY')
        if public_id and secret_key:
            headers['Authorization'] = f'Token {public_id}${secret_key}'
        else:
            raise ValueError("API keys required for this action.")

    result = post_with_err_handling(
        API_TOURNAMENT_URL, body, headers,
        retries=retries, delay=delay, backoff=backoff)

    if result and "errors" in result:
        err = _handle_call_error(result['errors'])
        # fail!
        raise ValueError(err)
    return result


def _handle_call_error(errors):
    if isinstance(errors, list):
        for error in errors:
            if "message" in error:
                msg = error['message']
    elif isinstance(errors, dict):
        if "detail" in errors:
            msg = errors['detail']
    return msg


def post_with_err_handling(url: str, body: str, headers: Dict,
                           timeout: Optional[int] = None,
                           retries: int = 3, delay: int = 1, backoff: int = 2
                           ) -> Dict:
    """send `post` request and handle (some) errors that might occur"""
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        while 500 <= resp.status_code < 600 and retries > 1:
            time.sleep(delay)
            delay *= backoff
            retries -= 1
            resp = requests.post(url, json=body,
                                 headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as err:
        logger.error(f"Http Error: {err}")
    except requests.exceptions.ConnectionError as err:
        logger.error(f"Error Connecting: {err}")
    except requests.exceptions.Timeout as err:
        logger.error(f"Timeout Error: {err}")
    except requests.exceptions.RequestException as err:
        logger.error(f"Oops, something went wrong: {err}")

    try:
        return resp.json()
    except UnboundLocalError:
        # `r` isn't available, probably because the try/except above failed
        pass
    except json.decoder.JSONDecodeError as err:
        logger.error(f"Did not receive a valid JSON: {err}")

    return {}

def maybe_create_bucket(aws_account_id):
    # use aws account id to create unique bucket name
    bucket_name = f'numerai-compute-{aws_account_id}'

    # create_bucket is idempotent, so it will create or return the existing bucket
    # if this step fails, an exception will be raised
    location = {'LocationConstraint': 'us-west-2'}
    try:
        boto3.client('s3').create_bucket(Bucket=bucket_name, CreateBucketConfiguration=location)
    except Exception as ex:
        pass
    return bucket_name


def upload_to_s3(bucket_name, model_id, file_path):
    s3 = boto3.session.Session().client("s3")
    s3.upload_file(file_path, bucket_name, f'{model_id}/{file_path}')


def maybe_create_zip_file(model_id, bucket_name, requirements_path, model_wrapper_path):
    # ideally we would only do this step if the requirements.txt changes. but until then
    # this will just run every time
    orig_dir = os.getcwd()

    # python 3.10 introduced a ignore_cleanup_errors argument, but we
    # dont want to force users to use 3.10, so the workaround is to
    # manually create the tmp dir and attempt a cleanup at the end.
    # found the answer on this post:
    # https://www.scivision.dev/python-tempfile-permission-error-windows/
    # TODO: create new {model_name} dir here and then put all these files there..
    temp_dir = tempfile.TemporaryDirectory()
    temp_path = Path(temp_dir.name)
    try:
        key = f"codebuild-container-{model_id}.zip"
        os.chdir(temp_path)

        # TODO: need way to support user modified Dockerfile?

        # download dockerfile and buildspec from git
        dockerfile_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/codebuild_files/Dockerfile'
        buildspec_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/codebuild_files/buildspec.yml'
        entrysh_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/codebuild_files/entry.sh'
        lambda_handler_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/codebuild_files/lambda_handler.py'
        model_pipeline_url = 'https://raw.githubusercontent.com/numerai/compute-lite/master/codebuild_files/model_pipeline.py'

        urllib.request.urlretrieve(dockerfile_url, 'Dockerfile')
        urllib.request.urlretrieve(buildspec_url, 'buildspec.yml')
        urllib.request.urlretrieve(entrysh_url, 'entry.sh')
        urllib.request.urlretrieve(lambda_handler_url, 'lambda_handler.py')
        urllib.request.urlretrieve(model_pipeline_url, 'model_pipeline.py')

        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w") as zip:
                for dirname, _, filelist in os.walk("."):
                    for file in filelist:
                        if file in('Dockerfile', 'buildspec.yml', 'entry.sh', 'lambda_handler.py', 'model_pipeline.py'):
                            zip.write(f"{dirname}/{file}")

                # TODO: need to error loudly if any of these files arent found!

                for dirname, _, filelist in os.walk(orig_dir):
                    for file in filelist:
                        if file == requirements_path:
                            print('found requirements.txt')
                            zip.write(f"{dirname}/{file}", file)
                        if file == model_wrapper_path:
                            print(f'found {model_wrapper_path}')
                            zip.write(f"{dirname}/{file}", file)

            tmp.seek(0)
            s3 = boto3.session.Session().client("s3")
            s3.upload_fileobj(tmp, bucket_name, key)
            print(f'Uploaded codebuild zip file: s3://{bucket_name}/{key}')
    finally:
        os.chdir(orig_dir)

    try:
        temp_dir.cleanup()
    except PermissionError:
        pass

    return key


def maybe_create_ecr_repo():
    repo_name = 'numerai-compute-lambda-image'

    client = boto3.client('ecr')
    try:
        ecr_resp = client.create_repository(
            repositoryName=repo_name
        )
        print('created repository')
    except Exception as ex:
        print(f'Repository already exists: {repo_name}. Retrieving..')
        ecr_resp = client.describe_repositories(repositoryNames=[repo_name])
        ecr_resp['repository'] = ecr_resp['repositories'][0]

    # TODO: would be nice to dataclass this response
    return ecr_resp['repository']


def maybe_create_codebuild_project(aws_account_id, bucket_name, zip_file_key, repo_name, model_id):
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
                "codebuild:*",
                "ecr:*",
                "s3:GetObject",
                "s3:GetObjectVersion",
                "logs:*"
            ],
            "Resource": [
                "arn:aws:s3:::numerai-compute-{aws_account_id}/codebuild-container-{model_id}.zip",
                "arn:aws:s3:::numerai-compute-{aws_account_id}/codebuild-container-{model_id}.zip/*",
                "arn:aws:ecr:us-west-2:{aws_account_id}:repository/*",
                "arn:aws:codebuild:us-west-2:{aws_account_id}:build/build-numerai-compute-lambda-image",
                "arn:aws:codebuild:us-west-2:{aws_account_id}:build/build-numerai-compute-lambda-image:*",
                "arn:aws:logs:us-west-2:{aws_account_id}:log-group:*"
            ]
        }},
        {{
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::numerai-compute-{aws_account_id}"
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
            "Resource": "arn:aws:ecr:us-west-2:{aws_account_id}:repository/*"
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
        }},
        {{
            "Effect": "Allow",
            "Action": [
                "logs:*"
            ],
            "Resource": "*"
        }}
    ]
}}
    '''
    maybe_create_policy_and_attach_role(policy_name, policy_document, aws_account_id, codebuild_role)

    # sleep for 10 seconds after creating policy cause boto is a liar and
    # complains about policy not being available when it is
    time.sleep(10)

    session = boto3.session.Session()
    region = session.region_name
    client = session.client("codebuild")
    codebuild_zipfile = f'{bucket_name}/{zip_file_key}'

    # remove patch version from python version
    runtime_version = python_version()
    runtime_version = runtime_version.split('.')[:2]
    runtime_version = '.'.join(runtime_version)
    print(f'runtime version: {runtime_version}')

    base_image = f'public.ecr.aws/lambda/python:{runtime_version}'

    args = {
        "name": cb_project_name,
        "description": f"Build the container {repo_name} for Numerai Compute",
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
                {"name": "RUNTIME_VERSION", "value": runtime_version}
            ],
            "privilegedMode": True,
        },
        "serviceRole": codebuild_role['Arn'],
    }

    retries = 0
    max_retries = 3
    while retries < max_retries:
        try:
            client.create_project(**args)
        except client.exceptions.InvalidInputException as iie:
            # this error happens when the IAM role isn't quite ready yet, so retry after 5 seconds
            print('Codebuild IAM role not created yet, retrying in 5 seconds..')
            retries = retries + 1
            time.sleep(5)
        except client.exceptions.ResourceAlreadyExistsException as ex:
            retries = max_retries
            print('Codebuild project exists, recreating..')
            client.delete_project(name=cb_project_name)
            client.create_project(**args)
            print('Codebuild project recreated')
        except Exception as ex:
            raise ex

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


def maybe_build_container(cb_project_name, log=True):
    try:
        id = start_build(cb_project_name)
        if log:
            logs_for_build(id, wait=True)
        else:
            wait_for_build(id)
    except Exception as ex:
        raise ex


def maybe_create_secret(public_id, secret_key):
    client = boto3.client("secretsmanager")

    secret_name = 'numerai-api-keys'
    try:
        res = client.describe_secret(SecretId=secret_name)
    except Exception as ex:
        print('Secret not found. creating..')

        secret_dict = {'public_id': public_id, 'secret_key': secret_key}
        client.create_secret(
            Name='numerai-api-keys',
            SecretString=json.dumps(secret_dict)
        )


def start_build(cb_project_name):
    args = {"projectName": cb_project_name}
    session = boto3.session.Session()
    client = session.client("codebuild")

    response = client.start_build(**args)
    return response["build"]["id"]


def wait_for_build(id, poll_seconds=10):
    session = boto3.session.Session()
    client = session.client("codebuild")
    status = client.batch_get_builds(ids=[id])
    first = True
    print('starting docker build')
    while status["builds"][0]["buildStatus"] == "IN_PROGRESS":
        if not first:
            print(".", end="")
            sys.stdout.flush()
        first = False
        time.sleep(poll_seconds)
        status = client.batch_get_builds(ids=[id])
    print()
    if status['builds'][0]['buildStatus'] == 'FAILED':
        print('Docker build failed, printing logs..')
        logs_for_build(id, wait=False, session=session)
        raise Exception('Codebuild build failed. See docker logs above for more information')
    print(f"Build complete, status = {status['builds'][0]['buildStatus']}")
    print(f"Logs at {status['builds'][0]['logs']['deepLink']}")


# Position is a tuple that includes the last read timestamp and the number of items that were read
# at that time. This is used to figure out which event to start with on the next read.
Position = collections.namedtuple("Position", ["timestamp", "skip"])


class LogState(object):
    STARTING = 1
    WAIT_IN_PROGRESS = 2
    TAILING = 3
    JOB_COMPLETE = 4
    COMPLETE = 5


def log_stream(client, log_group, stream_name, position):
    start_time, skip = position
    next_token = None

    event_count = 1
    while event_count > 0:
        if next_token is not None:
            token_arg = {"nextToken": next_token}
        else:
            token_arg = {}

        response = client.get_log_events(
            logGroupName=log_group,
            logStreamName=stream_name,
            startTime=start_time,
            startFromHead=False,
            **token_arg,
        )
        next_token = response["nextForwardToken"]
        events = response["events"]
        event_count = len(events)
        if event_count > skip:
            events = events[skip:]
            skip = 0
        else:
            skip = skip - event_count
            events = []
        for ev in events:
            ts, count = position
            if ev["timestamp"] == ts:
                position = Position(timestamp=ts, skip=count + 1)
            else:
                position = Position(timestamp=ev["timestamp"], skip=1)
            yield ev, position


def logs_for_build(
        build_id, wait=False, poll=10, session=None
):  # noqa: C901 - suppress complexity warning for this method

    codebuild = boto3.client("codebuild")
    description = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
    status = description["buildStatus"]

    log_group = description["logs"].get("groupName")
    stream_name = description["logs"].get("streamName")  # The list of log streams
    position = Position(
        timestamp=0, skip=0
    )  # The current position in each stream, map of stream name -> position

    # Increase retries allowed (from default of 4), as we don't want waiting for a build
    # to be interrupted by a transient exception.
    config = botocore.config.Config(retries={"max_attempts": 15})
    client = boto3.client("logs", config=config)

    job_already_completed = False if status == "IN_PROGRESS" else True

    state = (
        LogState.STARTING if wait and not job_already_completed else LogState.COMPLETE
    )
    dot = True

    while state == LogState.STARTING and log_group == None:
        time.sleep(poll)
        description = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        log_group = description["logs"].get("groupName")
        stream_name = description["logs"].get("streamName")

    if state == LogState.STARTING:
        state = LogState.TAILING

    # The loop below implements a state machine that alternates between checking the build status and
    # reading whatever is available in the logs at this point. Note, that if we were called with
    # wait == False, we never check the job status.
    #
    # If wait == TRUE and job is not completed, the initial state is STARTING
    # If wait == FALSE, the initial state is COMPLETE (doesn't matter if the job really is complete).
    #
    # The state table:
    #
    # STATE               ACTIONS                        CONDITION               NEW STATE
    # ----------------    ----------------               -------------------     ----------------
    # STARTING            Pause, Get Status              Valid LogStream Arn     TAILING
    #                                                    Else                    STARTING
    # TAILING             Read logs, Pause, Get status   Job complete            JOB_COMPLETE
    #                                                    Else                    TAILING
    # JOB_COMPLETE        Read logs, Pause               Any                     COMPLETE
    # COMPLETE            Read logs, Exit                                        N/A
    #
    # Notes:
    # - The JOB_COMPLETE state forces us to do an extra pause and read any items that got to Cloudwatch after
    #   the build was marked complete.
    last_describe_job_call = time.time()
    dot_printed = False
    while True:
        for event, position in log_stream(client, log_group, stream_name, position):
            print(event["message"].rstrip())
            if dot:
                dot = False
                if dot_printed:
                    print()
        if state == LogState.COMPLETE:
            break

        time.sleep(poll)
        if dot:
            print(".", end="")
            sys.stdout.flush()
            dot_printed = True
        if state == LogState.JOB_COMPLETE:
            state = LogState.COMPLETE
        elif time.time() - last_describe_job_call >= 30:
            description = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
            status = description["buildStatus"]

            last_describe_job_call = time.time()

            status = description["buildStatus"]

            if status != "IN_PROGRESS":
                print()
                state = LogState.JOB_COMPLETE

    if wait:
        if dot:
            print()


def maybe_create_lambda_function(model_name, ecr, bucket_name, aws_account_id, model_id, external_id):
    lambda_role_name = 'numerai-compute-lambda-execution-role'

    assume_role_policy_document = f'''{{
            "Version": "2012-10-17",
            "Statement": [
                {{
                    "Effect": "Allow",
                    "Principal": {{
                        "AWS": "arn:aws:iam::074996771758:root"
                    }},
                    "Action": "sts:AssumeRole",
                    "Condition": {{
                        "StringEquals": {{
                            "sts:ExternalId": "{external_id}"
                        }}
                    }}
                }},
                {{
                    "Effect": "Allow",
                    "Principal": {{
                        "Service": "lambda.amazonaws.com"
                    }},
                    "Action": "sts:AssumeRole"
                }}
            ]
        }}
    '''
    description = 'Lambda execution role created for Numerai Compute'
    lambda_role = create_or_get_role(lambda_role_name, assume_role_policy_document, description)

    cleaned_model_name = model_name.replace('_', '-')

    function_name = f'numerai-compute-{cleaned_model_name}-submit'

    lambda_policy_doc = f'''{{
                "Version": "2012-10-17",
                "Statement": [
                    {{
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogStream",
                            "logs:PutLogEvents"
                        ],
                        "Resource": "arn:aws:logs:us-west-2:{aws_account_id}:log-group:/aws/lambda/{function_name}:*"
                    }},
                    {{
                        "Effect": "Allow",
                        "Action": [
                            "s3:*",
                            "logs:*"
                        ],
                        "Resource": [
                            "arn:aws:logs:us-west-2:{aws_account_id}:*",
                            "arn:aws:s3:::{bucket_name}",
                            "arn:aws:s3:::{bucket_name}/*"
                        ]
                    }},
                    {{
                        "Effect": "Allow",
                        "Action": [
                            "lambda:*",
                            "secretsmanager:*"
                        ],
                        "Resource": "*"
                    }}
                ]
            }}
    '''
    lambda_policy_name = 'numerai-compute-lambda-execution-policy'
    maybe_create_policy_and_attach_role(lambda_policy_name, lambda_policy_doc, aws_account_id, lambda_role)

    # sleep for 10 seconds after creating policy cause boto is a liar and
    # complains about policy not being available when it is
    time.sleep(10)

    client = boto3.client('lambda')

    repo_uri = ecr['repositoryUri']
    image_uri = f'{repo_uri}:latest'

    retries = 0
    max_retries = 3
    while retries < max_retries:
        try:
            client.update_function_code(
                FunctionName=function_name,
                ImageUri=image_uri,
            )
            print('Updated function code')

            waiter = client.get_waiter('function_updated_v2')
            waiter.wait(
                FunctionName=function_name,
            )

            client.update_function_configuration(
                FunctionName=function_name,
                EphemeralStorage={
                    'Size': 2000
                },
                MemorySize=3008,
                Timeout=300
            )
            print(f'Updated function configuration')
            retries = max_retries

            # set the event invoke config to not retry on error (default is retry twice)
            try:
                client.update_function_event_invoke_config(
                    FunctionName=function_name,
                    MaximumRetryAttempts=0
                )
            except client.exceptions.ResourceNotFoundException as ex:
                client.put_function_event_invoke_config(
                    FunctionName=function_name,
                    MaximumRetryAttempts=0
                )
            print('Lambda update complete')
        except client.exceptions.InvalidParameterValueException as ex:
            print('Lambda IAM role not created yet, retrying in 5 seconds..')
            retries = retries + 1
            time.sleep(5)
        except client.exceptions.ResourceConflictException as ex:
            retries = max_retries
            print(ex)
        except client.exceptions.ResourceNotFoundException as ex:
            retries = max_retries
            print('Creating Lambda function')
            client.create_function(
                FunctionName=function_name,
                PackageType='Image',
                Code={
                    'ImageUri': image_uri
                },
                Role=lambda_role['Arn'],
                MemorySize=3008,
                Timeout=300,
                EphemeralStorage={
                    'Size': 2000
                }
            )
            print('Lambda created')

            # set the event invoke config to not retry on error (default is retry twice)
            client.put_function_event_invoke_config(
                FunctionName=function_name,
                MaximumRetryAttempts=0
            )
        except Exception as ex:
            raise ex

    return lambda_role['Arn'], function_name
