"""Classes for migrating repos"""

import datetime
import logging
import os
import re
import typing
from itertools import chain
from typing import Any, List, Optional, Sequence, Set

import dateutil.parser
import networkx as nx

from conda_forge_tick.contexts import FeedstockContext, MigratorContext
from conda_forge_tick.lazy_json_backends import LazyJson
from conda_forge_tick.make_graph import make_outputs_lut_from_graph
from conda_forge_tick.path_lengths import cyclic_topological_sort
from conda_forge_tick.update_recipe import update_build_number
from conda_forge_tick.utils import (
    frozen_to_json_friendly,
    get_bot_run_url,
    get_keys_default,
)

if typing.TYPE_CHECKING:
    from conda_forge_tick.utils import JsonFriendly

    from ..migrators_types import AttrsTypedDict, MigrationUidTypedDict, PackageName


logger = logging.getLogger(__name__)


def _sanitized_muids(pred: List[dict]) -> List["JsonFriendly"]:
    lst = []
    for pr in pred:
        d: "JsonFriendly" = {"data": pr["data"], "keys": pr["keys"]}
        lst.append(d)
    return lst


def _parse_bad_attr(attrs: "AttrsTypedDict", not_bad_str_start: str) -> bool:
    """Overlook some bad entries"""
    bad = attrs.get("pr_info", {}).get("bad", False)
    if isinstance(bad, str):
        bad_bool = not bad.startswith(not_bad_str_start)
    else:
        bad_bool = bad

    return bad_bool or attrs.get("parsing_error", False)


def _gen_active_feedstocks_payloads(nodes, gx):
    for node in nodes:
        try:
            payload = gx.nodes[node]["payload"]
        except KeyError as e:
            print(node)
            raise e

        # we don't need to look at archived feedstocks
        # they are always "migrated"
        if payload.get("archived", False):
            continue
        else:
            yield node, payload


class MiniMigrator:
    post_migration = False

    def filter(self, attrs: "AttrsTypedDict", not_bad_str_start: str = "") -> bool:
        """If true don't act upon node

        Parameters
        ----------
        attrs : dict
            The node attributes

        Returns
        -------
        bool :
            True if node is to be skipped
        """
        return True

    def migrate(self, recipe_dir: str, attrs: "AttrsTypedDict", **kwargs: Any) -> None:
        """Perform the migration, updating the ``meta.yaml``

        Parameters
        ----------
        recipe_dir : str
            The directory of the recipe
        attrs : dict
            The node attributes

        Returns
        -------
        namedtuple or bool:
            If namedtuple continue with PR, if False scrap local folder
        """
        return


