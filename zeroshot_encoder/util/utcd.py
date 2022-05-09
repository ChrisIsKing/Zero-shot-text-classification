import os
import json
import math
import pickle
from os.path import join as os_join
from typing import List, Tuple, Dict, Iterable, Callable, Any, Union
from zipfile import ZipFile
from statistics import harmonic_mean
from collections import Counter, namedtuple, defaultdict

import numpy as np
import pandas as pd
import torch
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import TSNE
from tsnecuda import TSNE as cuTSNE
from datasets import Value, Features, ClassLabel, Sequence, Dataset, DatasetDict
from sentence_transformers import SentenceTransformer
import spacy
import matplotlib.pyplot as plt
from matplotlib import transforms
from matplotlib.patches import Ellipse
from matplotlib.colors import to_rgba
from matplotlib.patheffects import withStroke
from adjustText import adjust_text
import seaborn as sns
from tqdm.auto import tqdm
import gdown

from stefutil import *
from zeroshot_encoder.util.util import *
from zeroshot_encoder.util.data_path import BASE_PATH, PROJ_DIR, DSET_DIR


def get_output_base():
    # For remote machines, save heavy-duty data somewhere else to save `/home` disk space
    hnm = get_hostname()
    if 'clarity' in hnm:  # Clarity lab
        return '/data'
    elif 'arc-ts' in hnm:  # Great Lakes; `profmars0` picked arbitrarily among [`profmars0`, `profmars1`]
        # Per https://arc.umich.edu/greatlakes/user-guide/
        return os_join('/scratch', 'profmars_root', 'profmars0', 'stefanhg')
    else:
        return BASE_PATH


def get_utcd_from_gdrive(domain: str = 'in'):
    ca(domain=domain)
    path = os_join(BASE_PATH, PROJ_DIR, DSET_DIR, 'UTCD')
    os.makedirs(path, exist_ok=True)
    if domain == 'in':
        url = 'https://drive.google.com/uc?id=1V7IzdZ9HQbFUQz9NzBDjmqYBdPd9Yfe3'
        fnm = os_join(path, 'in-domain')
    else:
        url = 'https://drive.google.com/uc?id=1nd32_UrFbgoCgH4bDtFFD_YFZhzcts3x'
        fnm = os_join(path, 'out-of-domain')
    fnm = f'{fnm}.zip'
    gdown.download(url=url, output=fnm, quiet=False)
    with ZipFile(fnm, 'r') as zip_:
        zip_.extractall(path)
        zip_.close()


