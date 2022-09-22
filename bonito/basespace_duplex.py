import re
from itertools import takewhile
from collections import OrderedDict

import pysam
import numpy as np
from mappy import revcomp
from edlib import align as edlib_align
from parasail import dnafull, sg_trace_scan_32

from bonito.multiprocessing import ProcessMap


# Cigar int code ops are: MIDNSHP=X
CODE_TO_OP = OrderedDict(
    (
        ("M", pysam.CMATCH),
        ("I", pysam.CINS),
        ("D", pysam.CDEL),
        ("N", pysam.CREF_SKIP),
        ("S", pysam.CSOFT_CLIP),
        ("H", pysam.CHARD_CLIP),
        ("P", pysam.CPAD),
        ("=", pysam.CEQUAL),
        ("X", pysam.CDIFF),
    )
)
CIGAR_IS_QUERY = np.array(
    [True, True, False, False, True, False, False, True, True]
)
CIGAR_IS_REF = np.array(
    [True, False, True, True, False, False, False, True, True]
)


class ReadIndexedBam:
    def __init__(bam_fp, skip_non_primary=True):
        self.bam_fp = bam_fp
        self.skip_non_primary = skip_non_primary
        self.bam_fh = None
        self.bam_idx = None

    def open_bam(self):
        # hid warnings for no index when using unmapped or unsorted files
        self.pysam_save = pysam.set_verbosity(0)
        self.bam_fh = pysam.AlignmentFile(self.bam_fp, mode="rb", check_sq=False)

    def close_bam(self):
        self.bam_fh.close()
        self.bam_fh = None
        pysam.set_verbosity(self.pysam_save)

    def compute_read_index(self):
        def read_is_primary(read):
            return not (read.is_supplementary or read.is_secondary)

        self.bam_idx = {} if self.skip_non_primary else defaultdict(list)
        self.open_bam()
        pbar = tqdm(smoothing=0, unit=" Reads", desc="Indexing BAM by read id")
        # iterating over file handle gives incorrect pointers
        while True:
            read_ptr = bam_fh.tell()
            try:
                read = next(bam_fh)
            except StopIteration:
                break
            if self.skip_non_primary:
                if not read_is_primary(read) or read.query_name in self.bam_idx):
                    continue
                self.bam_idx[read.query_name] = [read_ptr]
            else:
                self.bam_idx[read.query_name].append(read_ptr)
            pbar.update()
        self.close_bam()
        if not self.skip_non_primary:
            self.bam_idx = dict(self.bam_idx)

    def get_alignments(self, read_id):
        if self.bam_idx is None:
            raise RuntimeError("Bam index not yet computed")
        if self.bam_fh is None:
            self.open_bam()
        try:
            read_ptrs = self.bam_idx[read_id]
        except KeyError:
            raise RuntimeError(f"Could not find {read_id} in {self.bam_fp}")
        for read_ptr in read_ptrs:
            self.bam_fh.seek(read_ptr)
            yield next(self.bam_fh)

    def get_first_alignment(self, read_id):
        return next(self.get_alignments(read_id))


def compute_consensus(
    cigar,
    temp_seq,
    temp_qscores,
    comp_seq,
    comp_qscores,
):
    """ Compute consensus by comparing qscores """
    def mask_expand(values, mask):
        x = np.full(len(mask), fill_value=np.uint8(ord("-")), dtype=values.dtype)
        x[mask] = values
        return x

    def as_array(seq):
        return np.frombuffer(seq.encode("ascii"), dtype=np.uint8)

    c_ops, c_counts = zip(*cigar)
    c_expanded = np.repeat(c_ops, c_counts)
    c_is_temp = np.array(CIGAR_IS_QUERY)[c_expanded]
    c_is_comp = np.array(CIGAR_IS_REF)[c_expanded]
    c_expanded_temp = mask_expand(as_array(temp_seq), c_is_temp)
    c_expanded_comp = mask_expand(as_array(comp_seq), c_is_comp)

    qs = np.stack([
        temp_qscores[np.maximum(np.cumsum(c_is_temp) - 1, 0)],
        comp_qscores[np.maximum(np.cumsum(c_is_comp) - 1, 0)]
    ])
    idx = qs.argmax(axis=0)

    consensus = np.where(idx, c_expanded_comp, c_expanded_temp)
    q = np.where(
        c_expanded_comp == c_expanded_temp,
        qs.sum(axis=0),
        qs[idx, np.arange(qs.shape[1])]
    )
    i = (consensus != ord("-"))

    cons_seq = consensus[i].tobytes().decode()
    cons_qstring = np.round(
        np.clip(q[i], 0, 60) + 33
    ).astype(np.uint8).tobytes().decode('ascii')

    return cons_seq, cons_qstring


