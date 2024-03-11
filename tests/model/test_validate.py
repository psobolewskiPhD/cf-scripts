import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from conda_forge_tick.lazy_json_backends import get_sharded_path
from conda_forge_tick.models.node_attributes import NodeAttributes
from conda_forge_tick.models.pr_info import PrInfo
from conda_forge_tick.models.versions import Versions

"""
These tests validate that the node attributes files in the node_attrs directory are valid JSON and
conform to the NodeAttributes schema.

Since we currently do not use the NodeAttributes schema in production, and also do not enforce some rules
in the conda-smithy linter (e.g. valid URLs in , it is very possible that failures in these tests can occur.

The most likely cause of these failures is that the meta.yaml file of an upstream feedstock does not conform to
the MetaYaml schema - note that some fields of the NodeAttributes schema are derived directly from the meta.yaml file.

You can add the name of a feedstock to the KNOWN_BAD_FEEDSTOCKS list if you know that it will fail these tests.
After fixing the issue, you can remove the feedstock from the list.
"""

NODE_ATTRS_BAD_FEEDSTOCKS = {
    "gmatelastoplasticqpot3d",  # missing platforms
    "semi-ate-stdf",  # missing platforms
    "thrust",  # missing platforms
    "cub",  # missing platforms
    "mamba",  # outdated version field in dependency graph (package.version field removed in meta.yaml)
    "napari",  # outdated version field in dependency graph (package.version field removed in meta.yaml)
    "birka",  # outdated version field in dependency graph (package.version field removed in meta.yaml)
    "xsimd",  # recipe/meta.yaml about.doc_url has a typo in the URL scheme
    "pytao",  # recipe/meta.yaml about.dev_url has a typo in the URL scheme
    "anyqt",  # recipe/meta.yaml about.dev_url has a typo in the URL scheme
    "cubed",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "condastats",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "pytermgui",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "torcpy",  # recipe/meta.yaml about.dev_url has typo
    "scikit-plot",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "matbench-genmetrics",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "neutronics_material_maker",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "gulp",  # recipe/meta.yaml about.dev_url has invalid URL scheme
    "wagtail-bakery",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "mp_time_split",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "shippinglabel",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "cddlib",  # recipe/meta.yaml about.doc_url has "ftp" URL scheme (and is unreachable)
    "cf-autotick-bot-test-package",  # recipe/meta.yaml source.sha256 is invalid
    "vs2008_runtime",  # node attributes error: build.skip is true for non-Windows, but osx and linux are platforms
    "everett",  # recipe/meta.yaml about.dev_url has invalid URL scheme
    "scheil",  # recipe/meta.yaml about.doc_url is not a valid URL
    "llspy-slm",  # recipe/meta.yaml about.doc_url has invalid URL scheme
    "path.py",  # build.noarch: true in meta.yaml, which should probably be build.noarch: python
    "parallel-hashmap",  # build.noarch: true (should be generic) but also probably broken on Windows
    "airflow",  # "grayskull-update" should be "update-grayskull" in conda-forge.yml
    "ipython_memory_usage",  # bot.inspect should be bot.inspection in conda-forge.yml
    "rich-argparse",  # grayskull-update should be update-grayskull in conda-forge.yml
    "htbuilder",  # bot.inspect should be bot.inspection in conda-forge.yml
    "stats_arrays",  # "grayskull-update" should be "update-grayskull" in conda-forge.yml
    "textual-fastdatatable",  # bot.inspect should be bot.inspection in conda-forge.yml
    "buildbot",  # bot.inspect should be bot.inspection in conda-forge.yml
    "sqlalchemy-drill",  # "grayskull-update" should be "update-grayskull" in conda-forge.yml
    "sphinx-sitemap",  # typo in bot.inspection (conda-forge.yml)
    "alibabacloud-openapi-util",  # "grayskull-update" should be "update-grayskull" in conda-forge.yml
    "st-annotated-text",  # bot.inspect should be bot.inspection in conda-forge.yml
    "buildbot-www",  # bot.inspect should be bot.inspection in conda-forge.yml
    "wgpu-native",  # bot.abi_migration_branches should be string, not float (conda-forge.yml)
    "google-ads",  # "grayskull-update" should be "update-grayskull" in conda-forge.yml
    "dnspython",  # "grayskull-update" should be "update-grayskull" in conda-forge.yml
    "pyobjc-framework-corebluetooth",  # bot.inspect should be bot.inspection in conda-forge.yml
    "azure-storage-queue",  # bot.inspect should be bot.inspection in conda-forge.yml
    "graphite2",  # provider.win has invalid value "win".
    "root",  # provider.osx_arm64 has invalid value "osx_64". See issue #238 of the feedstock.
    "espaloma",  # typo in `conda-forge.yml`.azure
    "sparc-x",  # `conda-forge.yml`.channels is unexpected
    "jupyter_core",  # `conda-forge.yml`.abi_migration_branches is unexpected, should be moved to `conda-forge.yml`.bot
    "bamnostic",  # unrecognized field `conda-forge.yml`.build
    "r-v8",  # unrecognized field `conda-forge.yml`.github.win
    "python-utils",  # unrecognized field `conda-forge.yml`.dependencies
    "pyrosm",  # unrecognized option `conda-forge.yml`.build, legacy field `conda-forge.yml`.matrix does not validate
    "sketchnu",  # `conda-forge.yml`.conda_build.pkg_format may not be None
    "sense2vec",  # `conda-forge.yml`.channels is unexpected
    "mpi4py",  # legacy field `conda-forge.yml`.matrix needs further investigation
    "rpaframework",  # `conda-forge.yml`.channel_priority has invalid value "False"
    # see https://github.com/conda-forge/conda-smithy/issues/1863 for the top-level build platform fields
    "nbgrader",  # `conda-forge.yml`.linux_ppc64le should be removed (see above)
    "sccache",  # `conda-forge.yml`.linux_aarch64 should be removed (see above)
    "cmdstan",  # `conda-forge.yml`.linux_aarch64 and linux_ppc64le should be removed (see above)
    "uarray",  # `conda-forge.yml`.linux_ppc64le and linux_aarch64 should be removed (see above)
    "libtk",  # `conda-forge.yml`.linux_ppc64le and linux_aarch64 should be removed (see above)
    "libsimpleitk",  # `conda-forge.yml`.linux_ppc64le and linux_aarch64 should be removed (see above)
    "pnab",  # missing build number in the recipe/meta.yaml
}


