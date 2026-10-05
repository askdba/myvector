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


def test_sql_string_body_escapes_backslash_then_quote():
    # Under the default sql_mode a trailing backslash would escape the closing quote.
    assert bench.sql_string_body("benchroot") == "benchroot"
    assert bench.sql_string_body("a'b\\") == "a''b\\\\"


def test_resolve_basedir_lost_extraction_race(tmp_path, monkeypatch):
    # Another run finishes extracting while we are still in tar: our rename
    # fails, we use their tree and leave no staging directory behind.
    name = bench.mysql_tarball_name("9.7.0")
    (tmp_path / f"{name}.tar.xz").write_text("")
    basedir = tmp_path / name

    def fake_tar(cmd, check):
        (basedir / "bin").mkdir(parents=True)
        (basedir / "bin" / "mysqld").write_text("theirs")
        staging = cmd[cmd.index("-C") + 1]
        os.makedirs(os.path.join(staging, "bin"))
        open(os.path.join(staging, "bin", "mysqld"), "w").close()

    monkeypatch.setattr(bench.subprocess, "run", fake_tar)
    assert bench.resolve_mysql_basedir("9.7.0", str(tmp_path)) == str(basedir)
    assert (basedir / "bin" / "mysqld").read_text() == "theirs"
    assert not [p for p in tmp_path.iterdir() if ".partial-" in p.name]


@pytest.fixture
def short_tmp():
    # pytest's tmp_path is too long for a unix socket path (108 bytes).
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mvb") as d:
        yield bench.Path(d)


def test_host_workdir_is_per_cell(short_tmp):
    # --all-cells runs 8.4 plugin and 8.4 component in one process; with
    # --keep-workdir the second must not reuse (and wipe) the first's dir.
    plugin = bench.HostServer("8.4", str(short_tmp), str(short_tmp), label="plugin")
    component = bench.HostServer("8.4", str(short_tmp), str(short_tmp), label="component")
    assert plugin.workdir != component.workdir
    assert plugin.workdir.name.endswith("-84-plugin")


def test_host_start_failure_removes_workdir(short_tmp):
    # A failing `mysqld --initialize-insecure` happens inside __enter__, so
    # __exit__ never runs; start() itself must clean up.
    basedir = short_tmp / "mysql"
    (basedir / "bin").mkdir(parents=True)
    (basedir / "lib" / "plugin").mkdir(parents=True)
    mysqld = basedir / "bin" / "mysqld"
    mysqld.write_text("#!/bin/sh\necho init failed >&2\nexit 1\n")
    mysqld.chmod(0o755)
    srv = bench.HostServer("8.4", str(basedir), str(short_tmp / "runs"), label="plugin")
    with pytest.raises(RuntimeError, match="initialize-insecure failed"):
        with srv:
            pass
    assert not srv.workdir.exists()


def test_require_connector_message(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_mysql(name, *args, **kwargs):
        if name.startswith("mysql"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mysql)
    with pytest.raises(RuntimeError, match="pip install mysql-connector-python"):
        bench.require_connector()
