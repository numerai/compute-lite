import argparse
import pandas as pd
from numerapi import NumerAPI
import boto3
import json
import os
import scipy
import numpy as np


# TODO: figure out how to pass in numerapi keys


def neutralize(df,
               columns,
               neutralizers=None,
               proportion=1.0,
               normalize=True,
               era_col="era"):
    if neutralizers is None:
        neutralizers = []
    unique_eras = df[era_col].unique()
    computed = []
    for u in unique_eras:
        df_era = df[df[era_col] == u]
        scores = df_era[columns].values
        if normalize:
            scores2 = []
            for x in scores.T:
                x = (scipy.stats.rankdata(x, method='ordinal') - .5) / len(x)
                x = scipy.stats.norm.ppf(x)
                scores2.append(x)
            scores = np.array(scores2).T
        exposures = df_era[neutralizers].values

        scores -= proportion * exposures.dot(
            np.linalg.pinv(exposures.astype(np.float32), rcond=1e-6).dot(scores.astype(np.float32)))

        scores /= scores.std(ddof=0)

        computed.append(scores)

    return pd.DataFrame(np.concatenate(computed),
                        columns=columns,
                        index=df.index)


def get_biggest_change_features(corrs, n):
    all_eras = corrs.index.sort_values()
    h1_eras = all_eras[:len(all_eras) // 2]
    h2_eras = all_eras[len(all_eras) // 2:]

    h1_corr_means = corrs.loc[h1_eras, :].mean()
    h2_corr_means = corrs.loc[h2_eras, :].mean()

    corr_diffs = h2_corr_means - h1_corr_means
    worst_n = corr_diffs.abs().sort_values(ascending=False).head(n).index.tolist()
    return worst_n


def run(event, context):
    napi = NumerAPI(
        public_id="V22H76F7UGZXRFHUK7EWRG53TJC34OVW",
        secret_key="6YCUIF523ALJIZE3GKIZU7BROOJRURHZQ3GAAJ4NAZHH7Z5GUWOAQKIY3LNZW753"
    )

    ERA_COL = "era"
    TARGET_COL = "target_nomi_v4_20"
    DATA_TYPE_COL = "data_type"
    EXAMPLE_PREDS_COL = "example_preds"

    os.makedirs(os.path.dirname("/tmp/v4/features.json"), exist_ok=True)
    napi.download_dataset("v4/features.json", "/tmp/v4/features.json")
    with open("/tmp/v4/features.json", "r") as f:
        feature_metadata = json.load(f)
    # features = list(feature_metadata["feature_stats"].keys()) # get all the features
    features = feature_metadata["feature_sets"]["small"]  # get the small feature set
    # features = feature_metadata["feature_sets"]["medium"] # get the medium feature set

    current_round = napi.get_current_round()
    napi.download_dataset("v4/live.parquet", f"/tmp/v4/live_{current_round}.parquet")
    read_columns = features + [ERA_COL, DATA_TYPE_COL, TARGET_COL]
    live_data = pd.read_parquet(f'/tmp/v4/live_{current_round}.parquet',
                                columns=read_columns)

    riskiest_features = ['feature_censorial_leachier_rickshaw', 'feature_trisomic_hagiographic_fragrance',
                         'feature_unsustaining_chewier_adnoun', 'feature_coastal_edible_whang',
                         'feature_steric_coxcombic_relinquishment', 'feature_cyclopedic_maestoso_daguerreotypist',
                         'feature_undrilled_wheezier_countermand', 'feature_unsizable_ancestral_collocutor',
                         'feature_coraciiform_sciurine_reef', 'feature_piping_geotactic_cusp',
                         'feature_corporatist_seborrheic_hopi', 'feature_unpainted_censual_pinacoid',
                         'feature_queenliest_childing_ritual', 'feature_godliest_consistorian_woodpecker',
                         'feature_undisguised_unenviable_stamen', 'feature_unswaddled_inenarrable_goody',
                         'feature_subfusc_furriest_nervule', 'feature_froggier_unlearned_underworkman',
                         'feature_septuple_bonapartean_sanbenito', 'feature_unreproved_cultish_glioma',
                         'feature_ugrian_schizocarpic_skulk', 'feature_iffy_pretty_gumming',
                         'feature_sodding_choosy_eruption', 'feature_tragical_rainbowy_seafarer',
                         'feature_esculent_erotic_epoxy', 'feature_elaborate_intimate_bor',
                         'feature_massive_demisable_spouse', 'feature_burning_phrygian_axinomancy',
                         'feature_entopic_interpreted_subsidiary', 'feature_unventilated_sollar_bason',
                         'feature_fribble_gusseted_stickjaw', 'feature_guardian_frore_rolling',
                         'feature_bijou_penetrant_syringa', 'feature_distressed_bloated_disquietude',
                         'feature_fearsome_merry_bluewing', 'feature_just_flavescent_draff',
                         'feature_mancunian_stalky_charmeuse', 'feature_ecstatic_foundational_crinoidea']

    s3 = boto3.client('s3')
    s3.download_file('numerai-compute-984109184174', 'model.pkl', '/tmp/model.pkl')
    model = pd.read_pickle(f"/tmp/model.pkl")

    model_name = 'model'
    model_expected_features = model.booster_.feature_name()
    live_data.loc[:, f"preds_{model_name}"] = model.predict(
        live_data.loc[:, model_expected_features])

    live_data[f"preds_{model_name}_neutral_riskiest_50"] = neutralize(
        df=live_data,
        columns=[f"preds_{model_name}"],
        neutralizers=riskiest_features,
        proportion=1.0,
        normalize=True,
        era_col=ERA_COL
    )

    model_to_submit = f"preds_{model_name}_neutral_riskiest_50"

    live_data["prediction"] = live_data[model_to_submit].rank(pct=True)

    predict_output_path = f"/tmp/live_predictions_{current_round}.csv"
    live_data["prediction"].to_csv(predict_output_path)

    print(f'submitting {predict_output_path}')
    napi.upload_predictions(predict_output_path, model_id='102052af-a3f4-44ea-b4e4-8d419d3ee4e2')
    print(f''' ______  ______  ______  _____         __  ______  ______    
/\  ___\/\  __ \/\  __ \/\  __-.      /\ \/\  __ \/\  == \   
\ \ \__ \ \ \/\ \ \ \/\ \ \ \/\ \    _\_\ \ \ \/\ \ \  __<   
 \ \_____\ \_____\ \_____\ \____-   /\_____\ \_____\ \_____\ 
  \/_____/\/_____/\/_____/\/____/   \/_____/\/_____/\/_____/ 
                                                             ''')
    return True


if __name__ == '__main__':
    run(None, None)