def adj_qscores(qscores, seq, qshift, pool_window=5, avg_hps_gt=2):
    def shift(x, n=1):
        if n > 0:
            x = np.concatenate([[x[0]] * n, x[:-n]])
        elif n < 0:
            x = np.concatenate([x[-n:], [x[-1]] * (-n)])
        return x

    def min_pool(x):
        x = np.pad(x.astype(np.float32), pool_window // 2, mode='edge')
        return np.lib.stride_tricks.as_strided(
            x,
            (len(x) + 1 - pool_window, pool_window),
            strides=(x.dtype.itemsize, x.dtype.itemsize)
        ).min(1)

    def hp_spans():
        pat = re.compile(r"(.)\1{%s,}" % (avg_hps_gt - 1))
        return (m.span() for m in pat.finditer(seq))

    qscores = min_pool(shift(qscores, qshift))
    for st, en in hp_spans():
        qscores[st:en] = np.mean(qscores[st:en])
    return qscores


def cigartuples_from_string(cigarstring):
    """Returns pysam-style list of (op, count) tuples from a cigarstring."""
    pattern = re.compile(rf"(\d+)([{''.join(CODE_TO_OP.keys())}])")
    return [
        (CODE_TO_OP[m.group(2)], int(m.group(1)))
        for m in re.finditer(pattern, cigarstring)
    ]


def seq_lens(cigartuples):
    """Length of query and reference sequences from cigar tuples."""
    if not len(cigartuples):
        return 0, 0
    ops, counts = np.array(cigartuples).T
    q_len = counts[CIGAR_IS_QUERY[ops]].sum()
    r_len = counts[CIGAR_IS_REF[ops]].sum()
    return q_len, r_len


def trim_while(cigar, from_end=False):
    """Trim cigartuples until predicate is not satisfied."""

    def trim_func(c_op_len, num_match=11):
        return (c_op_len[1] < num_match) or (c_op_len[0] != pysam.CEQUAL)

    cigar_trim = (
        list(takewhile(trim_func, reversed(cigar)))[::-1]
        if from_end
        else list(takewhile(trim_func, cigar))
    )
    if len(cigar_trim):
        cigar = (
            cigar[: -len(cigar_trim)] if from_end else cigar[len(cigar_trim):]
        )
    q_trim, r_trim = seq_lens(cigar_trim)
    return cigar, q_trim, r_trim


def edlib_adj_align(query, ref, num_match=11):
    def find_first(predicate, seq):
        return next((i for i, x in enumerate(seq) if predicate(x)), None)

    def long_match(c_op_len, num_match=11):
        return (c_op_len[0] == pysam.CEQUAL) and (c_op_len[1] >= num_match)

    def concat(*cigars):
        cigars = [c for c in cigars if len(c)]
        for c1, c2 in zip(cigars[:-1], cigars[1:]):
            (o1, n1), (o2, n2) = c1[-1], c2[0]
            if o1 == o2:
                c1[-1] = (o1, 0)
                c2[0] = (o2, n1 + n2)
        return [(o, n) for c in cigars for (o, n) in c if n]

    def parasail_align(query, ref):
        return cigartuples_from_string(
            sg_trace_scan_32(query, ref, 10, 2, dnafull).cigar.decode.decode()
        )

    # compute full read cigar with edlib
    cigar = cigartuples_from_string(
        edlib_align(query, ref, task="path")["cigar"]
    )

    # find first and last long matches and fix up alignments with parasail
    flm_idx = find_first(long_match, cigar)
    if flm_idx is None:
        return parasail_align(query, ref)
    if flm_idx > 0:
        q_start, r_start = seq_lens(cigar[: flm_idx + 1])
        cigar = concat(
            parasail_align(query[:q_start], ref[:r_start]), cigar[flm_idx + 1:]
        )
    llm_idx = find_first(long_match, reversed(cigar))
    if llm_idx is None:
        return parasail_align(query, ref)
    if llm_idx > 0:
        q_end, r_end = seq_lens(cigar[-(llm_idx + 1):])
        cigar = concat(
            cigar[: -(llm_idx + 1)],
            parasail_align(query[-q_end:], ref[-r_end:]),
        )

    return cigar


def call_basespace_duplex(temp_seq, temp_qstring, comp_seq, comp_qstring):
    # convert qscores to numpy array
    temp_qscores = np.frombuffer(
        temp_qstring.encode("ascii"), dtype=np.uint8
    ) - 33
    comp_qscores = np.frombuffer(
        comp_qstring.encode("ascii"), dtype=np.uint8
    ) - 33

    temp_qscores = adj_qscores(temp_qscores, temp_seq, qshift=1)
    comp_qscores = adj_qscores(comp_qscores, comp_seq, qshift=-1)

    comp_seq = revcomp(comp_seq)
    comp_qscores = comp_qscores[::-1]

    cigar = edlib_adj_align(temp_seq, comp_seq)
    cigar, temp_st, comp_st = trim_while(cigar)
    cigar, temp_en, comp_en = trim_while(cigar, from_end=True)
    if len(cigar) == 0:
        return None, None

    temp_seq = temp_seq[temp_st:len(temp_seq) - temp_en]
    temp_qscores = temp_qscores[temp_st:len(temp_qscores) - temp_en]
    comp_seq = comp_seq[comp_st:len(comp_seq) - comp_en]
    comp_qscores = comp_qscores[comp_st:len(comp_qscores) - comp_en]
    seq, qstring = compute_consensus(
        cigar,
        temp_seq,
        temp_qscores,
        comp_seq,
        comp_qscores,
    )
    return seq, qstring


def extract_and_call_duplex(read_pair, read_ids_bam):
    temp_read_id, comp_read_id = read_pair
    temp_read = read_ids_bam.get_first_alignment(temp_read_id)
    comp_read = read_ids_bam.get_first_alignment(comp_read_id)
    cons_seq, cons_qstring = call_basespace_duplex(
        temp_read.query_sequence,
        temp_read.qstring,
        comp_read.query_sequence,
        comp_read.qstring,
    )
    return temp_read_id, cons_seq, cons_qstring


def run_all_basespace_duplex(in_fp, out_fp, duplex_pairs_fp, num_workers):
    read_idx_bam = ReadIndexedBam(in_fp)
    duplex_pairs = []
    with open(duplex_pairs_fp) as duplex_pairs_fh:
        for line in duplex_pairs_fh:
            temp_read, comp_read = line.split()
            duplex_pairs.append((temp_read, comp_read))
    duplex_calls = ProcessMap(
        partial(extract_and_call_duplex, read_ids_bam=read_ids_bam),
        duplex_pairs,
        num_workers,
    )
    with open(out_fp, "w") as out_fh:
        for read_id, seq, qstring in tqdm(
            duplex_calls,
            smoothing=0,
            total=len(duplex_pairs),
            desc="Calling Duplex Reads"
        ):
            out_fh.write(f"@{read_id}\n{seq}\n+\n{qstring}\n")