@dataclass
class PerPackageModel:
    base_path: Path
    model: TypeAdapter
    bad_feedstocks: set[str] = field(default_factory=set)
    must_exist: bool = True
    """
    If True, the feedstock must exist in the base_path directory.
    """

    @property
    def __name__(self):
        return str(self.base_path.name)


PER_PACKAGE_MODELS: list[PerPackageModel] = [
    PerPackageModel(Path("node_attrs"), NodeAttributes, NODE_ATTRS_BAD_FEEDSTOCKS),
    PerPackageModel(Path("pr_info"), PrInfo),
    PerPackageModel(Path("versions"), Versions, must_exist=False),
]


def get_all_feedstocks() -> set[str]:
    packages: set[str] = set()

    for model in PER_PACKAGE_MODELS:
        for file in model.base_path.rglob("*.json"):
            packages.add(file.stem)

    return packages


def pytest_generate_tests(metafunc):
    packages = get_all_feedstocks()

    if not packages:
        raise ValueError(
            "No packages found. Make sure the cf-graph is in the current working directory."
        )

    all_invalid_feedstocks = set()
    for model in PER_PACKAGE_MODELS:
        all_invalid_feedstocks.update(model.bad_feedstocks)

    nonexistent_bad_feedstocks = all_invalid_feedstocks - packages

    if nonexistent_bad_feedstocks:
        warnings.warn(
            f"Some feedstocks are mentioned as bad feedstock but do not exist: {nonexistent_bad_feedstocks}"
        )

    if "valid_feedstock" in metafunc.fixturenames:
        parameters: list[tuple[PerPackageModel, str]] = []
        for model in PER_PACKAGE_MODELS:
            for package in packages:
                if package not in model.bad_feedstocks:
                    parameters.append((model, package))

        metafunc.parametrize(
            "model,valid_feedstock",
            parameters,
        )
        return

    if "invalid_feedstock" in metafunc.fixturenames:
        parameters: list[tuple[PerPackageModel, str]] = []
        for model in PER_PACKAGE_MODELS:
            for package in packages:
                if package in model.bad_feedstocks:
                    parameters.append((model, package))

        metafunc.parametrize(
            "model,invalid_feedstock",
            parameters,
        )


def test_model_valid(model: PerPackageModel, valid_feedstock: str):
    path = get_sharded_path(model.base_path / f"{valid_feedstock}.json")
    try:
        with open(path) as f:
            node_attrs = f.read()
    except FileNotFoundError:
        if model.must_exist:
            raise
        pytest.skip(f"{path} does not exist")

    model.model.validate_json(node_attrs)


def test_model_invalid(model: PerPackageModel, invalid_feedstock: str):
    path = get_sharded_path(model.base_path / f"{invalid_feedstock}.json")
    try:
        with open(path) as f:
            node_attrs = f.read()
    except FileNotFoundError:
        if model.must_exist:
            raise
        pytest.skip(f"{path} does not exist")

    with pytest.raises(ValidationError):
        model.model.validate_json(node_attrs)
