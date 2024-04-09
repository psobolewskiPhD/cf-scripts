#!/usr/bin/env python
"""This file runs specific tasks for the bot.

All imports from the bot need to be guarded by putting them in the subcommands.
This ensures that we can set important environment variables before any imports,
including `CONDA_BLD_PATH`.

This container is run in a read-only environment except a small tmpfs volume
mounted at `/tmp`. The `TMPDIR` environment variable is set to `/tmp` so that
one can use the `tempfile` module to create temporary files and directories.

These tasks return their info to the bot by printing a JSON blob to stdout.
"""

import copy
import os
import tempfile
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO

import click

existing_feedstock_node_attrs_option = click.option(
    "--existing-feedstock-node-attrs",
    required=True,
    type=str,
    help=(
        "The existing feedstock node attrs JSON as a string "
        "or the name of the feedstock. The data will be downloaded "
        "from the bot metadata if a feedstock name is passed."
    ),
)


@contextmanager
def _setenv(name, value):
    """set an environment variable temporarily"""
    old = os.environ.get(name)
    try:
        os.environ[name] = value
        yield
    finally:
        if old is None:
            del os.environ[name]
        else:
            os.environ[name] = old


def _get_existing_feedstock_node_attrs(existing_feedstock_node_attrs):
    from conda_forge_tick.lazy_json_backends import (
        LazyJson,
        lazy_json_override_backends,
        loads,
    )

    if existing_feedstock_node_attrs.startswith("{"):
        attrs = loads(existing_feedstock_node_attrs)
    else:
        if not existing_feedstock_node_attrs.endswith(".json"):
            existing_feedstock_node_attrs += ".json"

        pth = os.path.join("node_attrs", existing_feedstock_node_attrs)
        with lazy_json_override_backends(["github"], use_file_cache=False), LazyJson(
            pth
        ) as lzj:
            attrs = copy.deepcopy(lzj.data)

    return attrs


def _run_bot_task(func, **kwargs):
    from conda_forge_tick.lazy_json_backends import dumps

    data = None
    ret = copy.copy(kwargs)
    outerr = StringIO()
    try:
        with redirect_stdout(outerr), redirect_stderr(outerr):
            data = func(**kwargs)

        ret["data"] = data
        ret["container_stdout_stderr"] = outerr.getvalue()

    except Exception as e:
        ret["data"] = data
        ret["container_stdout_stderr"] = outerr.getvalue()
        ret["error"] = repr(e)
        ret["traceback"] = traceback.format_exc()

    print(dumps(ret))


def _get_latest_version(*, existing_feedstock_node_attrs, sources):
    from conda_forge_tick.os_utils import pushd
    from conda_forge_tick.update_upstream_versions import (
        all_version_sources,
        get_latest_version_local,
    )

    _sources = all_version_sources()
    if sources is not None:
        sources = sources.split(",")
        sources = [s.strip().lower() for s in sources]
        _sources = [s for s in _sources if s.name.strip().lower() in sources]

    with tempfile.TemporaryDirectory() as tmpdir:
        with pushd(tmpdir):
            attrs = _get_existing_feedstock_node_attrs(existing_feedstock_node_attrs)
            name = attrs["feedstock_name"]

            data = get_latest_version_local(
                name,
                attrs,
                _sources,
            )
    return data


def _parse_feedstock(
    *, existing_feedstock_node_attrs, meta_yaml, conda_forge_yaml, mark_not_archived
):
    with tempfile.TemporaryDirectory() as tmpdir_cbld, _setenv(
        "CONDA_BLD_PATH", os.path.join(tmpdir_cbld, "conda-bld")
    ):
        os.makedirs(os.path.join(tmpdir_cbld, "conda-bld"), exist_ok=True)

        from conda_forge_tick.feedstock_parser import load_feedstock_local
        from conda_forge_tick.os_utils import pushd

        with tempfile.TemporaryDirectory() as tmpdir:
            with pushd(tmpdir):
                attrs = _get_existing_feedstock_node_attrs(
                    existing_feedstock_node_attrs
                )
                name = attrs["feedstock_name"]

                outerr = StringIO()
                with redirect_stdout(outerr), redirect_stderr(outerr):
                    load_feedstock_local(
                        name,
                        attrs,
                        meta_yaml=meta_yaml,
                        conda_forge_yaml=conda_forge_yaml,
                        mark_not_archived=mark_not_archived,
                    )
    return attrs


@click.group()
def cli():
    pass


@cli.command(name="parse-feedstock")
@existing_feedstock_node_attrs_option
@click.option("--meta-yaml", default=None, type=str, help="The meta.yaml file to use.")
@click.option(
    "--conda-forge-yaml", default=None, type=str, help="The meta.yaml file to use."
)
@click.option(
    "--mark-not-archived", is_flag=True, help="Mark the feedstock as not archived."
)
def parse_feedstock(
    existing_feedstock_node_attrs, meta_yaml, conda_forge_yaml, mark_not_archived
):
    return _run_bot_task(
        _parse_feedstock,
        existing_feedstock_node_attrs=existing_feedstock_node_attrs,
        meta_yaml=meta_yaml,
        conda_forge_yaml=conda_forge_yaml,
        mark_not_archived=mark_not_archived,
    )


@cli.command(name="get-latest-version")
@existing_feedstock_node_attrs_option
@click.option(
    "--sources",
    default=None,
    type=str,
    help="Comma separated list of sources to use. Default is all sources as given by `all_version_sources`.",
)
def get_latest_version(existing_feedstock_node_attrs, sources):
    return _run_bot_task(
        _get_latest_version,
        existing_feedstock_node_attrs=existing_feedstock_node_attrs,
        sources=sources,
    )


if __name__ == "__main__":
    cli()
