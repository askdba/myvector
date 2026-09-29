import importlib.util
import os

import pytest


def _load_bench():
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'myvectorbench.py')
    spec = importlib.util.spec_from_file_location("myvectorbench", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load_bench()


# Recall and the ef_search sweep used to query with vectors sampled from the
# indexed rows, so every query's nearest neighbour was itself (distance 0) and
# 1 of the 10 hits came free. holdout_queries keeps a query set out of the index.

def _vectors(n):
    return [[float(i), float(-i)] for i in range(n)]


def test_holdout_zero_keeps_every_row_indexed():
    vecs = _vectors(10)
    indexed, queries = bench.split_holdout(vecs, 0)
    assert indexed == vecs
    assert queries is None


def test_holdout_queries_are_not_indexed():
    vecs = _vectors(1000)
    indexed, queries = bench.split_holdout(vecs, 100)
    assert len(indexed) == 900
    assert len(queries) == 100
    indexed_keys = {tuple(v) for v in indexed}
    assert not any(tuple(q) in indexed_keys for q in queries)
    assert sorted(map(tuple, indexed + queries)) == sorted(map(tuple, vecs))


def test_holdout_split_is_deterministic():
    vecs = _vectors(500)
    assert bench.split_holdout(vecs, 50) == bench.split_holdout(vecs, 50)


def test_holdout_must_leave_rows_to_index():
    with pytest.raises(ValueError):
        bench.split_holdout(_vectors(10), 10)


# The distance metric used to be hard-coded to L2 in the index and in every
# ground-truth query. GloVe is normally benchmarked with cosine (angular).

def test_distance_defaults_to_l2():
    assert bench.distance_metric({}) == "L2"


@pytest.mark.parametrize("given,expected", [("cosine", "Cosine"), ("Cosine", "Cosine"), ("l2", "L2")])
def test_distance_is_normalised(given, expected):
    assert bench.distance_metric({"distance": given}) == expected


def test_unknown_distance_is_rejected():
    with pytest.raises(ValueError):
        bench.distance_metric({"distance": "manhattan"})


def test_knn_sql_uses_the_configured_metric():
    sql = bench.knn_sql([1.0, 2.0], "Cosine")
    assert "myvector_distance(vec," in sql
    assert "'Cosine')" in sql
    assert "'L2'" not in sql


class _RecordingContainer:
    def __init__(self):
        self.statements = []

    def sql(self, statement, db=""):
        self.statements.append(statement)
        return ""


def test_bench_table_uses_the_configured_metric():
    c = _RecordingContainer()
    bench._create_bench_table(c, 2, 10, 16, 200, "bench", "build_t", dist="Cosine")
    create = [s for s in c.statements if s.startswith("CREATE TABLE")][0]
    assert "dist=Cosine" in create


# _load_tsv used to treat the first field as a word only if it failed to parse
# as a float. GloVe has thousands of numeric words ("100", "2008", "nan"), so
# those rows kept the word as a vector component and lost their last one
# (4.3% of the first 101k glove.6B.50d rows).

def test_tsv_numeric_words_are_not_vector_components(tmp_path):
    path = tmp_path / "vecs.txt"
    path.write_text(
        "the 0.1 0.2 0.3\n"
        "2008 0.4 0.5 0.6\n"
        "nan 0.7 0.8 0.9\n"
        "1.0 1.1 1.2\n"          # no word column
    )
    assert bench._load_tsv(str(path), 10, 3) == [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
        [0.7, 0.8, 0.9],
        [1.0, 1.1, 1.2],
    ]


def test_tsv_word_with_extra_columns_still_loads(tmp_path):
    path = tmp_path / "vecs.txt"
    path.write_text("the 0.1 0.2 0.3 9.9\n")
    assert bench._load_tsv(str(path), 10, 3) == [[0.1, 0.2, 0.3]]


# A file without a word column may still carry one trailing field. Per line,
# "1.0 1.1 1.2 9.9" looks like GloVe's "2008 0.4 0.5 0.6", so whether lines
# start with a word is decided for the whole file.

def test_tsv_wordless_file_with_trailing_field_keeps_first_component(tmp_path):
    path = tmp_path / "vecs.txt"
    path.write_text("1.0 1.1 1.2 9.9\n2.0 2.1 2.2 9.9\n")
    assert bench._load_tsv(str(path), 10, 3) == [[1.0, 1.1, 1.2], [2.0, 2.1, 2.2]]


def test_negative_holdout_is_rejected():
    with pytest.raises(ValueError):
        bench.load_workload("synthetic", {"rows": 10, "dim": 2, "holdout_queries": -1})


def test_load_workload_splits_rows_and_holdout():
    wp = {"rows": 50, "dim": 4, "holdout_queries": 5}
    indexed, held_out = bench.load_workload("synthetic", wp)
    assert len(indexed) == 50
    assert len(held_out) == 5


def test_load_workload_reports_short_datasets(tmp_path):
    # 12 vectors on disk, 10 indexed rows + 5 held out requested: the
    # indexed count is what is left, not the configured 10.
    path = tmp_path / "vecs.txt"
    path.write_text("".join(f"w{i} {i}.0 1.0\n" for i in range(12)))
    indexed, held_out = bench.load_workload(str(path), {"rows": 10, "dim": 2, "holdout_queries": 5})
    assert len(held_out) == 5
    assert len(indexed) == 7
