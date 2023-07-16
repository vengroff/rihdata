
import logging
from pathlib import Path
from typing import Dict, Any

import censusdis.data as ced
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shap
import xgboost
import yaml
from censusdis.datasets import ACS5
from matplotlib.ticker import FuncFormatter, PercentFormatter
from argparse import BooleanOptionalAction

import rih.util as util
from rih.loggingargparser import LoggingArgumentParser
from rih.util import xyw, read_data

logger = logging.getLogger(__name__)


def shap_force(
        xgb_params: Dict[str, Any],
        gdf_cbsa_bg: pd.DataFrame,
        year: int,
        group_lh_together: bool,
        random_state: int
):
    # Sample randomly before training.
    gdf_cbsa_bg = gdf_cbsa_bg.sample(frac=0.8, random_state=random_state).reset_index(names="original_index")
    np.random.seed(random_state)

    X, w, y = xyw(gdf_cbsa_bg, year, group_lh_together)

    logger.info(f"Instantiating XGB with {xgb_params}.")

    # Use the optimal hyperparams, but fit on all the data.
    xgb = xgboost.XGBRegressor(eval_metric='rmsle', **xgb_params)
    xgb = xgb.fit(X=X, y=y, sample_weight=w)

    y_hat = xgb.predict(X=X)

    explainer = shap.TreeExplainer(xgb)

    shap_values = explainer.shap_values(X)

    expected_value = explainer.expected_value

    df_forces = pd.DataFrame(
        shap_values,
        columns=X.columns,
        index=X.index,
    )

    # Should be zero.
    check = df_forces.sum(axis="columns") + expected_value - y

    df_forces = df_forces.rename({force_col: f"SHAP_{force_col}" for force_col in X.columns}, axis='columns')

    for force_col in X.columns:
        df_forces[force_col] = X[force_col]
        df_forces[f"rel_SHAP_{force_col}"] = df_forces[f"SHAP_{force_col}"] / y_hat

    df_forces["y_hat"] = y_hat
    df_forces["expected_value"] = expected_value
    df_forces["original_index"] = gdf_cbsa_bg["original_index"]
    df_forces[w.name] = w
    df_forces['random_state'] = random_state

    return df_forces


once = False


def main():

    parser = LoggingArgumentParser(logger)

    parser.add_argument('-v', '--vintage', required=True, type=int, help="Year to get data.")
    parser.add_argument('--group-hispanic-latino', action='store_true')

    parser.add_argument("-p", "--param_file", required=True, help="Parameter file, as created by treegress.py")
    parser.add_argument("-o", "--output-dir", required=True, help="Output directory for plots.")

    parser.add_argument("-r", "--add_relative", default=True, action="store_true")

    parser.add_argument("--background", default=False, action=BooleanOptionalAction)
    parser.add_argument("--bounds", default=False, action=BooleanOptionalAction)

    parser.add_argument("input_file", help="Input file, as created by datagen.py")

    args = parser.parse_args()

    logging.info(f"{args.input_file} + {args.param_file} -> {args.output_dir}")

    with open(args.param_file) as f:
        result = yaml.full_load(f)

    params = result['params']

    gdf_cbsa_bg = read_data(args.input_file, drop_outliers=True)

    n = len(gdf_cbsa_bg.index)
    k = 50
    seed = 0x6A1C55E7

    rng = np.random.default_rng(seed=seed)
    random_states = rng.integers(np.iinfo(np.int32).max, size=(k,), dtype=np.int32)

    df_shap_forces = pd.concat(
        shap_force(params, gdf_cbsa_bg, args.vintage, args.group_hispanic_latino, random_state)
        for random_state in random_states
    )

    year = args.vintage

    all_variables = ced.variables.all_variables(ACS5, year, util.GROUP_RACE_ETHNICITY)

    linear_regression_file = Path(args.input_file.replace(".geojson", ".linreg.yaml"))
    if linear_regression_file.exists():
        with open(linear_regression_file) as f:
            linear_regression_results = yaml.full_load(f)
    else:
        linear_regression_results = None

    for col in df_shap_forces.columns:
        if col.startswith("SHAP_frac_"):
            feature = col.replace("SHAP_frac_", "")

            label = all_variables[all_variables['VARIABLE'] == feature]['LABEL'].iloc[0]
            label = label.replace("Estimate!!Total:!!", "")
            label = label.replace(":!!", "; ")

            shap_force_cols = [
                "original_index", f"frac_{feature}", f"SHAP_frac_{feature}", "random_state", "y_hat",
                util.VARIABLE_TOTAL_OWNER_OCCUPIED
            ]
            if args.add_relative:
                shap_force_cols = shap_force_cols + [f"rel_SHAP_frac_{feature}"]
            df_shap_force = df_shap_forces[shap_force_cols].sort_values(f"frac_{feature}")

            # All the points from the same original index should have the same
            # fraction of the feature.
            same_fraction = df_shap_forces.groupby("original_index")[[f"frac_{feature}"]].apply(
                lambda df_group: len(df_group[f"frac_{feature}"].unique()) == 1
            )
            assert same_fraction.all()

            if linear_regression_results is not None:
                linear_coefficient = linear_regression_results['full']['coefficients'][f'frac_{feature}']
            else:
                linear_coefficient = None

            plot_shap_force(
                feature,
                label,
                args.background,
                args.bounds,
                args.output_dir,
                df_shap_force,
                k,
                linear_coefficient,
                _plot_id(feature, k, n, seed)
            )

            if args.add_relative:
                plot_shap_force(
                    feature,
                    label,
                    args.background,
                    args.bounds,
                    args.output_dir,
                    df_shap_force,
                    k,
                    linear_coefficient,
                    _plot_id(feature, k, n, seed),
                    relative=True
                )


