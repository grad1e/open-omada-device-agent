import os

import pytest

from open_omada_device_agent import bootstrap, cli


def _fail_settings():
    raise RuntimeError("stop after --workdir")


def test_workdir_changes_directory_before_loading_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(os.getcwd())
    monkeypatch.setattr(bootstrap.AgentSettings, "from_environment", staticmethod(_fail_settings))
    monkeypatch.setattr("sys.argv", ["open-omada-agent", "--workdir", str(tmp_path)])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 2
    assert os.path.realpath(os.getcwd()) == os.path.realpath(tmp_path)


def test_missing_workdir_exits_with_configuration_error(tmp_path, monkeypatch):
    monkeypatch.chdir(os.getcwd())
    monkeypatch.setattr("sys.argv", ["open-omada-agent", "--workdir", str(tmp_path / "missing")])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 2
