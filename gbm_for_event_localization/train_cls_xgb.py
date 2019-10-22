import pandas as pd
import numpy as np
from glob import glob
from tqdm import tqdm
import mlcrate as mlc
import sys

from mlcrate.time import Timer
# from mlcrate.xgb import get_importances
import os
import gc
gc.collect()

def get_importances(model, features):
    """Get XGBoost feature importances from an xgboost model and list of features.
    Keyword arguments:
    model -- a trained xgboost.Booster object
    features -- a list of feature names corresponding to the features the model was trained on.
    Returns:
    importance -- A list of (feature, importance) tuples representing sorted importance
    """

    for feature in features:
        assert '\n' not in feature and '\t' not in feature, "\\n and \\t cannot be in feature names"

    fn = 'mlcrate_xgb_{}.fmap'.format(np.random.randint(100000))
    outfile = open(fn, 'w')
    for i, feat in enumerate(features):
        outfile.write('{0}\t{1}\tq\n'.format(i, feat))
    outfile.close()

    importance = model.get_fscore(fmap=fn)
    importance = sorted(importance.items(), key=lambda x: x[1], reverse=True)

    os.remove(fn)

    return importance

def train_kfold(params, x_train, y_train, x_test=None, folds=5, stratify=None, random_state=1337, skip_checks=False, print_imp='final'):
    """Trains a set of XGBoost models with chosen parameters on a KFold split dataset, returning full out-of-fold
    training set predictions (useful for stacking) as well as test set predictions and the models themselves.
    Test set predictions are generated by averaging predictions from all the individual fold models - this means
    1 model fewer has to be trained and from my experience performs better than retraining a single model on the full set.
    Optionally, the split can be stratified along a passed array. Feature importances are also computed and summed across all folds for convenience.
    Keyword arguments:
    params -- Parameters passed to the xgboost model, as well as ['early_stopping_rounds', 'nrounds', 'verbose_eval'], which are passed to xgb.train()
              Defaults: early_stopping_rounds = 50, nrounds = 100000, verbose_eval = 1
    x_train -- The training set features
    y_train -- The training set labels
    x_test (optional) -- The test set features
    folds (default: 5) -- The number of folds to perform
    stratify (optional) -- An array to stratify the splits along
    random_state (default: 1337) -- Random seed for splitting folds
    skip_checks -- By default, this function tries to reorder the test set columns to match the order of the training set columns. Set this to disable this behaviour.
    print_imp -- One of ['every', 'final', None] - 'every' prints importances for every fold, 'final' prints combined importances at the end, None does not print importance
    Returns:
    models -- a list of trained xgboost.Booster objects
    p_train -- Out-of-fold training set predictions (shaped like y_train)
    p_test -- Mean of test set predictions from the models
    imps -- dict with \{feature: importance\} pairs representing the sum feature importance from all the models.
    """

    from sklearn.model_selection import KFold, StratifiedKFold  # Optional dependencies
    from collections import defaultdict
    import numpy as np
    import xgboost as xgb

    assert print_imp in ['every', 'final', None]

    # If it's a dataframe, we can take column names, otherwise just use column indices (eg. for printing importances).
    if hasattr(x_train, 'columns'):
        columns = x_train.columns.values
        columns_exists = True
    else:
        columns = np.arange(x_train.shape[1]).astype(str) # MODIFIED
        columns_exists = False

    x_train = np.asarray(x_train)
    y_train = np.array(y_train)

    if x_test is not None:
        if columns_exists and not skip_checks:
            try:
                x_test = x_test[columns]
            except Exception as e:
                print('[mlcrate] Could not coerce x_test columns to match x_train columns. Set skip_checks=True to run anyway.')
                raise e

        x_test = np.asarray(x_test)
        d_test = xgb.DMatrix(x_test)

        # MODIFIED
        if not skip_checks:
            assert x_train.shape[1] == x_test.shape[1], "x_train and x_test have different numbers of features."
    else:
        d_test = None

    print('[mlcrate] Training {} {}XGBoost models on training set {} {}'.format(folds, 'stratified ' if stratify is not None else '',
            x_train.shape, 'with test set {}'.format(x_test.shape) if x_test is not None else 'without a test set'))

    # Init a timer to get fold durations
    t = Timer()

    if isinstance(folds, int):
        if stratify is not None:
            kf = StratifiedKFold(n_splits=folds, shuffle=True, random_state=random_state)
            splits = kf.split(x_train, stratify)
        else:
            kf = KFold(n_splits=folds, shuffle=True, random_state=4242)
            splits = kf.split(x_train)
    else:
        # Use provided-folds as is
        splits = folds#.split(x_train)

    p_train = np.zeros_like(y_train, dtype=np.float32)
    ps_test = []
    models = []
    scores = []
    imps = defaultdict(int)

    fold_i = 0
    for train_kf, valid_kf in splits:
        print('[mlcrate] Running fold {}, {} train samples, {} validation samples'.format(fold_i, len(train_kf), len(valid_kf)))
        d_train = xgb.DMatrix(x_train[train_kf], label=y_train[train_kf])
        d_valid = xgb.DMatrix(x_train[valid_kf], label=y_train[valid_kf])

        # Start a timer for the fold
        t.add('fold{}'.format(fold_i))

        # Metrics to print
        watchlist = [(d_train, 'train'), (d_valid, 'valid')]

        mdl = xgb.train(params, d_train, params.get('nrounds', 100000), watchlist,
                        early_stopping_rounds=params.get('early_stopping_rounds', 50), verbose_eval=params.get('verbose_eval', 1))

        scores.append(mdl.best_score)

        print('[mlcrate] Finished training fold {} - took {} - running score {}'.format(fold_i, t.fsince('fold{}'.format(fold_i)), np.mean(scores)))

        # Get importances for this model and add to global importance
        imp = get_importances(mdl, columns)
        if print_imp == 'every':
            print('Fold {} importances:'.format(fold_i), imp)

        for f, i in imp:
            imps[f] += i

        # Get predictions from the model
        p_valid = mdl.predict(d_valid, ntree_limit=mdl.best_ntree_limit)
        if x_test is not None:
            p_test = mdl.predict(d_test, ntree_limit=mdl.best_ntree_limit)

        p_train[valid_kf] = p_valid

        if x_test is not None: # MODIFIED
            ps_test.append(p_test)
        models.append(mdl)

        fold_i += 1

    if x_test is not None:
        p_test = np.mean(ps_test, axis=0)

    print('[mlcrate] Finished training {} XGBoost models, took {}'.format(folds, t.fsince(0)))

    if print_imp in ['every', 'final']:
        print('[mlcrate] Overall feature importances:', sorted(imps.items(), key=lambda x: x[1], reverse=True))

    if x_test is None:
        p_test = None

    del x_train, d_train, d_valid, x_test, d_test
    gc.collect()

    return models, p_train, p_test, imps, scores

