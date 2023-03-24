import pandas as pd
from numerapi import NumerAPI
import boto3
import json
import gc
import logging
import math
from time import sleep
import sys
import traceback
from pyarrow.parquet import ParquetFile
import pyarrow.parquet as pq
from numerapi.compute.model_pipeline import DefaultPipeline


logger = logging.getLogger()
logger.setLevel(logging.INFO)

secretsmanager = boto3.client('secretsmanager')
api_keys_secret = secretsmanager.get_secret_value(SecretId='numerai-api-keys')
secret = json.loads(api_keys_secret['SecretString'])


def run(event, context):
    napi = NumerAPI(
        public_id=secret['public_id'],
        secret_key=secret['secret_key']
    )

    model_id = event['model_id']
    if 'data_version' in event:
        data_version = event['data_version']
    else:
        data_version = 'v4'

    if 'invocation_type' in event:
        invocation_type = event['invocation_type']
    else:
        invocation_type = 'submission'

    request_id = context.aws_request_id
    log_stream_name = context.log_stream_name
    set_lambda_status(context.function_name, model_id, request_id, "in_progress", napi, log_stream_name)

    try:
        if invocation_type == 'submission':
            run_submission(napi, model_id, data_version)
        else:
            run_diagnostics(napi, model_id, data_version)

        set_lambda_status(context.function_name, model_id, request_id, "complete", napi, log_stream_name)
    except Exception as ex:
        set_lambda_status(context.function_name, model_id, request_id, "error", napi, log_stream_name)
        exception_type, exception_value, exception_traceback = sys.exc_info()
        traceback_string = traceback.format_exception(exception_type, exception_value, exception_traceback)
        err_msg = json.dumps({
            "errorType": exception_type.__name__,
            "errorMessage": str(exception_value),
            "stackTrace": traceback_string
        })
        logger.error(err_msg)
        return False

    return True


def run_submission(napi, model_id, data_version):
    print(f'Running submission for model_id: {model_id}')
    print(f'Data version: {data_version}')

    current_round = napi.get_current_round()
    live_data = get_data(napi, data_version, 'live')
    logger.info(f'Downloaded live data')

    model_name = 'model'
    model, model_wrapper, features = get_model_wrapper_and_features(model_id)

    live_data.loc[:, f"preds_{model_name}"] = model.predict(live_data.loc[:, features])

    live_data["prediction"] = model_wrapper.post_predict(live_data[f"preds_{model_name}"], round_number=current_round)
    logger.info(f'Live predictions and ranked')

    predict_output_path = f"/tmp/live_predictions_{current_round}.csv"
    if data_version == 'v2':
        # v2 live data id column is not the index so needs to be specified in output here
        live_data[["id", "prediction"]].to_csv(predict_output_path, index=False)
    else:
        live_data["prediction"].to_csv(predict_output_path)

    print(f'submitting {predict_output_path}')
    napi.upload_predictions(predict_output_path, model_id=model_id)
    print('submission complete!')


def run_diagnostics(napi, model_id, data_version):
    print(f'Running submission for model_id: {model_id}')
    print(f'Data version: {data_version}')

    if data_version == 'v2':
        raise AttributeError('Diagnostics for v2 is not supported')

    # download validation data
    data_filename = get_data_filename(data_version, 'validation')
    data_local_path = f"/tmp/{data_version}/{data_filename}"
    napi.download_dataset(f"{data_version}/{data_filename}", data_local_path)
    logger.info(f'Downloaded validation data')

    # predict on validation data
    model_name = 'model'
    model, model_wrapper, features = get_model_wrapper_and_features(model_id)

    predictions = []
    gc.collect()

    parquet_file = pq.ParquetFile(data_local_path)
    for batch in parquet_file.iter_batches(batch_size=32000, columns=features, use_pandas_metadata=True):
        batch_df = batch.to_pandas()
        batch_df.loc[:, f"preds_{model_name}"] = model.predict(batch_df.loc[:, features])
        batch_df["prediction"] = model_wrapper.post_predict(batch_df[f"preds_{model_name}"], round_number=None)
        predictions.append(batch_df["prediction"])

    preds = pd.concat(predictions)
    preds = preds.to_frame()

    predict_output_path = f"/tmp/validation_predictions.csv"
    preds["prediction"].to_csv(predict_output_path)

    diagnostics_id = napi.upload_diagnostics(predict_output_path, tournament=8, model_id=model_id)
    logger.info(f'diagnostics submitted, id: {diagnostics_id}')


def set_lambda_status(function_name, model_id, request_id, status, napi, log_stream_name=None):
    query = f'''
        mutation setModelLambdaStatus($function_name: String!, $model_id: String!, $request_id: String!, $status: String!, $log_stream_name: String) {{
          modelLambdaStatus(
            functionName: $function_name, 
            modelId: $model_id, 
            requestId: $request_id, 
            status: $status,
            logStreamName: $log_stream_name) {{
            requestId
          }}
        }}
        '''
    napi.raw_query(
        query=query,
        authorization=True,
        variables={
            'function_name': function_name,
            'model_id': model_id,
            'request_id': request_id,
            'status': status,
            'log_stream_name': log_stream_name
        }
    )


def get_data_filename(data_version, data_type):
    if data_version in ['v2', 'v3']:
        return f'numerai_{data_type}_data.parquet'
    return f'{data_type}.parquet'


def get_data(napi, data_version, data_type):
    data_filename = get_data_filename(data_version, data_type)
    data_local_path = f"/tmp/{data_version}/{data_filename}"
    napi.download_dataset(f"{data_version}/{data_filename}", data_local_path)
    return pd.read_parquet(data_local_path)


def get_model_wrapper_and_features(model_id):
    s3 = boto3.client('s3')
    aws_account_id = boto3.client('sts').get_caller_identity().get('Account')
    try:
        from custom_pipeline import CustomPipeline
        model_wrapper = CustomPipeline(model_id)
    except Exception as ex:
        print('No custom model wrapper found, using default')
        model_wrapper = DefaultPipeline(model_id)

    pickle_prefix = '/tmp'
    s3.download_file(
        f'numerai-compute-{aws_account_id}',
        f'{model_id}/{model_wrapper.pickled_model_path}',
        f'{pickle_prefix}/{model_wrapper.pickled_model_path}')
    model = model_wrapper.unpickle(pickle_prefix)

    s3.download_file(f'numerai-compute-{aws_account_id}', f'{model_id}/features.json', '/tmp/features.json')
    f = open('/tmp/features.json')
    features = json.load(f)
    logger.info(f'Loaded features {model_id}/features.json')
    return model, model_wrapper, features


def read_parquet_via_pandas_generator(filename, batch_size=128, reads_per_file=5):
    num_rows = ParquetFile(filename).metadata.num_rows
    cache_size = math.ceil(num_rows / batch_size / reads_per_file) * batch_size
    batch_count = math.ceil(cache_size / batch_size)
    for n_read in range(reads_per_file):
        cache = pd.read_parquet(filename).iloc[cache_size * n_read: cache_size * (n_read + 1)].copy()
        gc.collect()
        sleep(1)  # sleep(1) is required to allow measurement of the garbage collector
        for n_batch in range(batch_count):
            yield cache[batch_size * n_batch: batch_size * (n_batch + 1)].copy()



if __name__ == '__main__':
    run({}, {})