class Migrator:
    """Base class for Migrators"""

    name: str

    rerender = True

    # bump this if the migrator object needs a change mid migration
    migrator_version = 0

    allow_empty_commits = False

    build_patterns = (
        (re.compile(r"(\s*?)number:\s*([0-9]+)"), "number: {}"),
        (
            re.compile(r'(\s*?){%\s*set build_number\s*=\s*"?([0-9]+)"?\s*%}'),
            "{{% set build_number = {} %}}",
        ),
        (
            re.compile(r'(\s*?){%\s*set build\s*=\s*"?([0-9]+)"?\s*%}'),
            "{{% set build = {} %}}",
        ),
    )

    def __init__(
        self,
        pr_limit: int = 0,
        # TODO: Validate this?
        obj_version: Optional[int] = None,
        piggy_back_migrations: Optional[Sequence[MiniMigrator]] = None,
        check_solvable=True,
    ):
        self.piggy_back_migrations = piggy_back_migrations or []
        self.pr_limit = pr_limit
        self.obj_version = obj_version
        self.ctx: MigratorContext = None
        self.check_solvable = check_solvable

    def bind_to_ctx(self, migrator_ctx: MigratorContext) -> None:
        self.ctx = migrator_ctx

    def downstream_children(
        self,
        feedstock_ctx: FeedstockContext,
        limit: int = 5,
    ) -> List["PackageName"]:
        """Utility method for getting a list of follow on packages"""
        return [
            a[1]
            for a in list(
                self.ctx.effective_graph.out_edges(feedstock_ctx.package_name),
            )
        ][:limit]

    def filter(self, attrs: "AttrsTypedDict", not_bad_str_start: str = "") -> bool:
        """If true don't act upon node

        Parameters
        ----------
        attrs : dict
            The node attributes
        not_bad_str_start : str, optional
            If the 'bad' notice starts with the string then it is not
            to be excluded. For example, rebuild migrations don't need
            to worry about if the upstream can be fetched. Defaults to ``''``

        Returns
        -------
        bool :
            True if node is to be skipped
        """
        # never run on archived feedstocks
        # don't run on things we've already done
        # don't run on bad nodes

        __name = attrs.get("name", "")

        def parse_already_pred() -> bool:
            pr_data = frozen_to_json_friendly(self.migrator_uid(attrs))
            migrator_uid: "MigrationUidTypedDict" = typing.cast(
                "MigrationUidTypedDict",
                pr_data["data"],
            )
            already_migrated_uids: typing.Iterable["MigrationUidTypedDict"] = list(
                z["data"] for z in attrs.get("pr_info", {}).get("PRed", [])
            )
            already_pred = migrator_uid in already_migrated_uids
            if already_pred:
                ind = already_migrated_uids.index(migrator_uid)
                logger.debug(f"{__name}: already PRed: uid: {migrator_uid}")
                if "PR" in attrs.get("pr_info", {}).get("PRed", [])[ind]:
                    if isinstance(
                        attrs.get("pr_info", {}).get("PRed", [])[ind]["PR"],
                        LazyJson,
                    ):
                        with attrs.get("pr_info", {}).get("PRed", [])[ind][
                            "PR"
                        ] as mg_attrs:
                            logger.debug(
                                "{}: already PRed: PR file: {}".format(
                                    __name, mg_attrs.file_name
                                ),
                            )

                            html_url = mg_attrs.get("html_url", "no url")

                            logger.debug(f"{__name}: already PRed: url: {html_url}")

            return already_pred

        if attrs.get("archived", False):
            logger.debug("%s: archived" % __name)

        bad_attr = _parse_bad_attr(attrs, not_bad_str_start)
        if bad_attr:
            logger.debug("%s: bad attr" % __name)

        return attrs.get("archived", False) or parse_already_pred() or bad_attr

    def get_possible_feedstock_branches(self, attrs: "AttrsTypedDict") -> List[str]:
        """Return the valid possible branches to which to apply this migration to
        for the given attrs.

        Parameters
        ----------
        attrs : dict
            The node attributes

        Returns
        -------
        branches : list of str
            List if valid branches for this migration.
        """
        branches = ["main"]
        try:
            branches += get_keys_default(
                attrs,
                ["conda-forge.yml", "bot", "abi_migration_branches"],
                {},
                [],
            )
        except Exception:
            logger.exception(f"Invalid value for {attrs.get('conda-forge.yml', {})=}")
        # make sure this is always a string
        return [str(b) for b in branches]

    def run_pre_piggyback_migrations(
        self, recipe_dir: str, attrs: "AttrsTypedDict", **kwargs: Any
    ) -> "MigrationUidTypedDict":
        """Perform any pre piggyback migrations, updating the feedstock.

        Parameters
        ----------
        recipe_dir : str
            The directory of the recipe
        attrs : dict
            The node attributes

        """
        for mini_migrator in self.piggy_back_migrations:
            if mini_migrator.post_migration:
                continue
            if not mini_migrator.filter(attrs):
                mini_migrator.migrate(recipe_dir, attrs, **kwargs)

    def run_post_piggyback_migrations(
        self, recipe_dir: str, attrs: "AttrsTypedDict", **kwargs: Any
    ) -> "MigrationUidTypedDict":
        """Perform any post piggyback migrations, updating the feedstock.

        Parameters
        ----------
        recipe_dir : str
            The directory of the recipe
        attrs : dict
            The node attributes

        """
        for mini_migrator in self.piggy_back_migrations:
            if not mini_migrator.post_migration:
                continue
            if not mini_migrator.filter(attrs):
                mini_migrator.migrate(recipe_dir, attrs, **kwargs)

    def migrate(
        self, recipe_dir: str, attrs: "AttrsTypedDict", **kwargs: Any
    ) -> "MigrationUidTypedDict":
        """Perform the migration, updating the ``meta.yaml``

        Parameters
        ----------
        recipe_dir : str
            The directory of the recipe
        attrs : dict
            The node attributes

        Returns
        -------
        namedtuple or bool:
            If namedtuple continue with PR, if False scrap local folder
        """
        return self.migrator_uid(attrs)

    def pr_body(self, feedstock_ctx: FeedstockContext, add_label_text=True) -> str:
        """Create a PR message body

        Returns
        -------
        body: str
            The body of the PR message
            :param feedstock_ctx:
        """
        body = "{}\n\n"

        if add_label_text:
            body += (
                "If this PR was opened in error or needs to be updated please add "
                "the `bot-rerun` label to this PR. The bot will close this PR and "
                "schedule another one. If you do not have permissions to add this "
                "label, you can use the phrase "
                "<code>@<space/>conda-forge-admin, please rerun bot</code> "
                "in a PR comment to have the `conda-forge-admin` add it for you.\n\n"
            )

        body += (
            "<sub>"
            "This PR was created by the [regro-cf-autotick-bot](https://github.com/regro/cf-scripts). "
            "The **regro-cf-autotick-bot** is a service to automatically "
            "track the dependency graph, migrate packages, and "
            "propose package version updates for conda-forge. "
            "Feel free to drop us a line if there are any "
            "[issues](https://github.com/regro/cf-scripts/issues)! "
            + f"This PR was generated by {get_bot_run_url()} - please use this URL for debugging."
            + "</sub>"
        )
        return body

    def commit_message(self, feedstock_ctx: FeedstockContext) -> str:
        """Create a commit message
        :param feedstock_ctx:
        """
        return f"migration: {self.__class__.__name__}"

    def pr_title(self, feedstock_ctx: FeedstockContext) -> str:
        """Title for PR
        :param feedstock_ctx:
        """
        return "PR from Regro-cf-autotick-bot"

    def remote_branch(self, feedstock_ctx: FeedstockContext) -> str:
        """Branch to use on local and remote
        :param feedstock_context:
        """
        return "bot-pr"

    def migrator_uid(self, attrs: "AttrsTypedDict") -> "MigrationUidTypedDict":
        """Make a unique id for this migrator and node attrs

        Parameters
        ----------
        attrs
            Node attrs

        Returns
        -------
        nt: frozen_to_json_friendly
            The unique id as a frozen_to_json_friendly (so it can be
            used as keys in dicts)
        """
        d: "MigrationUidTypedDict" = {
            "migrator_name": self.__class__.__name__,
            "migrator_version": self.migrator_version,
            "bot_rerun": False,
        }
        # Carveout for old migrators w/o obj_versions
        if self.obj_version:
            d["migrator_object_version"] = self.obj_version

        branch = attrs.get("branch", "main")
        if branch != "main" and branch != "master":
            d["branch"] = branch

        return d

    def order(
        self,
        graph: nx.DiGraph,
        total_graph: nx.DiGraph,
    ) -> Sequence["PackageName"]:
        """Order to run migrations in

        Parameters
        ----------
        graph : nx.DiGraph
            The graph of migratable PRs

        Returns
        -------

        """
        top_level = {
            node
            for node in graph
            if not list(graph.predecessors(node))
            or list(graph.predecessors(node)) == [node]
        }
        return cyclic_topological_sort(graph, top_level)

    def set_build_number(self, filename: str) -> None:
        """Bump the build number of the specified recipe.

        Parameters
        ----------
        filename : str
            Path the the meta.yaml
        """
        with open(filename) as f:
            raw = f.read()

        new_myaml = update_build_number(
            raw,
            self.new_build_number,
            build_patterns=self.build_patterns,
        )

        with open(filename, "w") as f:
            f.write(new_myaml)

    def new_build_number(self, old_number: int) -> int:
        """Determine the new build number to use.

        Parameters
        ----------
        old_number : int
            Old build number detected

        Returns
        -------
        new_build_number
        """
        increment = getattr(self, "bump_number", 1)
        return old_number + increment

    @classmethod
    def migrator_label(cls) -> dict:
        # This is the label that the bot will attach to a pr made by the bot
        return {
            "name": f"bot-{cls.__name__.lower()}",
            "description": (cls.__doc__ or "").strip(),
            "color": "#6c64ff",
        }