def process_utcd_dataset(domain: str = 'in', join=False):
    """
    :param domain: One of [`in`, `out`]
        If 'in', process all the in-domain datasets; otherwise, process all the out-of-domain datasets
    :param join: If true, all datasets are joined to a single dataset

    .. note::
        1. The original dataset format is dictionary mapping text to list of label
        2. the datasets are processed to a multi-label format always

    Save processed datasets to disk
    """
    logger = get_logger('Process UTCD')
    ca(domain=domain)
    output_dir = 'UTCD-in' if domain == 'in' else 'UTCD-out'
    ext = sconfig('UTCD.dataset_ext')
    path_dsets = os_join(BASE_PATH, PROJ_DIR, DSET_DIR)
    path_out = os_join(get_output_base(), PROJ_DIR, DSET_DIR, 'processed')
    logger.info(f'Processing UTCD datasets with {log_dict(dict(domain=domain, join=join))}... ')

    def path2dsets(dnm: str, d_dset: Dict) -> Union[DatasetDict, Dict[str, pd.DataFrame]]:
        logger.info(f'Processing dataset {logi(dnm)}... ')
        path = d_dset['path']
        path = os_join(path_dsets, f'{path}.{ext}')
        with open(path) as f:
            dsets_: Dict = json.load(f)

        def json2dset(split: str, dset: Dict[str, List[str]]) -> Union[Dataset, pd.DataFrame]:
            assert split in ['train', 'test']
            if join:  # will convert to global integers later, see below
                return pd.DataFrame([dict(text=txt, labels=lbs) for txt, lbs in dset.items()])
            else:  # TODO: didn't test
                lbs_: List[str] = sconfig(f'UTCD.datasets.{dnm}.splits.{split}.labels')
                # Map to **local** integer labels; index is label per `lbs_` ordering, same with `datasets.ClassLabel`
                lb2id = {lb: i for i, lb in enumerate(lbs_)}
                # if not multi-label, `Sequence` of single element
                df = pd.DataFrame([dict(text=txt, labels=[lb2id[lb] for lb in lbs]) for txt, lbs in dset.items()])
                length = -1 if sconfig(f'UTCD.datasets.{dnm}.splits.{split}.multi_label') else 1
                lbs = Sequence(feature=ClassLabel(names=lbs_), length=length)
                feats = Features(text=Value(dtype='string'), labels=lbs)
                return Dataset.from_pandas(df, features=feats)
        return DatasetDict(
            {key: json2dset(key, dset) for key, dset in dsets_.items() if key not in ['labels', 'aspect']}
        )
    d_dsets = {
        dnm: path2dsets(dnm, d) for dnm, d in sconfig('UTCD.datasets').items() if d['domain'] == domain
    }
    if join:
        dnm2id = sconfig('UTCD.dataset_name2id')
        # Global label across all datasets, all splits
        # Needed for inversely mapping to local label regardless of joined split, e.g. train/test,
        #   in case some label only in certain split
        lbs_global = [
            sconfig(f'UTCD.datasets.{dnm}.splits.{split}.labels')
            for dnm in d_dsets.keys() for split in ['train', 'test']
        ]
        lbs_global = sorted(set().union(*lbs_global))
        lb2id_global = {lb: i for i, lb in enumerate(lbs_global)}
        # cos definitely multi-label
        lbs_global = Sequence(feature=ClassLabel(names=lbs_global), length=-1)

        def map_labels(lbs: List[str]) -> List[int]:
            return [lb2id_global[lb] for lb in lbs]

        def prep_single(dnm: str, df_: pd.DataFrame) -> pd.DataFrame:
            df_['dataset_id'] = [dnm2id[dnm]] * len(df_)  # Add dataset source information to each row
            df_.labels = df_.labels.apply(map_labels)
            return df_

        def dfs2dset(dfs: Iterable[pd.DataFrame]) -> Dataset:
            df = pd.concat(dfs)
            # The string labels **may overlap** across the datasets
            # Keep internal feature label ordering same as dataset id
            lbs_dset = sorted(dnm2id, key=dnm2id.get)
            features = Features(text=Value(dtype='string'), labels=lbs_global, dataset_id=ClassLabel(names=lbs_dset))
            return Dataset.from_pandas(df, features=features)
        tr = dfs2dset([prep_single(dnm, dsets['train']) for dnm, dsets in d_dsets.items()])
        vl = dfs2dset([prep_single(dnm, dsets['test']) for dnm, dsets in d_dsets.items()])
        dsets = DatasetDict(train=tr, test=vl)
        dsets.save_to_disk(os_join(path_out, output_dir))
    else:
        for dnm, dsets in d_dsets.items():
            dsets.save_to_disk(os_join(path_out, dnm))
    logger.info(f'Dataset(s) saved to {logi(path_out)}')


def map_ag_news():
    dnm = 'ag_news'
    d_dset = sconfig(f'UTCD.datasets.{dnm}')
    ext = sconfig('UTCD.dataset_ext')
    path_dset = os_join(BASE_PATH, PROJ_DIR, DSET_DIR)
    path = d_dset['path']
    path = os_join(path_dset, f'{path}.{ext}')
    with open(path) as f:
        dsets: Dict = json.load(f)
    d_lb2desc = sconfig(f'baselines.gpt2-nvidia.label-descriptors.{dnm}')
    for split, dset in dsets.items():
        dsets[split] = [[txt, d_lb2desc[lb]] for txt, lb in dset]
    with open(os_join(path_dset, f'{dnm}.json'), 'w') as f:
        json.dump(dsets, f, indent=4)


def get_utcd_info() -> pd.DataFrame:
    """
    Metadata about each dataset in UTCD
    """
    k_avg_tok = [f'{mode}-{text_type}_avg_tokens' for text_type in ['txt', 'lb'] for mode in ['re', 'bert', 'gpt2']]
    infos = [
        dict(dataset_name=dnm, aspect=d_dset['aspect'], domain=d_dset['domain'])
        | {f'{split}-{k}': v for split, d_info in d_dset['splits'].items() for k, v in d_info.items()}
        | {k: d_dset[k] for k in k_avg_tok}
        for dnm, d_dset in sconfig('UTCD.datasets').items()
    ]
    return pd.DataFrame(infos)


