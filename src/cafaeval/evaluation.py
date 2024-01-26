import os
import numpy as np
import pandas as pd
import multiprocessing as mp
from cafaeval.parser import obo_parser, gt_parser, pred_parser
import logging
logging.getLogger(__name__).addHandler(logging.NullHandler())


# Return a mask for all the predictions (matrix) >= tau
def solidify_prediction(pred, tau):
    return pred >= tau


# computes the f metric for each precision and recall in the input arrays
def compute_f(pr, rc):
    n = 2 * pr * rc
    d = pr + rc
    return np.divide(n, d, out=np.zeros_like(n, dtype=float), where=d != 0)


def compute_s(ru, mi):
    return np.sqrt(ru**2 + mi**2)
    # return np.where(np.isnan(ru), mi, np.sqrt(ru + np.nan_to_num(mi)))


def compute_metrics_(tau_arr, g, pred, toi, n_gt, ic_arr):

    # cov, tp, fp, fn, pr, rc
    # fp = misinformation, fn = remaining uncertainty)
    metrics = np.zeros((len(tau_arr), 6), dtype='float')

    for i, tau in enumerate(tau_arr):

        # Filter predictions based on tau threshold
        p = solidify_prediction(pred.matrix[:, toi], tau)

        # Coverage, number of proteins with at least one term predicted with score >= tau
        metrics[i, 0] = (p.sum(axis=1) > 0).sum()

        # Terms subsets
        intersection = np.logical_and(p, g)  # TP
        mis = np.logical_and(p, np.logical_not(g))  # FP --> predicted but not in the ground truth
        remaining = np.logical_and(np.logical_not(p), g)  # FN --> not predicted but in the ground truth

        # Normal evaluation
        if ic_arr is None:
            # Positive predictions
            n_pred = p.sum(axis=1)  # TP + FP
            n_intersection = intersection.sum(axis=1)  # TP

            # TP, FP (Misinformation), FN (Remaining uncertainty)
            metrics[i, 1] = n_intersection.sum()  # TP
            metrics[i, 2] = mis.sum(axis=1).sum()  # FP
            metrics[i, 3] = remaining.sum(axis=1).sum()  # FN

        # Weighted evaluation
        else:
            # Positive predictions
            n_pred = (p * ic_arr[toi]).sum(axis=1)  # TP + FP
            n_intersection = (intersection * ic_arr[toi]).sum(axis=1)  # TP

            # TP, FP (Misinformation), FN (Remaining uncertainty)
            metrics[i, 1] = (intersection * ic_arr[toi]).sum(axis=1).sum()
            metrics[i, 2] = (mis * ic_arr[toi]).sum(axis=1).sum()
            metrics[i, 3] = (remaining * ic_arr[toi]).sum(axis=1).sum()

        # Precision, recall (micro-average)
        metrics[i, 4] = np.divide(n_intersection, n_pred, out=np.zeros_like(n_intersection, dtype='float'),
                                  where=n_pred > 0).sum()
        metrics[i, 5] = np.divide(n_intersection, n_gt, out=np.zeros_like(n_gt, dtype='float'),
                                  where=n_gt > 0).sum()

    return metrics


def compute_metrics(pred, gt, tau_arr, toi, toi_ia, ic_arr, n_cpu=0):
    """
    Takes the prediction and the ground truth and for each threshold in tau_arr
    calculates the confusion matrix and returns the coverage,
    precision, recall, remaining uncertainty and misinformation.
    Toi is the list of terms (indexes) to be considered
    """
    # Parallelization
    if n_cpu == 0:
        n_cpu = mp.cpu_count()

    # Simple metrics
    columns = ["cov", "tp", "fp", "fn", "pr", "rc"]
    g = gt.matrix[:, toi]
    n_gt = g.sum(axis=1)
    arg_lists = [[tau_arr, g, pred, toi, n_gt, None] for tau_arr in np.array_split(tau_arr, n_cpu)]
    with mp.Pool(processes=n_cpu) as pool:
        metrics = np.concatenate(pool.starmap(compute_metrics_, arg_lists), axis=0)

    # Weighted metrics
    if ic_arr is not None:
        columns.extend(["wcov", "wtp", "wfp", "wfn", "wpr", "wrc"])
        g = gt.matrix[:, toi_ia]
        n_gt = (g * ic_arr[toi_ia]).sum(axis=1)
        arg_lists = [[tau_arr, g, pred, toi_ia, n_gt, ic_arr] for tau_arr in np.array_split(tau_arr, n_cpu)]
        with mp.Pool(processes=n_cpu) as pool:
            metrics_ia = np.concatenate(pool.starmap(compute_metrics_, arg_lists), axis=0)
        metrics = np.concatenate((metrics, metrics_ia), axis=1)

    return pd.DataFrame(metrics, columns=columns)