class GraphMigrator(Migrator):
    def __init__(
        self,
        *,
        name: Optional[str] = None,
        graph: nx.DiGraph = None,
        pr_limit: int = 0,
        top_level: Set["PackageName"] = None,
        cycles: Optional[Sequence["PackageName"]] = None,
        obj_version: Optional[int] = None,
        piggy_back_migrations: Optional[Sequence[MiniMigrator]] = None,
        check_solvable=True,
        ignored_deps_per_node=None,
    ):
        super().__init__(
            pr_limit,
            obj_version,
            piggy_back_migrations,
            check_solvable=check_solvable,
        )
        # TODO: Grab the graph from the migrator ctx
        if graph is None:
            self.graph = nx.DiGraph()
        else:
            self.graph = graph

        # IDK if this will be there so I am going to make it if needed
        if "outputs_lut" in self.graph.graph:
            self.outputs_lut = self.graph.graph["outputs_lut"]
        else:
            self.outputs_lut = make_outputs_lut_from_graph(self.graph)

        self.name = name
        self.top_level = top_level or set()
        self.cycles = set(chain.from_iterable(cycles or []))
        self.ignored_deps_per_node = ignored_deps_per_node or {}

    def all_predecessors_issued_and_stale(self, attrs: "AttrsTypedDict") -> bool:
        # Check if all upstreams have been issue and are stale
        for node, payload in _gen_active_feedstocks_payloads(
            self.graph.predecessors(attrs["feedstock_name"]),
            self.graph,
        ):
            if node in self.ignored_deps_per_node.get(
                attrs.get("feedstock_name", None),
                [],
            ):
                continue

            muid = frozen_to_json_friendly(self.migrator_uid(payload))
            pr_muids = _sanitized_muids(payload.get("pr_info", {}).get("PRed", []))
            if muid not in pr_muids:
                logger.debug(
                    "node %s PR %s not yet issued!",
                    node,
                    muid.get("data", {}).get("name", None),
                )
                # not yet issued
                return False
            else:
                # issued so check timestamp
                pr_index = pr_muids.index(muid)
                ts = (
                    payload.get("pr_info", {})
                    .get("PRed", [])[pr_index]
                    .get("PR", {})
                    .get("created_at", None)
                )
                state = (
                    payload.get("pr_info", {})
                    .get("PRed", [])[pr_index]
                    .get("PR", {"state": "open"})
                    .get("state", "")
                )
                if state == "open":
                    if ts is not None:
                        now = datetime.datetime.now(datetime.timezone.utc)
                        ts = dateutil.parser.parse(ts)
                        if now - ts < datetime.timedelta(days=14):
                            logger.debug(
                                "node %s has PR %s open for %s",
                                node,
                                muid.get("data", {}).get("name", None),
                                now - ts,
                            )
                            return False
                    else:
                        # no timestamp so keep things open
                        logger.debug(
                            "node %s has PR %s:%s with no timestamp",
                            node,
                            muid.get("data", {}).get("name", None),
                            payload.get("pr_info", {})
                            .get("PRed", [])[pr_index]["PR"]
                            .file_name,
                        )
                        return False

        return True

    def predecessors_not_yet_built(self, attrs: "AttrsTypedDict") -> bool:
        # Check if all upstreams have been built
        for node, payload in _gen_active_feedstocks_payloads(
            self.graph.predecessors(attrs["feedstock_name"]),
            self.graph,
        ):
            if node in self.ignored_deps_per_node.get(
                attrs.get("feedstock_name", None),
                [],
            ):
                continue

            muid = frozen_to_json_friendly(self.migrator_uid(payload))

            if muid not in _sanitized_muids(
                payload.get("pr_info", {}).get("PRed", []),
            ):
                logger.debug("not yet built: %s" % node)
                return True

            # This is due to some PRed_json loss due to bad graph deploy outage
            for m_pred_json in payload.get("pr_info", {}).get("PRed", []):
                if m_pred_json["data"] == muid["data"]:
                    break
            else:
                m_pred_json = None

            # note that if the bot is missing the PR we assume it is open
            # so that errors halt the migration and can be fixed
            if (
                m_pred_json
                and m_pred_json.get("PR", {"state": "open"}).get("state", "") == "open"
            ):
                logger.debug("not yet built: %s" % node)
                return True

        return False

    def filter(self, attrs: "AttrsTypedDict", not_bad_str_start: str = "") -> bool:
        name = attrs.get("name", "")

        if super().filter(attrs, "Upstream:"):
            logger.debug("filter %s: archived or done", name)
            return True

        if attrs.get("feedstock_name", None) not in self.graph:
            logger.debug("filter %s: node not in graph", name)
            return True

        # If in top level or in a cycle don't check for upstreams just build
        if (attrs["feedstock_name"] in self.top_level) or (
            attrs["feedstock_name"] in self.cycles
        ):
            return False

        # once all PRs are issued (not merged) and old propose the change in pin
        if name == "conda-forge-pinning" and self.all_predecessors_issued_and_stale(
            attrs=attrs,
        ):
            logger.debug("not filtered %s: pinning parents issued and stale", name)
            return False

        # Check if all upstreams have been built
        if self.predecessors_not_yet_built(attrs=attrs):
            logger.debug("filter %s: parents not built", name)
            return True

        return False

    def migrator_uid(self, attrs: "AttrsTypedDict") -> "MigrationUidTypedDict":
        n = super().migrator_uid(attrs)
        n["name"] = self.name
        return n


