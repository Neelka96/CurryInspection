# Import dependencies

# Scikit Helpers
from sklearn import set_config
from sklearn.base import clone
from sklearn.metrics import make_scorer, cohen_kappa_score
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder, FunctionTransformer
from sklearn.pipeline import Pipeline

# Scikit/Compatible Models
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.linear_model import RidgeClassifier
from sklearn.ensemble import RandomForestClassifier, StackingClassifier
from mord import LogisticIT

# Other major externals
from datetime import datetime, timezone, timedelta
import pandas as pd
import joblib
import json

# Bring in Custom Libraries
from core import get_settings
from ETL.etl_bin import BaseLoader
from ml_lib import LGBMOrdinal, suppress_warnings, read_write_grid, full_est_scores


# Loader to be called by pipeline runner
class ModelLoader(BaseLoader):
    def __init__(
            self,
            name: str,
            numbers: list[str], 
            cycles: list[str], 
            categories: list[str],
            cutoff: timedelta = None,
            hard_date: datetime = None,
            tscv_n: int = 3,
            final_cv_n: int = 3,
            n_jobs: int = -1
        ):
        set_config(transform_output = 'pandas')
        self.cfg = get_settings()
        self.name = name
        if cutoff is not None:
            self.cutoff_date = datetime.now(timezone.utc) - cutoff
        elif hard_date:
            self.cutoff_date = hard_date
        else:
            raise RuntimeError('Model loader reqiures a date split.')
        self.tscv_n = tscv_n
        self.final_cv_n = final_cv_n
        self.n_jobs = n_jobs
        self.numbers = numbers
        self.cycles = cycles
        self.categories = categories
        self.kappa_scorer = make_scorer(cohen_kappa_score, weights = 'quadratic')
        self.cache = joblib.Memory('cache_dir', verbose = 0)


    def split_data(self):
        training_df = self.df[self.df['inspection_date'] <  self.cutoff_date]
        testing_df  = self.df[self.df['inspection_date'] >= self.cutoff_date]

        self.X_tr = training_df.drop(columns = ['inspection_date', 'grade'])
        self.y_tr = training_df['grade']

        self.X_te = testing_df.drop(columns = ['inspection_date', 'grade'])
        self.y_te = testing_df['grade']
        self.all_Xy = {
            'X_tr': self.X_tr,
            'y_tr': self.y_tr,
            'X_te': self.X_te,
            'y_te': self.y_te
        }
        return self

    def _mk_prep(self):
        self.prep = ColumnTransformer(
            [
                ('num', StandardScaler(), self.numbers),
                ('cyc', 'passthrough', self.cycles),
                ('cat', OneHotEncoder(handle_unknown = 'ignore', sparse_output = False), self.categories),
            ],
        )
        self.prep.set_output(transform = 'pandas')
    
    def mk_pipeline(self):
        self.pipe = Pipeline(
            [
                ('prep', self.prep),
                ('clf', LogisticIT())
            ],
            memory = self.cache
        )
        return self

    def _run_search(self, grid: dict):
        search_grid = GridSearchCV(
            self.pipe,
            grid,
            cv = TimeSeriesSplit(n_splits = self.tscv_n),
            scoring = self.kappa_scorer,
            n_jobs = self.n_jobs
        )
        search_grid.fit(self.X_tr, self.y_tr)
        return search_grid

    def _run_lgbm_search(self, grid: dict):
        prep: ColumnTransformer = clone(self.prep)
        prep.set_output(transform = 'pandas')
        pipe = Pipeline(
            [
                ('prep', prep),
                ('to_numpy', FunctionTransformer(func = pd.DataFrame.to_numpy, validate = False)),
                ('clf', LGBMOrdinal())
            ],
            memory = self.cache
        )
        with suppress_warnings():
            search_grid = GridSearchCV(
                pipe,
                grid,
                cv = TimeSeriesSplit(n_splits = self.tscv_n),
                scoring = self.kappa_scorer,
                n_jobs = self.n_jobs
            )
            search_grid.fit(self.X_tr, self.y_tr)
        return search_grid
    
    def write_model(self, model: Pipeline):
        model_name = f'{self.name}.joblib'
        json_ = f'{self.name}_meta.json'
        model_path = self.cfg.storage / model_name
        json_path = self.cfg.storage / json_
        
        joblib.dump(model, model_path, compress = ('gzip', 3))

        _s = model.named_steps['stack']
        meta = {
            'model_file': model_path,
            'train_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'estimators': [name for name, _ in _s.estimators],
            'final_estimator': type(_s.final_estimator).__name__,
            'cv_folds': _s.cv
        }
        with open(json_path, 'w') as f:
            json.dump(meta, f, indent=2)


    def load(self, df: pd.DataFrame) -> None:
        self.df = df
        self.split_data()
        self._mk_prep()
        self.mk_pipeline()

        # --- ordinal logistic models ---
        mord_grid = {
            'clf':                  [LogisticIT()],
            'clf__alpha':           [1.0],
            'clf__max_iter':        [250, 500],
        }
        # --- random forest baseline ---
        randf_grid = {
            'clf':                  [RandomForestClassifier(random_state = 42)],
            'clf__n_estimators':    [100, 250],
            'clf__max_depth':       [15],
            'clf__min_samples_leaf':[1, 3,],
            'clf__class_weight':    ['balanced'],
        }
        # --- gradient-boosting regressor + round-to-ordinal trick ---
        lgbm_grid = {
            'clf':                          [LGBMOrdinal(random_state = 42, verbosity = -1)],
            'clf__n_estimators':            [100, 200],
            'clf__max_depth':               [7, 9],
            'clf__learning_rate':           [0.1],
            'clf__reg_lambda':              [0.1, 1],
        }
        mord_search  = self._run_search( mord_grid)
        read_write_grid(mord_search, self.all_Xy)
        full_est_scores(mord_search, self.all_Xy)

        randf_search = self._run_search(randf_grid)
        read_write_grid(randf_search, self.all_Xy)
        full_est_scores(randf_search, self.all_Xy)

        lgbm_search  = self._run_lgbm_search(lgbm_grid)
        read_write_grid(lgbm_search, self.all_Xy)
        full_est_scores(lgbm_search, self.all_Xy)
        with suppress_warnings():
            estimators = [
                ('logit', mord_search.best_estimator_),
                ('randf', randf_search.best_estimator_),
                ('lgbm', lgbm_search.best_estimator_)
            ]
            _stack = StackingClassifier(
                estimators = estimators,
                final_estimator = RidgeClassifier(alpha = 1.0),
                cv = self.final_cv_n,
                passthrough = False
            )
            prep: ColumnTransformer = clone(self.prep)
            prep.set_output(transform = 'pandas')
            stack = Pipeline(
                [
                    ('prep', prep),
                    ('stack', _stack),
                ],
                memory = self.cache
            )
            stack.fit(self.X_tr, self.y_tr)

        # joblib.dump(stack, self.pipeline, compress = ('gzip', 3))
        self.write_model(stack)
        return None