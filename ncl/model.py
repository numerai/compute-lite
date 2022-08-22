import pandas as pd
from numerapi import NumerAPI


class BaseModel:

    def __init__(self, model_id, napi_private_key, napi_public_key):
        self.model_id = model_id
        self.napi = NumerAPI(napi_private_key, napi_public_key)

    def run_train(self, feature_data, target_data):
        # generate requirements.txt
        # save input variables and target to s3 (or api-tournament?)
        # fit model
        # pickle model
        # upload pickled model to s3
        pass

    def run_diagnostics(self):
        # download validation data
        # run predict(validation_data)
        # submit diagnostics
        pass

    def run_submit(self):
        # download live data
        # run predict(live_data)
        # submit live
        pass

    def run_predict(self):
        # get input variables from s3
        # process data (if necessary)
        # download model from s3
        # unpickle model
        # model predict
        pass

    def get_input_variables(self):
        pass

    def download_live_data(self, target_col="target_nomi_v4_20"):
        ERA_COL = "era"
        DATA_TYPE_COL = "data_type"
        EXAMPLE_PREDS_COL = "example_preds"

        current_round = self.napi.get_current_round()
        self.napi.download_dataset("v4/live.parquet", f"/tmp/v4/live_{current_round}.parquet")
        # TODO: get saved feature list from api-tournament?
        features = self.get_input_variables()
        read_columns = features + [ERA_COL, DATA_TYPE_COL, target_col]
        live_data = pd.read_parquet(f'/tmp/v4/live_{current_round}.parquet',
                                    columns=read_columns)

    def process_live_data(self):
        pass

    def download_model_file(self):
        # can we infer this from.. model_id?
        pass

    def unpickle_model(self, pickle_path):
        pass

    def predict(self, live_data, model):
        pass

    def submit(self, preds):
        pass
