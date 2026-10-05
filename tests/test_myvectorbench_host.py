import importlib.util
import os

import pytest


def _load(name, filename):
    path = os.path.join(os.path.dirname(__file__), '..', 'scripts', filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load("myvectorbench", "myvectorbench.py")
compare = _load("myvectorbench_compare", "myvectorbench-compare.py")


# Host mode (#133) downloads Oracle's generic tarball for the exact patch release
# the MyVector artifact was built against.

def test_tarball_name_per_arch():
    assert (bench.mysql_tarball_name("8.4.8", "aarch64")
            == "mysql-8.4.8-linux-glibc2.28-aarch64")
    assert (bench.mysql_tarball_name("9.7.0", "x86_64")
            == "mysql-9.7.0-linux-glibc2.28-x86_64")
    assert (bench.mysql_tarball_name("26.7.0", "arm64")
            == "mysql-26.7.0-linux-glibc2.28-aarch64")


def test_tarball_name_rejects_unknown_arch():
    with pytest.raises(ValueError, match="riscv64"):
        bench.mysql_tarball_name("8.4.8", "riscv64")


def test_tarball_urls_try_current_then_archive():
    # 8.4.8 x86_64 is only under archives/ once a newer 8.4 patch ships.
    urls = bench.mysql_tarball_urls("8.4.8", "x86_64")
    assert urls == [
        "https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-8.4.8-linux-glibc2.28-x86_64.tar.xz",
        "https://cdn.mysql.com/archives/mysql-8.4/mysql-8.4.8-linux-glibc2.28-x86_64.tar.xz",
    ]


def test_full_versions_match_build_script_tags():
    assert bench.MYSQL_FULL_VERSIONS == {"8.4": "8.4.8", "9.7": "9.7.0", "26.7": "26.7.0"}


def test_resolve_basedir_reuses_extracted_tree(tmp_path):
    # Already extracted: no download, no tar.
    basedir = tmp_path / bench.mysql_tarball_name("8.4.8")
    (basedir / "bin").mkdir(parents=True)
    (basedir / "bin" / "mysqld").write_text("")
    assert bench.resolve_mysql_basedir("8.4.8", str(tmp_path)) == str(basedir)


def test_myvector_cnf_uses_given_port():
    assert "myvector_port=40123" in bench.myvector_cnf("pw", 40123).splitlines()


# Result rows from the connector must look like `mysql --batch --silent` output,
# because recall parsing is shared with the Docker path.

def test_format_cell_matches_mysql_batch_output():
    assert bench._format_cell(None) == "NULL"
    assert bench._format_cell(b"abc") == "abc"
    assert bench._format_cell(42) == "42"


def test_machine_key_is_filesystem_safe():
    key = bench.machine_key("aarch64", "Neoverse-N1 (r3p1) @ 3.0GHz", 16)
    assert key == "aarch64-neoverse-n1-r3p1-3-0ghz-16c"
    assert bench.machine_key("x86_64", "", 4) == "x86_64-unknown-4c"


def test_host_metadata_has_no_hostname():
    meta = bench.host_metadata()
    for field in ("arch", "cpu_model", "cpu_cores", "mem_total_gb", "kernel", "machine_key"):
        assert field in meta
    hostname = os.uname().nodename
    assert hostname not in str(meta)


def test_cell_artifact_dir_prefers_ol9_plugin(tmp_path):
    (tmp_path / "plugin-8.4").mkdir()
    cell = {"mysql": "8.4", "build": "plugin"}
    assert bench.cell_artifact_dir(cell, str(tmp_path)) == str(tmp_path / "plugin-8.4")
    (tmp_path / "plugin-8.4-ol9").mkdir()
    (tmp_path / "plugin-8.4-ol9" / "myvector.so").write_text("")
    assert bench.cell_artifact_dir(cell, str(tmp_path)) == str(tmp_path / "plugin-8.4-ol9")


def test_cell_artifact_dir_component():
    cell = {"mysql": "9.7", "build": "component", "artifact": "component-9.7"}
    assert bench.cell_artifact_dir(cell, "dist") == os.path.join("dist", "component-9.7")
    assert (bench.cell_artifact_dir({"mysql": 26.7, "build": "component"}, "dist")
            == os.path.join("dist", "component-26.7"))


def test_host_server_socket_path_limit(tmp_path):
    with pytest.raises(ValueError, match="socket path too long"):
        bench.HostServer("8.4", str(tmp_path), str(tmp_path / ("x" * 120)))


# Baselines must only be compared with results from the same kind of run.

def _result(mode=None, key=None):
    r = {"metrics": {}}
    if mode:
        r["server_mode"] = mode
    if key:
        r["host"] = {"machine_key": key}
    return r


def test_compare_warns_on_server_mode_mismatch():
    w = compare.comparability_warnings(_result(), _result("host", "a"))
    assert len(w) == 1 and "server mode differs" in w[0]


def test_compare_warns_on_machine_mismatch():
    w = compare.comparability_warnings(_result("host", "a"), _result("host", "b"))
    assert len(w) == 1 and "machine type differs" in w[0]


def test_compare_quiet_for_same_machine_and_legacy_results():
    assert compare.comparability_warnings(_result("host", "a"), _result("host", "a")) == []
    assert compare.comparability_warnings(_result(), _result()) == []
