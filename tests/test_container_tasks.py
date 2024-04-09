import copy
import json
import subprocess

import conda_smithy

from conda_forge_tick.feedstock_parser import load_feedstock_containerized
from conda_forge_tick.lazy_json_backends import (
    LazyJson,
    dumps,
    lazy_json_override_backends,
)
from conda_forge_tick.update_upstream_versions import (
    all_version_sources,
    get_latest_version_containerized,
)
from conda_forge_tick.utils import (
    get_default_container_name,
    get_default_container_run_args,
)


def test_container_tasks_get_latest_version():
    res = subprocess.run(
        [
            *get_default_container_run_args(),
            "-t",
            get_default_container_name(),
            "python",
            "/opt/autotick-bot/docker/run_bot_task.py",
            "get-latest-version",
            "--existing-feedstock-node-attrs=conda-smithy",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    data = json.loads(res.stdout)
    assert "error" not in data
    assert data["data"]["new_version"] == conda_smithy.__version__


def test_container_tasks_get_latest_version_json():
    with lazy_json_override_backends(["github"], use_file_cache=False):
        with LazyJson("node_attrs/conda-smithy.json") as lzj:
            existing_feedstock_node_attrs = dumps(lzj.data)

    res = subprocess.run(
        [
            *get_default_container_run_args(),
            "-t",
            get_default_container_name(),
            "python",
            "/opt/autotick-bot/docker/run_bot_task.py",
            "get-latest-version",
            "--existing-feedstock-node-attrs",
            existing_feedstock_node_attrs,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    data = json.loads(res.stdout)
    assert "error" not in data
    assert data["data"]["new_version"] == conda_smithy.__version__


def test_get_latest_version_containerized():
    with lazy_json_override_backends(["github"], use_file_cache=False):
        with LazyJson("node_attrs/conda-smithy.json") as lzj:
            attrs = copy.deepcopy(lzj.data)

    data = get_latest_version_containerized(
        "conda-smithy", attrs, all_version_sources()
    )
    assert data["new_version"] == conda_smithy.__version__


def test_container_tasks_parse_feedstock():
    res = subprocess.run(
        [
            *get_default_container_run_args(),
            "-t",
            get_default_container_name(),
            "python",
            "/opt/autotick-bot/docker/run_bot_task.py",
            "parse-feedstock",
            "--existing-feedstock-node-attrs=conda-smithy",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    data = json.loads(res.stdout)
    assert "error" not in data

    with lazy_json_override_backends(["github"], use_file_cache=False), LazyJson(
        "node_attrs/conda-smithy.json"
    ) as lzj:
        attrs = copy.deepcopy(lzj.data)

    assert data["data"]["feedstock_name"] == attrs["feedstock_name"]
    assert not data["data"]["parsing_error"]
    assert data["data"]["raw_meta_yaml"] == attrs["raw_meta_yaml"]


def test_container_tasks_parse_feedstock_json():
    with lazy_json_override_backends(["github"], use_file_cache=False):
        with LazyJson("node_attrs/conda-smithy.json") as lzj:
            attrs = copy.deepcopy(lzj.data)
            existing_feedstock_node_attrs = dumps(lzj.data)

    res = subprocess.run(
        [
            *get_default_container_run_args(),
            "-t",
            get_default_container_name(),
            "python",
            "/opt/autotick-bot/docker/run_bot_task.py",
            "parse-feedstock",
            "--existing-feedstock-node-attrs",
            existing_feedstock_node_attrs,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    data = json.loads(res.stdout)
    assert "error" not in data
    assert data["data"]["feedstock_name"] == attrs["feedstock_name"]
    assert not data["data"]["parsing_error"]
    assert data["data"]["raw_meta_yaml"] == attrs["raw_meta_yaml"]


def test_load_feedstock_containerized():
    with lazy_json_override_backends(["github"], use_file_cache=False):
        with LazyJson("node_attrs/conda-smithy.json") as lzj:
            attrs = copy.deepcopy(lzj.data)

    data = load_feedstock_containerized("conda-smithy", attrs)
    assert data["feedstock_name"] == attrs["feedstock_name"]
    assert not data["parsing_error"]
    assert data["raw_meta_yaml"] == attrs["raw_meta_yaml"]
