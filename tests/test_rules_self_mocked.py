"""tests.self_mocked (SPEC 5.8): a new or changed test patches what its assertions call."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import Repo, init_repo, static_findings

RULE = "tests.self_mocked"
PATH = "tests/test_math.py"
BASE = """\
from mathx import total


def test_total():
    assert total([1, 2]) == 3
"""


def project(tmp_path: Path, base: str = BASE, path: str = PATH) -> Repo:
    repo = init_repo(tmp_path / "repo")
    repo.write("src/mathx.py", "def total(xs):\n    return sum(xs)\n")
    repo.write(path, base)
    repo.commit("base")
    return repo


def fired(tmp_path: Path, candidate: str, base: str = BASE, path: str = PATH) -> list:
    repo = project(tmp_path, base, path)
    repo.write(path, candidate)
    return [f for f in static_findings(repo) if f.rule == RULE]


# ---- Python: every patch form fires ------------------------------------------------------

FORMS = {
    "monkeypatch object form": (
        "import mathx\nfrom mathx import total\n\n\n"
        "def test_total(monkeypatch):\n"
        '    monkeypatch.setattr(mathx, "total", lambda xs: 3)\n'
        "    assert total([1, 2]) == 3\n"
    ),
    "monkeypatch string form": (
        "from mathx import total\n\n\n"
        "def test_total(monkeypatch):\n"
        '    monkeypatch.setattr("mathx.total", lambda xs: 3)\n'
        "    assert total([1, 2]) == 3\n"
    ),
    "mock.patch context manager": (
        "import mathx\nfrom unittest import mock\n\n\n"
        "def test_total():\n"
        '    with mock.patch("mathx.total", return_value=3):\n'
        "        assert mathx.total([1, 2]) == 3\n"
    ),
    "patch decorator": (
        "import mathx\nfrom unittest.mock import patch\n\n\n"
        '@patch("mathx.total", return_value=3)\n'
        "def test_total(mocked):\n"
        "    assert mathx.total([1, 2]) == 3\n"
    ),
    "patch call": (
        "import mathx\nfrom unittest.mock import patch\n\n\n"
        "def test_total():\n"
        '    patch("mathx.total", return_value=3).start()\n'
        "    assert mathx.total([1, 2]) == 3\n"
    ),
    "mocker.patch": (
        "import mathx\n\n\n"
        "def test_total(mocker):\n"
        '    mocker.patch("mathx.total", return_value=3)\n'
        "    assert mathx.total([1, 2]) == 3\n"
    ),
    "patch.object": (
        "import mathx\nfrom unittest.mock import patch\n\n\n"
        "def test_total():\n"
        '    with patch.object(mathx, "total", return_value=3):\n'
        "        assert mathx.total([1, 2]) == 3\n"
    ),
    "mocker.patch.object": (
        "import mathx\n\n\n"
        "def test_total(mocker):\n"
        '    mocker.patch.object(mathx, "total", return_value=3)\n'
        "    assert mathx.total([1, 2]) == 3\n"
    ),
    "module alias patched, from-imported name asserted": (
        "import mathx as mx\nfrom mathx import total\n\n\n"
        "def test_total(monkeypatch):\n"
        '    monkeypatch.setattr(mx, "total", lambda xs: 3)\n'
        "    assert total([1, 2]) == 3\n"
    ),
    "module alias on both sides": (
        "import mathx as mx\n\n\n"
        "def test_total(monkeypatch):\n"
        '    monkeypatch.setattr(mx, "total", lambda xs: 3)\n'
        "    assert mx.total([1, 2]) == 3\n"
    ),
    "unittest assertion method": (
        "import mathx\nfrom unittest import TestCase, mock\n\n\n"
        "class TestTotal(TestCase):\n"
        "    def test_total(self):\n"
        '        with mock.patch("mathx.total", return_value=3):\n'
        "            self.assertEqual(mathx.total([1, 2]), 3)\n"
    ),
    "relative from-import": (
        "from .mathx import total\n\n\n"
        "def test_total(monkeypatch):\n"
        '    monkeypatch.setattr("pkg.mathx.total", lambda xs: 3)\n'
        "    assert total([1, 2]) == 3\n"
    ),
}


@pytest.mark.parametrize("form", FORMS)
def test_each_patch_form_fires_on_a_changed_test(tmp_path: Path, form: str) -> None:
    found = fired(tmp_path, FORMS[form])
    assert len(found) == 1, form
    assert found[0].severity == "warn"
    assert found[0].file == PATH


def test_message_names_the_test_the_target_and_the_call(tmp_path: Path) -> None:
    (finding,) = fired(tmp_path, FORMS["monkeypatch object form"])
    assert finding.message == (
        "test test_total patches mathx.total and asserts on total(...); it tests the mock"
    )
    assert finding.line == 6


def test_a_new_test_that_patches_its_subject_fires(tmp_path: Path) -> None:
    candidate = BASE + (
        "\n\ndef test_total_again(monkeypatch):\n"
        '    monkeypatch.setattr("mathx.total", lambda xs: 9)\n'
        "    assert total([4, 5]) == 9\n"
    )
    found = fired(tmp_path, candidate)
    assert [f.message.split()[1] for f in found] == ["test_total_again"]


def test_a_new_test_file_fires(tmp_path: Path) -> None:
    repo = project(tmp_path)
    repo.write("tests/test_new.py", FORMS["monkeypatch object form"])
    assert [f.rule for f in static_findings(repo)] == [RULE]


# ---- Python: legitimate changes stay quiet -----------------------------------------------

QUIET = {
    "a collaborator is patched": (
        "import requests\nfrom mathx import total\n\n\n"
        "def test_total(monkeypatch):\n"
        '    monkeypatch.setattr(requests, "get", lambda url: None)\n'
        "    assert total([1, 2]) == 3\n"
    ),
    "another function of the module is patched": (
        "from unittest import mock\n\nfrom mathx import total\n\n\n"
        "def test_total():\n"
        '    with mock.patch("mathx.helper", return_value=1):\n'
        "        assert total([1, 2]) == 3\n"
    ),
    "the patched name is only asserted on as a mock": (
        "import mathx\nfrom unittest import mock\n\n\n"
        "def test_total():\n"
        '    with mock.patch("mathx.total", return_value=3) as fake:\n'
        "        fake([1, 2])\n"
        "        fake.assert_called_once_with([1, 2])\n"
    ),
    "the patched callable is called outside the assertion": (
        "import mathx\nfrom unittest import mock\n\n\n"
        "def test_total():\n"
        '    with mock.patch("mathx.total", return_value=3):\n'
        "        value = mathx.total([1, 2])\n"
        "    assert value == 3\n"
    ),
    "same name in another module": (
        "import other\nfrom unittest import mock\n\n\n"
        "def test_total():\n"
        '    with mock.patch("mathx.total", return_value=3):\n'
        "        assert other.total([1, 2]) == 3\n"
    ),
    "patch.object on another object": (
        "import mathx\nfrom unittest.mock import patch\n\n\n"
        "def test_total():\n"
        '    with patch.object(mathx, "helper"):\n'
        "        assert mathx.total([1, 2]) == 3\n"
    ),
}


@pytest.mark.parametrize("case", QUIET)
def test_legitimate_patching_does_not_fire(tmp_path: Path, case: str) -> None:
    assert fired(tmp_path, QUIET[case]) == [], case


def test_a_patch_already_in_the_same_test_at_base_does_not_fire(tmp_path: Path) -> None:
    base = FORMS["monkeypatch object form"]
    candidate = base.replace("== 3\n", "== 3\n    assert total([]) == 3\n")
    assert fired(tmp_path, candidate, base=base) == []


def test_the_same_patch_in_another_test_at_base_does_not_excuse_a_new_one(
    tmp_path: Path,
) -> None:
    base = FORMS["monkeypatch object form"]
    candidate = base + (
        "\n\ndef test_other(monkeypatch):\n"
        '    monkeypatch.setattr(mathx, "total", lambda xs: 3)\n'
        "    assert total([1]) == 3\n"
    )
    assert [f.message.split()[1] for f in fired(tmp_path, candidate, base=base)] == ["test_other"]


def test_an_unchanged_test_is_not_looked_at(tmp_path: Path) -> None:
    base = FORMS["monkeypatch object form"]
    candidate = base + "\n\ndef test_extra():\n    assert total([2]) == 2\n"
    assert fired(tmp_path, candidate, base=base) == []


def test_a_reformatted_patching_test_that_existed_at_base_does_not_fire(tmp_path: Path) -> None:
    base = FORMS["mock.patch context manager"]
    assert fired(tmp_path, base.replace("[1, 2]", "[1,2]"), base=base) == []


def test_non_test_files_are_ignored(tmp_path: Path) -> None:
    repo = project(tmp_path)
    repo.write("helpers.py", FORMS["monkeypatch object form"])
    assert [f for f in static_findings(repo) if f.rule == RULE] == []


# ---- JS / TS ------------------------------------------------------------------------------

JS_PATH = "src/math.test.ts"
JS_BASE = (
    "import { add } from './math';\n\ntest('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n"
)


def js_fired(tmp_path: Path, candidate: str, base: str = JS_BASE) -> list:
    return fired(tmp_path, candidate, base=base, path=JS_PATH)


JS_FORMS = {
    "jest.mock": (
        "import { add } from './math';\n\njest.mock('./math');\n\n"
        "test('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n"
    ),
    "vi.mock": (
        "import { add } from './math';\n\nvi.mock('./math', () => ({ add: () => 3 }));\n\n"
        "test('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n"
    ),
    "default import and require": (
        "const math = require('./math');\n\njest.mock('./math');\n\n"
        "test('adds', () => {\n  expect(math.add(1, 2)).toBe(3);\n});\n"
    ),
    "spyOn mockReturnValue": (
        "import * as math from './math';\n\n"
        "test('adds', () => {\n"
        "  jest.spyOn(math, 'add').mockReturnValue(3);\n"
        "  expect(math.add(1, 2)).toBe(3);\n});\n"
    ),
    "vi.spyOn mockResolvedValue": (
        "import * as math from './math';\n\n"
        "test('adds', async () => {\n"
        "  vi.spyOn(math, 'add').mockResolvedValue(3);\n"
        "  expect(await math.add(1, 2)).toBe(3);\n});\n"
    ),
    "spyOn mockImplementation through a variable": (
        "import * as math from './math';\n\n"
        "test('adds', () => {\n"
        "  const spy = jest.spyOn(math, 'add');\n"
        "  spy.mockImplementation(() => 3);\n"
        "  expect(math.add(1, 2)).toBe(3);\n});\n"
    ),
    "spyOn on a named import": (
        "import { add } from './math';\nimport * as math from './math';\n\n"
        "test('adds', () => {\n"
        "  jest.spyOn(math, 'add').mockImplementation(() => 3);\n"
        "  expect(add(1, 2)).toBe(3);\n});\n"
    ),
    "expect over several lines": (
        "import { add } from './math';\n\njest.mock('./math');\n\n"
        "test('adds', () => {\n  expect(\n    add(1, 2)\n  ).toBe(3);\n});\n"
    ),
}


@pytest.mark.parametrize("form", JS_FORMS)
def test_each_js_form_fires(tmp_path: Path, form: str) -> None:
    found = js_fired(tmp_path, JS_FORMS[form])
    assert len(found) == 1, form
    assert found[0].message.startswith("test adds patches ")
    assert found[0].message.endswith("; it tests the mock")


def test_js_message_names_the_module_and_the_imported_name(tmp_path: Path) -> None:
    (finding,) = js_fired(tmp_path, JS_FORMS["jest.mock"])
    assert finding.message == "test adds patches ./math and asserts on add; it tests the mock"
    assert finding.line == 3


JS_QUIET = {
    "another module is mocked": (
        "import { add } from './math';\n\njest.mock('./api');\n\n"
        "test('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n"
    ),
    "the mocked module's names are not asserted": (
        "import { add } from './math';\nimport { fetchRate } from './api';\n\n"
        "jest.mock('./api');\n\n"
        "test('adds', () => {\n  fetchRate.mockReturnValue(1);\n  expect(add(1, 2)).toBe(3);\n});\n"
    ),
    "a spy that is not given a return value": (
        "import * as math from './math';\n\n"
        "test('adds', () => {\n"
        "  const spy = jest.spyOn(math, 'add');\n"
        "  expect(math.add(1, 2)).toBe(3);\n  expect(spy).toHaveBeenCalled();\n});\n"
    ),
    "a spy on an object the file does not import": (
        "import { add } from './math';\n\n"
        "test('adds', () => {\n"
        "  jest.spyOn(console, 'log').mockImplementation(() => {});\n"
        "  expect(add(1, 2)).toBe(3);\n});\n"
    ),
    "a spy on another function of the module": (
        "import * as math from './math';\n\n"
        "test('adds', () => {\n"
        "  jest.spyOn(math, 'rate').mockReturnValue(1);\n"
        "  expect(math.add(1, 2)).toBe(3);\n});\n"
    ),
}


@pytest.mark.parametrize("case", JS_QUIET)
def test_legitimate_js_mocking_does_not_fire(tmp_path: Path, case: str) -> None:
    assert js_fired(tmp_path, JS_QUIET[case]) == [], case


def test_a_js_mock_already_at_base_does_not_fire(tmp_path: Path) -> None:
    base = JS_FORMS["jest.mock"]
    candidate = base.replace("toBe(3)", "toBe(3);\n  expect(add(2, 2)).toBe(4)")
    assert js_fired(tmp_path, candidate, base=base) == []


def test_a_js_mock_in_another_test_does_not_taint_a_new_one(tmp_path: Path) -> None:
    candidate = (
        "import * as math from './math';\n\n"
        "test('mocked', () => {\n"
        "  jest.spyOn(math, 'add').mockReturnValue(3);\n"
        "  expect(math.add(1, 2)).toBe(3);\n});\n\n"
        "test('real', () => {\n  expect(math.add(2, 2)).toBe(4);\n});\n"
    )
    found = js_fired(tmp_path, candidate, base="")
    assert [f.message.split()[1] for f in found] == ["mocked"]
