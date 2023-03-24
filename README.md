# compute-lite

# build and release
```
pip install -r requirements.txt
# modify pyproject.toml version
python -m build  # this will generate dist dir
python -m twine upload dist/*  # upload to pypi
```

# usage
```python
import json
import os
import pandas as pd
import numerai_compute_lite as ncl
from numerapi import NumerAPI
from lightgbm import LGBMRegressor

import dotenv
dotenv.load_dotenv() # loads API secrets from .env file

napi = NumerAPI()

napi.download_dataset("v4/train.parquet")
napi.download_dataset("v4/features.json")
training_data = pd.read_parquet('v4/train.parquet')

feature_set = []
with open("v4/features.json", "r") as f:
    feature_metadata = json.load(f)
features = feature_metadata["feature_sets"]["small"]

model = LGBMRegressor()
model.fit(
    training_data[features],
    training_data['target']
)

targets = training_data.columns.str.startswith('target')

model_id = '08e77bbf-036c-4216-b2f7-f8ed4beb88e9'
ncl.deploy(model_id, model, features, 'requirements.txt')
```