class Replacement(Migrator):
    """Migrator for replacing one package with another.

    Parameters
    ----------
    old_pkg : str
        The package to be replaced.
    new_pkg : str
        The package to replace the `old_pkg`.
    rationale : str
        The reason the for the migration. Should be a full statement.
    graph : nx.DiGraph, optional
        The graph of feedstocks.
    pr_limit : int, optional
        The maximum number of PRs made per run of the bot.
    check_solvable : bool, optional
        If True, uses mamba to check if the final recipe is solvable.
    """

    migrator_version = 0
    rerender = True

    def __init__(
        self,
        *,
        old_pkg: "PackageName",
        new_pkg: "PackageName",
        rationale: str,
        graph: nx.DiGraph = None,
        pr_limit: int = 0,
        check_solvable=True,
    ):
        super().__init__(pr_limit, check_solvable=check_solvable)
        self.old_pkg = old_pkg
        self.new_pkg = new_pkg
        self.pattern = re.compile(r"\s*-\s*(%s)(\s+|$)" % old_pkg)
        self.packages = {old_pkg}
        self.rationale = rationale
        self.name = f"{old_pkg}-to-{new_pkg}"
        if graph is None:
            self.graph = nx.DiGraph()
        else:
            self.graph = graph

    def order(
        self,
        graph: nx.DiGraph,
        total_graph: nx.DiGraph,
    ) -> Sequence["PackageName"]:
        """Order to run migrations in

        Parameters
        ----------
        graph : nx.DiGraph
            The graph of migratable PRs

        Returns
        -------
        graph : nx.DiGraph
            The ordered graph.
        """
        return graph

    def filter(self, attrs: "AttrsTypedDict", not_bad_str_start: str = "") -> bool:
        requirements = attrs.get("requirements", {})
        rq = (
            requirements.get("build", set())
            | requirements.get("host", set())
            | requirements.get("run", set())
            | requirements.get("test", set())
        )
        return super().filter(attrs) or len(rq & self.packages) == 0

    def migrate(
        self, recipe_dir: str, attrs: "AttrsTypedDict", **kwargs: Any
    ) -> "MigrationUidTypedDict":
        with open(os.path.join(recipe_dir, "meta.yaml")) as f:
            raw = f.read()
        lines = raw.splitlines()
        n = False
        for i, line in enumerate(lines):
            m = self.pattern.match(line)
            if m is not None:
                lines[i] = lines[i].replace(m.group(1), self.new_pkg)
                n = True
        if not n:
            return False
        upd = "\n".join(lines) + "\n"
        with open(os.path.join(recipe_dir, "meta.yaml"), "w") as f:
            f.write(upd)
        self.set_build_number(os.path.join(recipe_dir, "meta.yaml"))
        return super().migrate(recipe_dir, attrs)

    def pr_body(self, feedstock_ctx: FeedstockContext) -> str:
        body = super().pr_body(feedstock_ctx)
        body = body.format(
            "I noticed that this recipe depends on `%s` instead of \n"
            "`%s`. %s \n"
            "This PR makes this change."
            "\n"
            "Notes and instructions for merging this PR:\n"
            "1. Make sure that the recipe can indeed only depend on `%s`. \n"
            "2. Please merge the PR only after the tests have passed. \n"
            "3. Feel free to push to the bot's branch to update this PR if "
            "needed. \n" % (self.old_pkg, self.new_pkg, self.rationale, self.new_pkg),
        )
        return body

    def commit_message(self, feedstock_ctx: FeedstockContext) -> str:
        return f"use {self.new_pkg} instead of {self.old_pkg}"

    def pr_title(self, feedstock_ctx: FeedstockContext) -> str:
        return f"Suggestion: depend on {self.new_pkg} instead of {self.old_pkg}"

    def remote_branch(self, feedstock_ctx: FeedstockContext) -> str:
        return f"{self.old_pkg}-to-{self.new_pkg}-migration-{self.migrator_version}"

    def migrator_uid(self, attrs: "AttrsTypedDict") -> "MigrationUidTypedDict":
        n = super().migrator_uid(attrs)
        n["name"] = self.name
        return n
