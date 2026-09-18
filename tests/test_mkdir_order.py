"""Regression tests for Debian bug #443136: sitecopy must create
directories shallow-to-deep, so that the first synchronization of a
multi-level tree completes in a single run."""

import re
import xml.etree.ElementTree as ET

from common import run_sitecopy

# A four-level tree with a file at every level.
LEVELS = ["a", "a/b", "a/b/c", "a/b/c/d"]
LEVEL_FILES = {
    "a": "top.txt",
    "a/b": "mid.txt",
    "a/b/c": "deep.txt",
    "a/b/c/d": "leaf.txt",
}


def populate(base):
    for level in LEVELS:
        directory = base / level
        directory.mkdir(parents=True, exist_ok=True)
        (directory / LEVEL_FILES[level]).write_text(
            "contents of %s\n" % LEVEL_FILES[level])


def fetched_directory_order(stdout):
    order = []
    for line in stdout.splitlines():
        match = re.match(r"^Directory: (.+)/$", line)
        if match:
            order.append(match.group(1))
    return order


def reverse_storage_items(senv):
    """Rewrite the storage file with items reversed, as written by
    sitecopy releases which did not sort the state file: directories
    deepest-first.  Storage files carried forward from those releases
    keep that ordering."""
    storage = senv["store"] / "testsite"
    tree = ET.parse(str(storage))
    items = tree.getroot().find("items")
    items[:] = list(reversed(list(items)))
    tree.write(str(storage), encoding="ISO-8859-1", xml_declaration=True)


def test_fetch_lists_parent_directories_first(dav_site_env):
    populate(dav_site_env["remote"])
    res = run_sitecopy(dav_site_env, ["--fetch", "testsite"])
    assert res.returncode == 0

    order = fetched_directory_order(res.stdout)
    assert set(order) == set(LEVELS)
    assert sorted(order, key=lambda name: (name.count("/"), name)) == order


def test_synch_single_run_with_unsorted_storage(dav_site_env):
    populate(dav_site_env["remote"])
    res = run_sitecopy(dav_site_env, ["--fetch", "testsite"])
    assert res.returncode == 0

    reverse_storage_items(dav_site_env)

    res = run_sitecopy(dav_site_env, ["--synchronize", "testsite"])
    assert res.returncode == 0
    assert "Synchronize completed successfully" in res.stdout
    for level in LEVELS:
        assert (dav_site_env["local"] / level).is_dir()
        assert (dav_site_env["local"] / level / LEVEL_FILES[level]).is_file()


def test_update_creates_nested_directories_single_run(dav_site_env):
    populate(dav_site_env["local"])
    res = run_sitecopy(dav_site_env, ["--initialize", "testsite"])
    assert res.returncode == 0

    res = run_sitecopy(dav_site_env, ["--update", "testsite"])
    assert res.returncode == 0
    assert "Update completed successfully" in res.stdout
    for level in LEVELS:
        assert (dav_site_env["remote"] / level).is_dir()
        assert (dav_site_env["remote"] / level / LEVEL_FILES[level]).is_file()