UtcdDatasetNames = namedtuple('UtcdDatasetNames', ['in_domain', 'out_of_domain'])


def _get_utcd_dnms() -> UtcdDatasetNames:
    return UtcdDatasetNames(
        [dnm for dnm, d in sconfig('UTCD.datasets').items() if d['domain'] == 'in'],
        [dnm for dnm, d in sconfig('UTCD.datasets').items() if d['domain'] == 'out']
    )


class VisualizeOverlap:
    path_dset = os_join(BASE_PATH, PROJ_DIR, DSET_DIR)
    in_dnms, out_dnms = _get_utcd_dnms()
    # for in-domain, the training split, for out-of-domain, the test split
    dnm2n_txt = {dnm: sconfig(f'UTCD.datasets.{dnm}.splits.train.n_text') for dnm in in_dnms}
    dnm2n_txt.update({dnm: sconfig(f'UTCD.datasets.{dnm}.splits.test.n_text') for dnm in out_dnms})

    def __init__(self):
        pass

    @staticmethod
    def dnm2samples_n_total(dnm: str, kind: str, split: str) -> Tuple[Union[Iterable[str], List[str]], int]:
        if kind == 'label':
            it = sconfig(f'UTCD.datasets.{dnm}.splits.{split}.labels')
            return it, len(it)
        else:  # text
            d = sconfig(f'UTCD.datasets.{dnm}')
            path = os_join(VisualizeOverlap.path_dset, f'{d["path"]}.json')
            with open(path) as fl:
                return json.load(fl)[split].keys(), VisualizeOverlap.dnm2n_txt[dnm]

    @staticmethod
    def get_utcd_overlap(
        kind: str = 'label', metric: str = 'harmonic', stat='tfidf', stat_args: Dict = None
    ) -> pd.DataFrame:
        """
        A normalized score for overlap, between each out-of-domain dataset,
            with each in-domain datasets and aggregated across all in-domain datasets

        Intended to get a sense of performance over overlap
        """
        ca.check_mismatch('Sample Type', kind, ['label', 'text'])
        ca.check_mismatch('Overlap Metric', metric, ['harmonic', 'absolute'])
        ca.check_mismatch('Word Statistics', stat, ['count', 'tfidf'])
        logger = get_logger('Get UTCD Overlap')
        logger.info(f'Getting UTCD Overlap for {log_dict(kind=kind, metric=metric, stat=stat, stat_args=stat_args)}')
        if stat == 'tfidf':
            def tokenize(pbar) -> Callable:
                def _tokenize(txt: str) -> List[str]:
                    lst = [tok.lemma_ for tok in nlp(txt) if not tok.is_stop]
                    pbar.update(1)
                    return lst
                return _tokenize
            # TODO: tweak?
            stat_args: Dict[str, Any] = stat_args if stat_args is not None else dict(max_df=0.8, min_df=3)
            assert 'token_pattern' not in stat_args and 'tokenizer' not in stat_args
            stat_args['token_pattern'] = None
        elif stat_args is not None:
            raise NotImplementedError(f'{logi("stat_args")} supported for {logi("tfidf")} only')

        nlp = spacy.load('en_core_web_sm')
        nlp.max_length *= 10  # for `multi_eurlex`

        def _dnm2lemma_count(dnm_: str, split: str) -> Union[Counter, TfidfVectorizer]:
            it, total = VisualizeOverlap.dnm2samples_n_total(dnm_, kind, split)
            in_domain = dnm_ in VisualizeOverlap.in_dnms
            domain_str = 'in-domain' if in_domain else 'out-of-domain'
            split = 'train' if in_domain else 'test'
            pbar_args = dict(desc=f'Lemmatizing {domain_str} {dnm_} {split}', unit='sample', total=total, miniters=64)
            if stat == 'count':
                c = Counter()
                for s in tqdm(it, **pbar_args):
                    # TODO: 1) `&` isn't a stop word? 2) lowercase everything? 3) remove characters?
                    c.update(tok.lemma_ for tok in nlp(s) if not tok.is_stop)
                return c
            else:  # tfidf
                pbar = tqdm(**pbar_args)
                stat_args['tokenizer'] = tokenize(pbar)
                ic(stat_args)
                v = TfidfVectorizer(**stat_args)
                v.fit(it)
                pbar.close()
                return v
        dnm2lemma_count = dict()
        in_dnms, out_dnms = VisualizeOverlap.in_dnms, VisualizeOverlap.out_dnms
        for dnm in in_dnms:
            dnm2lemma_count[dnm] = _dnm2lemma_count(dnm, 'train')
            logger.info(f'Lemmatizing {logi("in-domain")} dataset {logi(dnm)}, {logi("train")} split')
        for dnm in out_dnms:
            dnm2lemma_count[dnm] = _dnm2lemma_count(dnm, 'test')
            logger.info(f'Lemmatizing {logi("out-of-domain")} dataset {logi(dnm)}, {logi("test")} split')
        lst_rows = []
        # See below, weighted by #samples for each in-domain dataset; TODO: weight also by label support?
        in_dnm2n_pr = {dnm: sconfig(f'UTCD.datasets.{dnm}.splits.train.n_pair') for dnm in in_dnms}
        for dnm_out in out_dnms:
            d_row = dict()
            for dnm_in in in_dnms:
                if stat == 'count':
                    c_in: Counter = dnm2lemma_count[dnm_in]
                    c_out: Counter = dnm2lemma_count[dnm_out]
                    inter = set(c_in) & set(c_out)
                    n_inter_in, n_in = sum(c_in[i] for i in inter), sum(c_in.values())
                    n_inter_out, n_out = sum(c_out[i] for i in inter), sum(c_out.values())
                else:  # tfidf
                    v_in: TfidfVectorizer = dnm2lemma_count[dnm_in]
                    v_out: TfidfVectorizer = dnm2lemma_count[dnm_out]
                    inter = set(v_in.get_feature_names_out()) & set(v_out.get_feature_names_out())
                    idxs_in, idxs_out = [v_in.vocabulary_[i] for i in inter], [v_out.vocabulary_[i] for i in inter]
                    n_inter_in, n_in = v_in.idf_[idxs_in].sum(), v_in.idf_.sum()
                    n_inter_out, n_out = v_out.idf_[idxs_out].sum(), v_out.idf_.sum()
                # Considers the count for both datasets; also ensure in range [0, 1]
                if metric == 'harmonic':
                    d_row[dnm_in] = harmonic_mean([n_inter_in / n_in, n_inter_out / n_out])
                else:
                    assert metric == 'absolute'
                    d_row[dnm_in] = (n_inter_in + n_inter_out) / (n_in + n_out)
            dnms, vals = zip(*d_row.items())
            d_row['average'] = np.mean(vals)
            d_row['weighted_average'] = np.average(vals, weights=[in_dnm2n_pr[dnm] for dnm in dnms])
            d_row['dataset_name'] = dnm_out
            lst_rows.append(d_row)
        return pd.DataFrame(lst_rows).set_index('dataset_name')

    @staticmethod
    def plot_utcd_overlap(kind: str = 'label', save: bool = False, **kwargs) -> None:
        d_dset = sconfig('UTCD.datasets')

        def dnm2dnm_print(dnm: str) -> str:
            if dnm in d_dset:
                return dnm.replace('_', '\n')
            else:
                words = dnm.split('_')
                return '\n'.join(rf'$\it{{{wd}}}$' for wd in words)
        df = VisualizeOverlap.get_utcd_overlap(kind=kind, **kwargs)
        df *= 100
        df.rename(lambda s: dnm2dnm_print(s), axis=1, inplace=True)
        df.rename(lambda s: dnm2dnm_print(s), axis=0, inplace=True)
        fig, (ax, ax_cbar) = plt.subplots(1, 2, figsize=(10+0.25, 8), gridspec_kw=dict(width_ratios=[10, 0.25]))
        sns.heatmap(df, annot=True, cmap='mako', fmt='.1f', square=True, ax=ax, cbar_ax=ax_cbar)
        ax.xaxis.set_ticks_position('top')
        ax.xaxis.set_label_position('top')
        ax.tick_params(axis='y', labelrotation=0)
        title = f'Out-of-domain eval datasets {kind.capitalize()} overlap against In-domain training datasets'
        plt.suptitle(title)
        ax.set_xlabel('In-domain dataset')
        ax.set_ylabel('Out-of-domain dataset')
        ax_cbar.set_ylabel('Overlap Score (%)')
        if save:
            mt_, st_ = kwargs.get('metric', 'harmonic'), kwargs.get('stat', 'tfidf')  # see `get_utcd_overlap`
            mt_ = 'harm' if mt_ == 'harmonic' else 'abs'
            st_ = 'ti' if st_ == 'tfidf' else 'ct'
            save_fig(f'{title}, mt={mt_}, st={st_}')
        else:
            plt.show()

    @staticmethod
    def get_utcd_embeddings(
            kind: str = 'label', aspect: str = None, batch_size: int = 16, cache: str = None
    ) -> Dict[str, np.ndarray]:
        """
        Plot sample embeddings in lower dimension
        and hopefully the overlap between each dataset cluster lines up with performance
        """
        def _get():
            return VisualizeOverlap._get_utcd_embeddings(kind=kind, aspect=aspect, batch_size=batch_size)
        if cache:
            fnm = f'{cache}.pkl'
            path = os_join(BASE_PATH, PROJ_DIR, 'cache')
            os.makedirs(path, exist_ok=True)
            path = os_join(path, fnm)

            if os.path.exists(path):
                with open(path, 'rb') as f:
                    return pickle.load(f)
            else:
                d = _get()
                with open(path, 'wb') as f:
                    pickle.dump(d, f)
                return d
        else:
            return _get()

    @staticmethod
    def _get_utcd_embeddings(kind, aspect, batch_size):
        # per SBert package, the one with the highest quality
        model = SentenceTransformer('all-mpnet-base-v2', device='cuda' if torch.cuda.is_available() else 'cpu')
        in_dnms, out_dnms = VisualizeOverlap.in_dnms, VisualizeOverlap.out_dnms
        ret = dict()
        dnms = in_dnms + out_dnms
        if aspect is not None:
            dnms = [dnm for dnm in dnms if sconfig(f'UTCD.datasets.{dnm}.aspect') == aspect]
        for dnm in dnms:
            split = 'train' if dnm in in_dnms else 'test'
            it, total = VisualizeOverlap.dnm2samples_n_total(dnm, kind, split)
            total = math.ceil(total/batch_size)
            desc = f'Encoding {dnm:>21} {kind:>5} {split:>5}'
            vects = np.empty(total, dtype=object)
            for i, sents in enumerate(tqdm(group_n(it, batch_size), total=total, desc=desc, unit='ba')):
                vects[i] = model.encode(sents, batch_size=batch_size)
            ret[dnm] = np.concatenate(vects)
        return ret

    @staticmethod
    def plot_utcd_embeddings(
            kind: str = 'label', save=False, aspect: str = None, cs: List = None, mode: str = 'sklearn',
            n_sample: int = None,
            **kwargs
    ):
        """
        :param kind: Encode either text or label
        :param save: If true, plot is saved
        :param aspect: If given, plot only one aspect
        :param cs: A list of colors for each cluster
        :param mode: t-SNE mode, one of ['sklearn', 'cuda']
        :param n_sample: If given, plot a subset of each dataset randomly
        :param n_sample: If given, plot a subset of each dataset randomly
        """
        ca.check_mismatch('Sample Type', kind, ['label', 'text'])
        ca.check_mismatch('t-SNE Mode', mode, ['sklearn', 'cuda'])
        if aspect is not None:
            ca.check_mismatch('Dataset Aspect', aspect, ['sentiment', 'intent', 'topic'])
        logger = get_logger('UTCD Embedding Plot')
        d_log = dict(kind=kind, aspect=aspect, mode=mode)
        logger.info(f'Plotting embeddings on {log_dict(d_log)}... ')
        d_vect = VisualizeOverlap.get_utcd_embeddings(kind=kind, aspect=aspect, **kwargs)
        if n_sample:
            def _get_sample(dnm):
                idxs = np.random.permutation(len(d_vect[dnm]))[:n_sample]
                return d_vect[dnm][idxs]
            d_vect = {dnm: _get_sample(dnm) for dnm in d_vect}
        dnms = VisualizeOverlap.in_dnms + VisualizeOverlap.out_dnms
        if aspect is not None:
            dnms = [dnm for dnm in dnms if sconfig(f'UTCD.datasets.{dnm}.aspect') == aspect]
        vect = np.concatenate([d_vect[dnm] for dnm in dnms])
        # TODO or `random` init?
        args = dict(
            n_components=2, perplexity=50,
            # learning_rate='auto',  # TODO: causes numpy error???
            learning_rate=1000,
            random_state=sconfig('random-seed')
        )
        if mode == 'sklearn':
            cls = TSNE
            args['init'] = 'pca'
        else:
            cls = cuTSNE
            args['init'] = 'random'
            del args['random_state']

        logger.info(f'Running t-SNE on {logi(len(vect))} vectors with args {log_dict(args)}... ')
        mapped = cls(**args).fit_transform(vect)

        logger.info('Plotting... ')
        k_dnm = 'dataset_name'
        df = pd.DataFrame(chain_its([dnm] * len(d_vect[dnm]) for dnm in dnms), columns=[k_dnm])
        df['x'] = mapped[:, 0]
        df['y'] = mapped[:, 1]
        aspect2domain2dset = defaultdict(lambda: defaultdict(list))
        for dnm, d_dset in sconfig('UTCD.datasets').items():
            aspect2domain2dset[d_dset['aspect']][d_dset['domain']].append(dnm)
        if not cs:
            n_gap = 6
            n_aspect, n_dset_per_aspect = sconfig('UTCD.num_aspect'), sconfig('UTCD.num_dataset_per_aspect')
            if aspect is not None:
                n_aspect = 1
            cs = sns.color_palette('husl', n_colors=n_aspect * (n_dset_per_aspect+n_gap))
            cs = cs[:n_dset_per_aspect] + cs[n_dset_per_aspect+n_gap:n_dset_per_aspect*2+n_gap] + \
                cs[n_dset_per_aspect*2+n_gap*2:-n_gap]
        dnms = []  # update order for color-coding
        for i_as, aspect_ in enumerate(aspect2domain2dset.keys()):
            if aspect is not None and aspect_ != aspect:
                continue
            for i_dm, (domain, dnms_) in enumerate(aspect2domain2dset[aspect_].items()):
                for i_dset, dnm in enumerate(dnms_):
                    dnms.append(dnm)
        df_col2cat_col(df, k_dnm, categories=dnms)  # enforce legend order
        dnm2count = {k: len(v) for k, v in d_vect.items()}
        n_sample = sum(dnm2count.values())  # now, all datasets combined
        fig_w, fig_h = 10, 12
        ms = max(min(fig_w * fig_h * 128/n_sample, 192), 16)
        dnm2ms = {dnm: 1/math.log(c) * ms for dnm, c in dnm2count.items()}

        fig = plt.figure(figsize=(fig_w, fig_h), constrained_layout=False)
        ax = sns.scatterplot(data=df, x='x', y='y', hue=k_dnm, palette=cs, size=k_dnm, sizes=dnm2ms, alpha=0.3)

        def confidence_ellipse(xs_, ys_, n_std=1., **kws):
            """
            Modified from https://matplotlib.org/stable/gallery/statistics/confidence_ellipse.html
            Create a plot of the covariance confidence ellipse of x and y

            :param xs_: x values
            :param ys_: y values
            :param n_std: number of standard deviations to determine the ellipse's radius'
            :return matplotlib.patches.Ellipse
            """
            cov = np.cov(xs_, ys_)
            pearson = cov[0, 1] / np.sqrt(cov[0, 0] * cov[1, 1])
            r_x, r_y = np.sqrt(1 + pearson), np.sqrt(1 - pearson)
            _args = {**dict(fc='none'), **kws}
            ellipse = Ellipse((0, 0), width=r_x*2, height=r_y*2, **_args)
            scl_x, scl_y = np.sqrt(cov[0, 0]) * n_std, np.sqrt(cov[1, 1]) * n_std
            mu_x, mu_y = np.mean(xs_), np.mean(ys_)
            tsf = transforms.Affine2D().rotate_deg(45).scale(scl_x, scl_y).translate(mu_x, mu_y)
            ellipse.set_transform(tsf + ax.transData)
            return ax.add_patch(ellipse)
        txt_locs, dnm2pa = [], dict()
        for dnm, c in zip(dnms, cs):
            xs, ys = df[df[k_dnm] == dnm]['x'].values, df[df[k_dnm] == dnm]['y'].values
            dnm2pa[dnm] = confidence_ellipse(xs, ys, n_std=1, fc=to_rgba(c, 0.1), ec=to_rgba(c, 0.6))

        inv_tsf = ax.transData.inverted()
        txts = []
        for dnm, c in zip(dnms, cs):
            xs, ys = df[df[k_dnm] == dnm]['x'].values, df[df[k_dnm] == dnm]['y'].values
            pa = dnm2pa[dnm]

            verts = pa.get_transform().transform_path(pa.get_path()).vertices
            verts = inv_tsf.transform(verts)  # this is needed to get the vertices properly

            def close_to_added(x_, y_, threshold=1):
                for x__, y__ in txt_locs:
                    if np.sqrt((x_ - x__) ** 2 + (y_ - y__) ** 2) < threshold:
                        return True
                return False

            def in_other_ellipse(x_, y_):
                other_dnms = [dnm_ for dnm_ in dnms if dnm_ != dnm]
                for dnm_ in other_dnms:
                    pa_ = dnm2pa[dnm_]
                    path = pa_.get_transform().transform_path(pa_.get_path())
                    if inv_tsf.transform_path(path).contains_point((x_, y_)):
                        return True
                return False
            x, y, coord_found = None, None, False  # find a working coordinate to add the text
            verts = np.random.permutation(verts)
            for x, y in verts:
                if not close_to_added(x, y, threshold=3) and not in_other_ellipse(x, y):
                    coord_found = True
                    break
            if not coord_found:
                verts = np.random.permutation(verts)
                for x, y in verts:
                    if not close_to_added(x, y):
                        coord_found = True
                        break
            if not coord_found:
                x, y = np.mean(xs), np.mean(ys)
            txt_locs.append((x, y))
            txts.append(plt.text(x=x, y=y, s=dnm.replace('_', ' '), c=c, ha='center', va='center'))
        adjust_text(txts)
        for txt in txts:  # add border-color
            txt.set_path_effects([withStroke(linewidth=1, foreground='w')])

        def map_label(dnm: str) -> str:
            _d_dset = sconfig(f'UTCD.datasets.{dnm}')
            dm = _d_dset['domain']
            asp = _d_dset['aspect']
            dnm = dnm.replace('_', ' ')
            dm = rf'$\it{{{dm}}}$'
            asp = rf'$\it{{{asp}}}$'
            return f'{asp}::{dm}::{dnm}'
        ax.set_aspect('equal')
        ax.set_xlabel(None)
        ax.set_ylabel(None)
        title = f'UTCD dataset Embedded {kind.capitalize()} t-SNE scatter plot'
        if aspect:
            title = f'{title} on {aspect.capitalize()}'
        plt.suptitle(title)

        l = ax.get_legend()  # need to have the seaborn legend added first
        l.remove()
        l = fig.legend(title=k_dnm.replace('_', ' '), loc='lower center', bbox_transform=fig.transFigure, ncol=3)
        for t in l.get_texts():
            t.set_text(map_label(t.get_text()))
        legend_v_ratio = 0.15
        plt.subplots_adjust(bottom=legend_v_ratio)
        plt.tight_layout(rect=[0, legend_v_ratio, 1, 1])
        if save:
            title = f'{title}, md={mode}'
            if n_sample:
                title = f'{title}, n={n_sample}'
            save_fig(title)
        else:
            plt.show()