assert len(sys.argv) == 2

target_cls = int(sys.argv[1])
print('training class', target_cls)

x_all, y_all, grps = mlc.load('./xgb-training/cls_{}.pkl'.format(target_cls))

print('loaded {} training examples'.format(len(x_all)))

params = {}
params['objective'] = 'binary:logistic'
params['tree_method'] = 'hist'
params['eta'] = 0.02
params['eval_metric'] = ['logloss']
params['max_depth'] = 4
params['colsample_bylevel'] = 0.8
params['subsample'] = 0.9
params['nthread'] = 2
# params['nrounds'] = 5
params['verbose_eval'] = 50

from sklearn import model_selection

kf = model_selection.GroupShuffleSplit(5, test_size=0.3, random_state=3000)
models, _, _, _, scores = train_kfold(params, x_all, y_all, folds=kf.split(x_all, y_all, grps), print_imp='final')
print('SCORES: {} MEAN: {}'.format(scores, np.mean(scores)))
open('xgb4_scores.csv', 'a').write('{},{}\n'.format(target_cls, np.mean(scores)))

mean = np.mean(scores)

# print('Saving models')
mlc.save([target_cls, models], 'xgb-models/xgb4_{}_{:.5f}.pkl'.format(target_cls, mean))

# print('Done', target_cls)