def _plot_id(feature, k, n, seed):
    return f'(f = {feature}; n = {n:,.0f}; k = {k}; s = {seed:08X})'


def plot_shap_force(
        feature, label, background, bounds, output_dir, df_shap_force, k, linear_coefficient, plot_id, relative=False
):

    logger.info(f"Plotting for {feature}: {label}")

    if relative:
        shap_prefix = "rel_SHAP"
    else:
        shap_prefix = "SHAP"

    if linear_coefficient is not None:
        mean_frac_feature = df_shap_force[f'frac_{feature}'].mean()
        y_hat_linear = linear_coefficient * (df_shap_force[f'frac_{feature}'] - mean_frac_feature)
        if relative:
            mean_prediction = df_shap_force['y_hat'].mean()
            y_hat_linear = y_hat_linear / mean_prediction

        df_y_hat = pd.DataFrame(df_shap_force[[f'frac_{feature}']])
        df_y_hat['force_y_hat'] = y_hat_linear

    ax = None

    if background:
        def plot_background(df_shap_force_for_state):
            nonlocal ax
            ax = df_shap_force_for_state.plot.scatter(
                x=f"frac_{feature}",
                y=f"{shap_prefix}_frac_{feature}",
                label=f"Impact of block group's\npercentage on all {k}\nensemble components.",
                legend=ax is None,
                figsize=(12, 8),
                color='lightgray',
                s=1,
                ax=ax
            )

        df_shap_force.groupby('random_state').apply(plot_background)

    df_stats_by_original_index = df_shap_force.groupby(
        ["original_index", util.VARIABLE_TOTAL_OWNER_OCCUPIED, f"frac_{feature}"]
    )[f"{shap_prefix}_frac_{feature}"].agg(
        ["mean", "std"]
    ).reset_index().sort_values(f"frac_{feature}")

    min_force = df_stats_by_original_index['mean'].min()
    max_force = df_stats_by_original_index['mean'].max()

    df_stats_by_original_index['upper'] = (
            df_stats_by_original_index['mean'] + 2 * df_stats_by_original_index['std']
    )
    df_stats_by_original_index['lower'] = (
            df_stats_by_original_index['mean'] - 2 * df_stats_by_original_index['std']
    )

    if bounds:
        for y in ["lower", "upper"]:
            ax = df_stats_by_original_index.plot.scatter(
                x=f"frac_{feature}", y=y,
                color="purple",
                legend=False,
                s=1,
                ax=ax,
                figsize=(12, 8) if ax is None else None,
            )
    ax = df_stats_by_original_index.plot.scatter(
        x=f"frac_{feature}", y="mean",
        color="darkgreen",
        label="Impact of block group's\npercentage on the\nensemble prediction.",
        s=df_stats_by_original_index[util.VARIABLE_TOTAL_OWNER_OCCUPIED] / 50,
        ax=ax,
    )

    if linear_coefficient is not None:
        ax = df_y_hat.plot(
            x=f"frac_{feature}", y="force_y_hat",
            color='purple',
            linestyle='--',
            label="Impact on linear model.",
            ax=ax
        )

    ax.set_xticks(np.arange(0.0, 1.01, 0.1))

    if relative:
        ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    else:
        dollar_formatter = FuncFormatter(
            lambda d, pos: f'\\${d:,.0f}' if d >= 0 else f'(\\${-d:,.0f})'
        )
        ax.yaxis.set_major_formatter(dollar_formatter)

    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    name = Path(output_dir).parent.name.replace('_', ' ')
    ax.set_title(f'Impact of {label} on Median Home Value\n{name}')
    ax.set_xlabel(label)
    ax.set_ylabel("Impact")
    ax.text(
        0.99, 0.01,
        plot_id,
        fontsize=8,
        backgroundcolor='white',
        horizontalalignment='right',
        verticalalignment='bottom',
        transform=ax.transAxes
    )
    ax.axhline(0, color='black', zorder=1)
    ax.grid()

    if relative:
        if max_force > 0.5:
            max_force = max(0.5, df_stats_by_original_index['mean'].quantile(0.98, 'higher'))
        if min_force < -0.5:
            min_force = min(-0.5, df_stats_by_original_index['mean'].quantile(0.02, 'lower'))
        max_force = max(max_force, 0.25)
        min_force = min(min_force, -0.25)
    else:
        max_force = (max_force // 10_000) * 10_000 + 20_000
        min_force = (min_force // 10_000) * 10_000 - 20_000

    ax.set_ylim(min_force, max_force)

    for handle in ax.legend().legend_handles:
        handle._sizes = [25]

    filename = label.replace(" ", "-").replace(";", '')
    if not relative:
        filename = f"abs-{filename}"

    plt.savefig(Path(output_dir) / f"{filename}.png")


if __name__ == "__main__":
    main()