if __name__ == '__main__':
    from icecream import ic

    from datasets import load_from_disk

    ic.lineWrapWidth = 512
    np.random.seed(sconfig('random-seed'))

    def sanity_check(dsets_nm):
        path = os_join(get_output_base(), PROJ_DIR, DSET_DIR, 'processed', dsets_nm)
        ic(path)
        dset = load_from_disk(path)
        te, vl = dset['train'], dset['test']
        ic(len(te), len(vl))
        lbs = vl.features['labels'].feature
        ic(lbs)
        ic(vl[60])
        ic(lbs.int2str(154))
    # sanity_check('UTCD-in')

    def get_utcd_in():
        process_utcd_dataset(domain='in', join=False)
        sanity_check('UTCD-in')
    # get_utcd_in()

    # get_utcd_from_gdrive(domain='out')

    def get_utcd_out():
        process_utcd_dataset(domain='out', join=False)
        sanity_check('UTCD-out')
    # get_utcd_out()

    # process_utcd_dataset(in_domain=True, join=False)
    # process_utcd_dataset(in_domain=False, join=False)

    def sanity_check_ln_eurlex():
        path = os_join(get_output_base(), PROJ_DIR, DSET_DIR, 'processed', 'multi_eurlex')
        ic(path)
        dset = load_from_disk(path)
        ic(dset, len(dset))
    # sanity_check_ln_eurlex()
    # ic(lst2uniq_ids([5, 6, 7, 6, 5, 1]))

    def output_utcd_info():
        df = get_utcd_info()
        ic(df)
        df.to_csv(os_join(BASE_PATH, PROJ_DIR, DSET_DIR, 'utcd-info.csv'), float_format='%.3f')
    # output_utcd_info()

    def fix_amazon_polarity():
        """
        One test sample has 2 labels, remove it
        """
        from tqdm import tqdm
        wicked_lb = {'positive', 'negative'}
        path = os_join(BASE_PATH, PROJ_DIR, DSET_DIR, 'UTCD', 'out-of-domain', 'amazon_polarity.json')
        with open(path, 'r') as f:
            dset = json.load(f)
        wicked_txts = []
        for k, v in tqdm(dset['test'].items()):
            if len(v) > 1:
                assert set(v) == wicked_lb
                wicked_txts.append(k)
        assert len(wicked_txts) == 1
        wicked_txt = wicked_txts[0]
        ic(wicked_txt)
        # assert wicked_txt in dset['test'] and wicked_lb == set(dset['test'][wicked_txt])
        dset['test'][wicked_txt] = ['positive']
        with open(path, 'w') as f:
            json.dump(dset, f)
    # fix_amazon_polarity()

    def chore_check_multi_label():
        """
        Some datasets have only a tiny fraction of multi-label samples in the training split,
            which might not be intended after processing
        """
        dnms = ['sentiment_tweets_2020', 'slurp', 'patent']
        path_dset = os_join(BASE_PATH, PROJ_DIR, DSET_DIR)
        for dnm in dnms:
            d = sconfig(f'UTCD.datasets.{dnm}')
            path = os_join(path_dset, f'{d["path"]}.json')
            with open(path) as fl:
                dsets: Dict = json.load(fl)['train']
            for text, labels in dsets.items():
                if len(labels) > 1:
                    d = dict(dset=dnm, labels=labels, text=text)
                    print(log_dict(d))
    # chore_check_multi_label()

    vs = VisualizeOverlap()

    def plot_token_overlap():
        # ic(get_utcd_overlap())
        # kd = 'label'
        kd = 'text'
        # st = 'count'
        st = 'tfidf'
        if kd == 'label':
            args = dict()
        else:
            # args = None
            args = dict()
        # sv = False
        sv = True
        vs.plot_utcd_overlap(kind=kd, save=sv, stat=st, stat_args=args)
        # vs.profile_runtime(lambda: get_utcd_overlap(kind=kd))
    # plot_token_overlap()

    def plot_encoded_overlap():
        kd = 'text'
        # kd = 'label'
        # ic(vs.get_utcd_embeddings(kind=kd))
        # sv = False
        sv = True
        cnm = f'{kd} embedding cache'
        # cs = None
        # cs = sns.color_palette('husl', n_colors=18)
        # cs = sns.color_palette('hls', n_colors=18)
        cs = sns.color_palette(n_colors=18)
        md = 'cuda'
        # TODO: running on all data & some # subset of data gives CUDA error???
        # n = None
        n = 3072 * 32
        vs.plot_utcd_embeddings(kind=kd, cs=cs, save=sv, cache=cnm, batch_size=1024, mode=md, n_sample=n)
    # plot_encoded_overlap()

    def plot_encoded_overlap_aspect():
        kd = 'label'
        # sv = False
        sv = True
        cnm = f'{kd} embedding cache'
        # aspect = None
        # aspect = 'topic'
        # aspect = 'intent'
        # aspect = 'sentiment'
        for aspect in sconfig('UTCD.aspects'):
            vs.plot_utcd_embeddings(kind=kd, aspect=aspect, save=sv, cache=cnm)
    # plot_encoded_overlap_aspect()
