# Author: Matt Clay <matt@mystile.com>
# Author: Felix Fontein <felix@fontein.de>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or
# https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2020, Ansible Project

"""
Collect and store information on Ansible plugins and modules.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from typing import Any, Generator

import packaging.version
from antsibull_fileutils.copier import CollectionCopier, Copier, GitCopier
from antsibull_fileutils.vcs import detect_vcs
from antsibull_fileutils.yaml import load_yaml_file, store_yaml_file

from .ansible import (
    PLUGIN_EXCEPTIONS,
    get_ansible_release,
    get_documentable_objects,
    get_documentable_plugins,
)
from .config import ChangelogConfig, CollectionDetails, PathsConfig
from .logger import LOGGER


class PluginDescription:
    # pylint: disable=too-few-public-methods
    """
    Stores a description of one plugin.
    """

    category: str
    type: str
    name: str
    namespace: str | None
    description: str
    version_added: str | None

    def __init__(
        self,
        plugin_type: str,
        name: str,
        namespace: str | None,
        description: str,
        version_added: str | None,
        category: str = "plugin",
    ):
        # pylint: disable=too-many-arguments
        """
        Create a new plugin description.
        """
        self.category = category
        self.type = plugin_type
        self.name = name
        self.namespace = namespace
        self.description = description
        self.version_added = version_added

    @staticmethod
    def from_dict(
        data: dict[str, dict[str, dict[str, Any]]],
        category: str = "plugin",
        add_plugin_period: bool = False,
    ) -> list[PluginDescription]:
        """
        Return a list of ``PluginDescription`` objects from the given data.

        :arg data: A dictionary mapping plugin types to a dictionary of plugins.
        :return: A list of plugin descriptions.
        """
        plugins = []

        for plugin_type, plugin_data in data.items():
            for plugin_name, plugin_details in plugin_data.items():
                description = plugin_details["description"]
                if (
                    add_plugin_period
                    and description
                    and not description.endswith((".", ",", "!", "?"))
                ):
                    description += "."
                plugins.append(
                    PluginDescription(
                        plugin_type=plugin_type,
                        name=plugin_name,
                        namespace=plugin_details.get("namespace"),
                        description=description,
                        version_added=plugin_details["version_added"],
                        category=category,
                    )
                )

        return plugins


def extract_namespace(
    paths: PathsConfig, collection_name: str | None, filename: str
) -> str:
    """
    Given a filename of a module, will extract the module's namespace.
    """
    # Follow links
    filename = os.path.realpath(filename)
    # Determine relative path
    if collection_name:
        rel_to = os.path.join(paths.base_dir, "plugins", "modules")
    else:
        rel_to = os.path.join(paths.base_dir, "lib", "ansible", "modules")
    path = os.path.relpath(filename, rel_to)
    path = os.path.split(path)[0]
    # Extract namespace from relative path
    namespace_list: list[str] = []
    while True:
        (path, last), prev = os.path.split(path), path
        if path == prev:
            break
        if last not in ("", ".", ".."):
            namespace_list.insert(0, last)
    return ".".join(namespace_list)


def jsondoc_to_metadata(  # pylint: disable=too-many-arguments
    paths: PathsConfig,
    collection_name: str | None,
    plugin_type: str,
    name: str,
    data: dict[str, Any],
    category: str = "plugin",
    is_ansible_core_2_13: bool = False,
) -> dict[str, Any]:
    """
    Convert ``ansible-doc --json`` output to plugin metadata.

    :arg paths: Paths configuration
    :arg collection_name: The name of the collection, if appropriate
    :arg plugin_type: The plugin's or object's type
    :arg name: The plugin's name
    :arg data: The JSON for this plugin returned by ``ansible-doc --json``
    :arg category: Set to ``object`` for roles and playbooks
    :arg is_ansible_core_2_13: Set to ``True`` for ``--metadata-dump`` output
    """
    namespace: str | None = None
    if collection_name and name.startswith(collection_name + "."):
        name = name[len(collection_name) + 1 :]
    docs: dict = data.get("doc") or {}
    if category == "object" and plugin_type == "role":
        entrypoints: dict = data.get("entry_points") or {}
        if "main" in entrypoints:
            docs = entrypoints["main"]
    if category == "plugin" and plugin_type == "module":
        if is_ansible_core_2_13:
            last_dot = name.rfind(".")
            if last_dot >= 0:
                namespace = name[:last_dot]
                name = name[last_dot + 1 :]
            else:
                namespace = ""
        else:
            filename: str | None = docs.get("filename")
            if filename:
                namespace = extract_namespace(paths, collection_name, filename)
            if "." in name:
                # Flatmapping
                name = name[name.rfind(".") + 1 :]
    return {
        "description": docs.get("short_description"),
        "name": name,
        "namespace": namespace,
        "version_added": docs.get("version_added"),
    }


def get_plugins_path(
    paths: PathsConfig, plugin_type: str, category: str = "plugin"
) -> str:
    """
    Return path to plugins of a given type.
    """
    if paths.is_collection:
        if category == "object":
            return os.path.join(paths.base_dir, "{0}s".format(plugin_type))
        return os.path.join(
            paths.base_dir,
            "plugins",
            "modules" if plugin_type == "module" else plugin_type,
        )
    lib_ansible = os.path.join(paths.base_dir, "lib", "ansible")
    if plugin_type == "module":
        return os.path.join(lib_ansible, "modules")
    return os.path.join(lib_ansible, "plugins", plugin_type)


def list_plugins_walk(
    paths: PathsConfig,
    playbook_dir: str | None,  # pylint: disable=unused-argument
    plugin_type: str,
    collection_name: str | None,
) -> list[str]:
    """
    Find all plugins of a type in a collection, or in ansible-core/-base. Uses os.walk().

    This will also work with Ansible 2.9.

    :arg paths: Paths configuration
    :arg playbook_dir: Value for the ``--playbook-dir`` argument of ``ansible-doc``
    :arg plugin_type: The plugin type to consider
    :arg collection_name: The name of the collection, if appropriate.
    """
    plugin_source_path = get_plugins_path(paths, plugin_type)

    if not os.path.exists(plugin_source_path):
        return []

    plugin_source_path = os.path.realpath(plugin_source_path)

    result = set()
    for dirpath, _, filenames in os.walk(plugin_source_path):
        if plugin_type != "module" and dirpath != plugin_source_path:
            continue
        for filename in filenames:
            if filename == "__init__.py" or not filename.endswith(".py"):
                continue
            if not paths.is_collection and dirpath == plugin_source_path:
                # Skip files which are *not* plugins/modules, but live in these directories inside
                # ansible-core/-base.
                if (plugin_type, filename) in PLUGIN_EXCEPTIONS:
                    continue
            path = os.path.realpath(os.path.join(dirpath, filename))
            path = os.path.splitext(path)[0]
            relpath = os.path.relpath(path, plugin_source_path)
            if not paths.is_collection and os.sep in relpath:
                # When listing modules in ansible-core/-base, get rid of the namespace.
                relpath = os.path.basename(relpath)
            relname = relpath.replace(os.sep, ".")
            if collection_name:
                relname = "{0}.{1}".format(collection_name, relname)
            result.add(relname)

    return sorted(result)


def list_plugins_ansibledoc(
    paths: PathsConfig,
    playbook_dir: str | None,
    plugin_type: str,
    collection_name: str | None,
    category: str = "plugin",
) -> list[str]:
    """
    Find all plugins of a type in a collection, or in ansible-core/-base. Uses ansible-doc.

    Note that ansible-doc from Ansible 2.10 or newer is needed for this!

    :arg paths: Paths configuration
    :arg playbook_dir: Value for the ``--playbook-dir`` argument of ``ansible-doc``
    :arg plugin_type: The plugin type to consider
    :arg collection_name: The name of the collection, if appropriate.
    """
    plugin_source_path = get_plugins_path(paths, plugin_type, category)

    if not os.path.exists(plugin_source_path) or os.listdir(plugin_source_path) == []:
        return []

    command = [paths.ansible_doc_path, "--json", "-t", plugin_type, "--list"]
    if playbook_dir:
        command.extend(["--playbook-dir", playbook_dir])
    if collection_name:
        command.append(collection_name)
    output = subprocess.check_output(command)
    plugins_list = json.loads(output.decode("utf-8"))

    if not collection_name:
        # Filter out FQCNs
        plugins_list = {
            name: data
            for name, data in plugins_list.items()
            if "." not in name or name.startswith("ansible.builtin.")
        }
    else:
        # Filter out without / wrong FQCN
        plugins_list = {
            name: data
            for name, data in plugins_list.items()
            if name.startswith(collection_name + ".")
        }

    return sorted(plugins_list.keys())


def run_ansible_doc(
    paths: PathsConfig,
    playbook_dir: str | None,
    plugin_type: str,
    plugin_names: list[str],
) -> dict:
    """
    Runs ansible-doc to retrieve documentation for a given set of plugins in JSON format.

    Plugins must be in FQCN for collections.
    """
    command = [paths.ansible_doc_path, "--json", "-t", plugin_type]
    if playbook_dir:
        command.extend(["--playbook-dir", playbook_dir])
    command.extend(plugin_names)
    output = subprocess.check_output(command)
    return json.loads(output.decode("utf-8"))


def run_ansible_doc_metadata_dump(
    paths: PathsConfig, playbook_dir: str | None, collection_name: str | None
) -> dict:
    """
    Runs ansible-doc to retrieve documentation for all plugins in a collection.
    """
    command = [paths.ansible_doc_path, "--metadata-dump"]
    if collection_name:
        command.append(collection_name)
    if playbook_dir:
        command.extend(["--playbook-dir", playbook_dir])
    output = subprocess.check_output(command)
    return json.loads(output.decode("utf-8"))


def load_plugin_metadata(  # pylint: disable=too-many-arguments
    paths: PathsConfig,
    playbook_dir: str | None,
    plugin_type: str,
    collection_name: str | None,
    use_ansible_doc: bool = False,
    category: str = "plugin",
) -> dict[str, dict[str, Any]]:
    """
    Collect plugin metadata for all plugins of a given type.

    :arg paths: Paths configuration
    :arg playbook_dir: Value for the ``--playbook-dir`` argument of ``ansible-doc``
    :arg plugin_type: The plugin type or object type to consider
    :arg collection_name: The name of the collection, if appropriate
    :arg use_ansible_doc: Set to ``True`` to always use ansible-doc to enumerate plugins/modules
    :arg category: Set to ``object`` for roles and playbooks
    """
    if use_ansible_doc or category == "object":
        # WARNING: Do not make this the default to this before ansible-core/-base is a requirement!
        plugins_list = list_plugins_ansibledoc(
            paths, playbook_dir, plugin_type, collection_name, category
        )
    else:
        plugins_list = list_plugins_walk(
            paths, playbook_dir, plugin_type, collection_name
        )

    result: dict[str, dict[str, Any]] = {}
    if not plugins_list:
        return result

    plugins_data = run_ansible_doc(paths, playbook_dir, plugin_type, plugins_list)

    for name, data in plugins_data.items():
        processed_data = jsondoc_to_metadata(
            paths, collection_name, plugin_type, name, data, category=category
        )
        result[processed_data["name"]] = processed_data
    return result


def _load_plugins_2_13(
    plugins_data: dict[str, Any],
    paths: PathsConfig,
    collection_name: str,
    playbook_dir: str | None = None,
) -> None:
    LOGGER.debug("Run ansible-doc for {!r}", playbook_dir)
    data = run_ansible_doc_metadata_dump(paths, playbook_dir, collection_name)["all"]

    LOGGER.debug("Processing result")
    for category, category_types in (
        ("plugins", get_documentable_plugins()),
        ("objects", get_documentable_objects()),
    ):
        for plugin_type in category_types:
            if plugin_type in data:
                plugins_data[category][plugin_type] = {}
                for plugin_name, plugin_data in data[plugin_type].items():
                    if plugin_name.startswith("ansible.builtin._"):
                        plugin_name = plugin_name.replace("_", "", 1)
                    processed_data = jsondoc_to_metadata(
                        paths,
                        collection_name,
                        plugin_type,
                        plugin_name,
                        plugin_data,
                        category=category[:-1],
                        is_ansible_core_2_13=True,
                    )
                    plugins_data[category][plugin_type][
                        processed_data["name"]
                    ] = processed_data


@contextmanager
def collection_copier(
    paths: PathsConfig, config: ChangelogConfig, namespace: str, name: str
) -> Generator[tuple[str, PathsConfig], None, None]:
    """
    Creates a copy of a collection to a place where ``--playbook-dir`` can be used
    to prefer this copy of the collection over any installed ones.
    """
    vcs = config.vcs
    if vcs == "auto":
        vcs = detect_vcs(paths.base_dir, log_debug=LOGGER.debug)
    copier = {
        "none": Copier,
        "git": GitCopier,
    }[
        vcs
    ](log_debug=LOGGER.debug)

    with CollectionCopier(
        source_directory=paths.base_dir,
        namespace=namespace,
        name=name,
        copier=copier,
        log_debug=LOGGER.debug,
    ) as (root_dir, collection_dir):
        new_paths = PathsConfig.force_collection(
            collection_dir, ansible_doc_bin=paths.ansible_doc_path
        )
        yield root_dir, new_paths


def _load_collection_plugins_2_13(
    plugins_data: dict[str, Any],
    paths: PathsConfig,
    collection_details: CollectionDetails,
    config: ChangelogConfig,
) -> None:
    collection_name = "{}.{}".format(
        collection_details.get_namespace(), collection_details.get_name()
    )

    with collection_copier(
        paths, config, collection_details.get_namespace(), collection_details.get_name()
    ) as (playbook_dir, new_paths):
        _load_plugins_2_13(
            plugins_data, new_paths, collection_name, playbook_dir=playbook_dir
        )


def _load_collection_plugins(
    plugins_data: dict[str, Any],
    paths: PathsConfig,
    collection_details: CollectionDetails,
    config: ChangelogConfig,
    use_ansible_doc: bool,
) -> None:
    collection_name = "{}.{}".format(
        collection_details.get_namespace(), collection_details.get_name()
    )

    with collection_copier(
        paths, config, collection_details.get_namespace(), collection_details.get_name()
    ) as (playbook_dir, new_paths):
        for plugin_type in get_documentable_plugins():
            plugins_data["plugins"][plugin_type] = load_plugin_metadata(
                new_paths,
                playbook_dir,
                plugin_type,
                collection_name,
                use_ansible_doc=use_ansible_doc,
            )

        for object_type in get_documentable_objects():
            plugins_data["objects"][object_type] = load_plugin_metadata(
                new_paths,
                playbook_dir,
                object_type,
                collection_name,
                use_ansible_doc=use_ansible_doc,
                category="object",
            )


def _load_ansible_plugins(
    plugins_data: dict[str, Any], paths: PathsConfig, use_ansible_doc: bool
) -> None:
    for plugin_type in get_documentable_plugins():
        plugins_data["plugins"][plugin_type] = load_plugin_metadata(
            paths, None, plugin_type, None, use_ansible_doc=use_ansible_doc
        )


def _get_ansible_core_version(paths: PathsConfig) -> packaging.version.Version:
    try:
        version, _ = get_ansible_release()
        return packaging.version.Version(version)
    except ValueError:
        pass

    command = [paths.ansible_doc_path, "--version"]
    output = subprocess.check_output(command).decode("utf-8")
    for regex in (r"^ansible-doc \[(?:core|base) ([^\]]+)\]", r"^ansible-doc ([^\s]+)"):
        match = re.match(regex, output)
        if match:
            return packaging.version.Version(match.group(1))
    raise RuntimeError(
        f"Cannot extract ansible-core version from ansible-doc --version output:\n{output}"
    )


def _refresh_plugin_cache(
    paths: PathsConfig,
    collection_details: CollectionDetails,
    config: ChangelogConfig,
    version: str,
    use_ansible_doc: bool = False,
):
    LOGGER.info("refreshing plugin cache")

    plugins_data: dict[str, Any] = {
        "version": version,
        "plugins": {},
        "objects": {},
    }

    core_version = _get_ansible_core_version(paths)
    if core_version >= packaging.version.Version("2.13.0.dev0"):
        if paths.is_collection:
            _load_collection_plugins_2_13(
                plugins_data, paths, collection_details, config
            )
        else:
            _load_plugins_2_13(plugins_data, paths, "ansible.builtin")
    elif paths.is_collection:
        _load_collection_plugins(
            plugins_data, paths, collection_details, config, use_ansible_doc
        )
    else:
        _load_ansible_plugins(plugins_data, paths, use_ansible_doc)

    # remove empty namespaces from plugins
    for category in ("plugins", "objects"):
        for section in plugins_data[category].values():
            for plugin in section.values():
                if plugin["namespace"] is None:
                    del plugin["namespace"]

    return plugins_data


def load_plugins(  # pylint: disable=too-many-arguments
    paths: PathsConfig,
    collection_details: CollectionDetails,
    config: ChangelogConfig,
    version: str,
    force_reload: bool = False,
    use_ansible_doc: bool = False,
    add_plugin_period: bool = False,
) -> list[PluginDescription]:
    """
    Load plugins from ansible-doc.

    :arg paths: Paths configuration
    :arg collection_details: Collection details
    :arg version: The current version. Used for caching data
    :arg force_reload: Set to ``True`` to ignore potentially cached data
    :arg use_ansible_doc: Set to ``True`` to always use ansible-doc to enumerate plugins/modules
    :return: A list of all plugins
    """
    if paths.is_other_project:
        return []

    plugin_cache_path = os.path.join(paths.changelog_dir, ".plugin-cache.yaml")
    plugins_data: dict[str, Any] = {}

    if not force_reload and os.path.exists(plugin_cache_path):
        plugins_data = load_yaml_file(plugin_cache_path)
        if version != plugins_data["version"]:
            LOGGER.info(
                "version {} does not match plugin cache version {}",
                version,
                plugins_data["version"],
            )
            plugins_data = {}

    if not plugins_data:
        plugins_data = _refresh_plugin_cache(
            paths, collection_details, config, version, use_ansible_doc
        )
        store_yaml_file(plugin_cache_path, plugins_data)

    plugins = PluginDescription.from_dict(
        plugins_data["plugins"],
        add_plugin_period=add_plugin_period,
    )
    if "objects" in plugins_data:
        plugins.extend(
            PluginDescription.from_dict(
                plugins_data["objects"],
                category="object",
                add_plugin_period=add_plugin_period,
            )
        )

    return plugins
