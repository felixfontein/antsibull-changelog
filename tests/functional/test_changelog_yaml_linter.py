# Author: Felix Fontein <felix@fontein.de>
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2020, Ansible Project

"""
Test changelog.yaml linting.
"""

from __future__ import annotations

import glob
import io
import json
import os.path
import re
import sys
from contextlib import redirect_stdout

import pytest

from antsibull_changelog import constants as C
from antsibull_changelog.cli import command_lint_changelog_yaml
from antsibull_changelog.lint import lint_changelog_yaml

# Collect files
PATTERNS = ["*.yml", "*.yaml"]
BASE_DIR = os.path.dirname(__file__)
GOOD_TESTS = []
BAD_TESTS = []


def load_tests():
    for pattern in PATTERNS:
        for filename in glob.glob(os.path.join(BASE_DIR, "good", pattern)):
            GOOD_TESTS.append(filename)
        for filename in glob.glob(os.path.join(BASE_DIR, "bad", pattern)):
            json_filename = os.path.splitext(filename)[0] + ".json"
            if os.path.exists(json_filename):
                BAD_TESTS.append((filename, json_filename))
            else:
                pytest.fail("Missing {0} for {1}".format(json_filename, filename))


load_tests()


class Args:
    def __init__(
        self, changelog_yaml_path=None, no_semantic_versioning=False, strict=False
    ):
        self.changelog_yaml_path = changelog_yaml_path
        self.no_semantic_versioning = no_semantic_versioning
        self.strict = strict


# Test good files
@pytest.mark.parametrize("yaml_filename", GOOD_TESTS)
def test_good_changelog_yaml_files(yaml_filename):
    # Run test directly against implementation
    errors = lint_changelog_yaml(yaml_filename)
    assert len(errors) == 0

    # Run test against CLI
    args = Args(changelog_yaml_path=yaml_filename)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = command_lint_changelog_yaml(args)
    stdout_lines = stdout.getvalue().splitlines()
    assert stdout_lines == []
    assert rc == C.RC_SUCCESS

    # Run test against CLI (strict checking)
    args = Args(changelog_yaml_path=yaml_filename, strict=True)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = command_lint_changelog_yaml(args)
    stdout_lines = stdout.getvalue().splitlines()
    assert stdout_lines == []
    assert rc == C.RC_SUCCESS


@pytest.mark.parametrize("yaml_filename, json_filename", BAD_TESTS)
def test_bad_changelog_yaml_files(yaml_filename, json_filename):
    # Run test directly against implementation
    errors = lint_changelog_yaml(yaml_filename)
    errors_strict = lint_changelog_yaml(yaml_filename, strict=True)
    assert len(errors_strict) > 0

    # Cut off path
    errors = [
        [error[1], error[2], error[3].replace(yaml_filename, "input.yaml")]
        for error in errors
    ]
    errors_strict = [
        [error[1], error[2], error[3].replace(yaml_filename, "input.yaml")]
        for error in errors_strict
    ]

    # Load expected errors
    with open(json_filename, "r") as json_f:
        data = json.load(json_f)

    if errors != data["errors"]:
        print(json.dumps(errors, indent=2))

    if errors_strict != data["errors_strict"]:
        print(json.dumps(errors_strict, indent=2))

    assert len(errors) == len(data["errors"])
    for error1, error2 in zip(errors, data["errors"]):
        assert error1[0:2] == error2[0:2]
        assert re.match(error2[2], error1[2], flags=re.DOTALL) is not None

    assert len(errors_strict) == len(data["errors_strict"])
    for error1, error2 in zip(errors_strict, data["errors_strict"]):
        assert error1[0:2] == error2[0:2]
        assert re.match(error2[2], error1[2], flags=re.DOTALL) is not None

    # Run test against CLI
    args = Args(changelog_yaml_path=yaml_filename)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = command_lint_changelog_yaml(args)
    stdout_lines = stdout.getvalue().splitlines()
    assert len(stdout_lines) == len(data["errors"])
    expected_lines = sorted(
        [
            "^input\\.yaml:%d:%d: %s" % (error[0], error[1], error[2])
            for error in data["errors"]
        ]
    )
    for line, expected in zip(stdout_lines, expected_lines):
        line = line.replace(yaml_filename, "input.yaml")
        assert re.match(expected, line, flags=re.DOTALL) is not None
    assert rc == C.RC_INVALID_FRAGMENT