def evaluate_prediction(prediction, gt, ontologies, tau_arr, normalization='cafa', n_cpu=0):

    dfs = []
    for ns in prediction:

        # Number of predicted proteins
        ne = np.full(len(tau_arr), gt[ns].matrix[:, ontologies[ns].toi].shape[0])

        # cov, tp, fp, fn, pr, rc (wcov, wtp, wfp, wfn, wpr, wrc)
        metrics = compute_metrics(prediction[ns], gt[ns], tau_arr, ontologies[ns].toi, ontologies[ns].toi_ia, ontologies[ns].ia, n_cpu)

        # Normalization
        for column in metrics.columns:
            if column not in ["cov", "wcov"]:

                # By default normalize by gt
                denominator = ne

                # Normalize by pred (cov)
                if normalization == 'pred' or (normalization == 'cafa' and column in ["pr" or "wpr"]):
                    if column[0] != "w":
                        denominator = metrics["cov"]
                    else:
                        # Normalize by weighted cov
                        denominator = metrics["wcov"]

                metrics[column] = np.divide(metrics[column], denominator,
                                            out=np.zeros_like(metrics[column], dtype='float'), where=denominator > 0)

        metrics['ns'] = [ns] * len(tau_arr)
        metrics['tau'] = tau_arr

        metrics['cov'] = np.divide(metrics['cov'], ne, out=np.zeros_like(metrics['cov'], dtype='float'), where=ne > 0)
        metrics['f'] = compute_f(metrics['pr'], metrics['rc'])

        if ontologies[ns].ia is not None:
            ne = np.full(len(tau_arr), gt[ns].matrix[:, ontologies[ns].toi_ia].shape[0])

            metrics['wcov'] = np.divide(metrics['wcov'], ne, out=np.zeros_like(metrics['wcov'], dtype='float'), where=ne > 0)
            metrics['wf'] = compute_f(metrics['wpr'], metrics['wrc'])
            metrics['s'] = compute_s(metrics['fn'], metrics['fp'])

        dfs.append(metrics)

    return pd.concat(dfs)


def cafa_eval(obo_file, pred_dir, gt_file, ia=None, no_orphans=False, norm='cafa', prop='max',
              max_terms=None, th_step=0.01, threads=1):

    # Tau array, used to compute metrics at different score thresholds
    tau_arr = np.arange(th_step, 1, th_step)

    # Parse the OBO file and creates a different graphs for each namespace
    ontologies = obo_parser(obo_file, ("is_a", "part_of"), ia, not no_orphans)

    # Parse ground truth file
    gt = gt_parser(gt_file, ontologies)

    # Set prediction files looking recursively in the prediction folder
    pred_folder = os.path.normpath(pred_dir) + "/"  # add the tailing "/"
    pred_files = []
    for root, dirs, files in os.walk(pred_folder):
        for file in files:
            pred_files.append(os.path.join(root, file))
    logging.debug("Prediction paths {}".format(pred_files))

    # Parse prediction files and perform evaluation
    dfs = []
    for file_name in pred_files:
        prediction = pred_parser(file_name, ontologies, gt, prop, max_terms)
        if prediction:
            df_pred = evaluate_prediction(prediction, gt, ontologies, tau_arr, norm, threads)
            df_pred['filename'] = file_name.replace(pred_folder, '').replace('/', '_')
            dfs.append(df_pred)
            logging.info("Prediction: {}, evaluated".format(file_name))
        else:
            logging.warning("Prediction: {}, not evaluated".format(file_name))

    # Concatenate all dataframes and save them
    df = None
    dfs_best = {}
    if dfs:
        df = pd.concat(dfs)

        # Remove rows with no coverage
        df = df[df['cov'] > 0].reset_index(drop=True)
        df.set_index(['filename', 'ns', 'tau'], inplace=True)

        # Select columns to save
        if ia is not None:
            columns = ["cov", "tp", "fp", "fn", "pr", "rc", "f", "wcov", "wtp", "wfp", "wfn", "wpr", "wrc", "wf", "s"]
        else:
            columns = ["cov", "tp", "fp", "fn", "pr", "rc", "f"]
        df = df[columns]

        # Calculate the best index for each namespace and each evaluation metric
        for metric, cols in [('f', ['rc', 'pr']), ('wf', ['wrc', 'wpr']), ('s', ['fn', 'fp'])]:
            if metric in columns:
                index_best = df.groupby(level=['filename', 'ns'])[metric].idxmax() if metric in ['f', 'wf'] else \
                    df.groupby(['filename', 'ns'])[metric].idxmin()
                df_best = df.loc[index_best]
                df_best['max_cov'] = df.reset_index('tau').loc[[ele[:-1] for ele in index_best]].groupby(level=['filename', 'ns'])['cov'].max()
                dfs_best[metric] = df_best[columns + ["max_cov"]]
    else:
        logging.info("No predictions evaluated")

    return df, dfs_best


def write_results(df, dfs_best, out_dir='results'):

    # Create output folder here in order to store the log file
    out_folder = os.path.normpath(out_dir) + "/"
    if not os.path.isdir(out_folder):
        os.makedirs(out_folder)

    df.to_csv('{}/evaluation_all.tsv'.format(out_folder), float_format="%.5f", sep="\t")

    for metric in dfs_best:
        dfs_best[metric].to_csv('{}/evaluation_best_{}.tsv'.format(out_folder, metric), float_format="%.5f", sep="\t")