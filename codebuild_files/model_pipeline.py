import pandas as pd


# This is the default model wrapper that can be extended for
# adding custom code to your submission pipeline
class DefaultPipeline:

    pickled_model_path = 'model.pkl'

    def __init__(self, model_id: str):
        self.model_id = model_id

    def pickle(self, model):
        pd.to_pickle(model, self.pickled_model_path)

    def unpickle(self, pickle_prefix: str):
        return pd.read_pickle(f'{pickle_prefix}/{self.pickled_model_path}')

    def pre_predict(self, data):
        pass

    def post_predict(self, predictions, round_number):
        return predictions.rank(pct=True)

