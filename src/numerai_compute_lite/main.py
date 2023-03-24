import boto3
import os
import json
from typing import List, Dict
import pkg_resources

from numerai_compute_lite import utils
# TODO: we dont wanna import this file cause then it makes pandas a dependency of this package..
from numerai_compute_lite.model_pipeline import DefaultPipeline

# when call deploy, make a new directory (using model_id? or grab model name?)
# in new directory, download default Dockerfile and default submit_model.py
# update dockerfile to work with this new setup..
# make codebuild zip
#


def deploy(model_id, model, features, requirements_path, data_version='v4', model_pipeline=None,
           custom_pipeline_path=None):
    ncl_version = pkg_resources.get_distribution('numerai_compute_lite').version

    # login to store numerai api keys from environment variables

    # we want to deploy everything to us-west-2
    os.environ['AWS_DEFAULT_REGION'] = 'us-west-2'

    # store numerapi keys in secrets manager
    numerai_public_id = ''
    numerai_secret_key = ''
    utils.maybe_create_secret(numerai_public_id, numerai_secret_key)

    # first get or create bucket so we can upload pickled model and feature list
    aws_account_id = boto3.client('sts').get_caller_identity().get('Account')
    bucket_name = utils.maybe_create_bucket(aws_account_id)


    if model_pipeline is None:
        model_pipeline = DefaultPipeline(model_id)

    model_pipeline.pickle(model)
    with open('features.json', 'w') as f:
        json.dump(features, f)

    # upload model and features to s3
    utils.upload_to_s3(bucket_name, model_id, model_pipeline.pickled_model_path)
    utils.upload_to_s3(bucket_name, model_id, 'features.json')

    # during the beta, we need to make sure that we dont put the beta version
    # of numerapi in the given requirements.txt, otherwise the docker build
    # will fail. so I'm modifying the contents of the file here
    # I'm also adding pyarrow because it's needed in the lambda_handler
    # to open the live data file
    with open(requirements_path, 'r') as file:
        all_packages = file.readlines()

    add_pyarrow = True
    add_boto3 = True
    for package in all_packages:
        if 'pyarrow' in package:
            add_pyarrow = False
        if 'boto3' in package:
            add_boto3 = False
    all_packages = [l for l in all_packages]
    with open(requirements_path, 'w') as file:
        file.writelines(all_packages)
        # TODO: update this branch to the main beta branch before merging
        # file.write("git+https://github.com/numerai/numerapi@chris/compute-lite-beta-test\n")
        if add_pyarrow:
            file.write("pyarrow==9.0.0\n")
        if add_boto3:
            file.write("boto3\n")

    # TODO: only run these steps if requirements.txt file changes
    zip_file_key = utils.maybe_create_zip_file(model_id, bucket_name, requirements_path, custom_pipeline_path)
    # TODO: need ability to not use default repo? feature not needed til later tho
    ecr = utils.maybe_create_ecr_repo()

    cb_project_name = utils.maybe_create_codebuild_project(
        aws_account_id,
        bucket_name,
        zip_file_key,
        ecr['repositoryName'],
        model_id
    )

    utils.maybe_build_container(cb_project_name, log=False)

    query = '''query computeExternalId {
          computeExternalId
        }
        '''
    resp = utils.raw_query(query, authorization=True)
    external_id = resp['data']['computeExternalId']

    query = f'''query modelName {{
        model(modelId: "{model_id}") {{
        name
      }}
    }}
    '''
    resp = utils.raw_query(query, authorization=True)
    model_name = resp['data']['model']['name']
    lambda_role_arn, lambda_function_name = utils.maybe_create_lambda_function(model_name, ecr, bucket_name,
                                                                                       aws_account_id, model_id,
                                                                                       external_id)
    set_lambda_data(model_id, lambda_role_arn, lambda_function_name, ncl_version, data_version)

    print(f'Deploy complete! Go to https://numer.ai/compute to view your deployed model')


def set_lambda_data(model_id, lambda_role_arn, lambda_function_name, numerapi_version, data_version):
    query = f'''mutation setModelLambdaArn {{
        modelLambdaArn(
            modelId:"{model_id}", 
            roleArn:"{lambda_role_arn}",
            functionName:"{lambda_function_name}",
            numerapiVersion:"{numerapi_version}",
            dataVersion:"{data_version}") {{
        lambdaRoleArn
        userId
        lambdaFunctionName
      }}
    }}
    '''
    print('registered lambda with api-tournament')
    utils.raw_query(query, authorization=True)