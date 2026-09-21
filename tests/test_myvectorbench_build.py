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


# MYVECTOR_INDEX_BUILD reports failures as a result row, not as an SQL error, so the
# harness has to look at the text. Before this check a build that could not connect
# back to the server left an empty index and the benchmark went on to report
# meaningless numbers (recall_at_10 = 0.0, ANN as slow as brute-force KNN).

def test_build_success_is_accepted():
    bench.check_index_build_result(
        "Status\nSUCCESS: Index created & saved at (binlog.000002 20865), rows : 500.\n",
        "bench.build_t",
    )


def test_build_connection_error_is_rejected():
    out = ("Status\nError in new connection to build vector index : "
           "Can't connect to local MySQL server through socket '' (111).\n")
    with pytest.raises(RuntimeError, match="did not report SUCCESS"):
        bench.check_index_build_result(out, "bench.build_t")


def test_build_save_error_is_rejected():
    out = "Status\nERROR: index built but could not be saved to disk (see the server log)\n"
    with pytest.raises(RuntimeError, match="did not report SUCCESS"):
        bench.check_index_build_result(out, "bench.build_t")


def test_failure_message_names_the_index_and_the_output():
    with pytest.raises(RuntimeError) as e:
        bench.check_index_build_result("Status\nboom\n", "bench.build_t")
    assert "bench.build_t" in str(e.value)
    assert "boom" in str(e.value)


# Both the plugin and the component need myvector.cnf so the index build can connect
# back to the server; only the component path used to write it.

def test_myvector_cnf_has_connection_settings():
    cnf = bench.myvector_cnf("s3cret")
    lines = cnf.splitlines()
    assert "myvector_host=127.0.0.1" in lines
    assert "myvector_user_id=root" in lines
    assert "myvector_user_password=s3cret" in lines
    assert "myvector_port=3306" in lines
