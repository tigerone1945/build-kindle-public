"""依存パッケージの宣言と、コードでの使用が一致すること（NFR-007、Design 第9章、FINDING-043）。

`src/` が直接 import する外部パッケージは、推移的な依存に頼らず、`pyproject.toml` の `dependencies` に宣言する。
宣言のない依存に頼ると、`uv sync` は通っても、依存元のパッケージの変更で、import が失敗しうる。
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SOURCE_FILES = sorted((REPO / "src" / "triage_agent").glob("*.py"))

# import する名前 → 配布パッケージ名（PyPI 上の名前）。新しい外部パッケージを import するときは、ここへ加える。
DISTRIBUTIONS = {
    "agents": "openai-agents",
    "openai": "openai",
    "pydantic": "pydantic",
    "yaml": "pyyaml",
}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependencies() -> set[str]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    # "openai>=3.16.2" → "openai"（名前だけを取り出す）
    return {normalize(re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0]) for requirement in project["dependencies"]}


def third_party_imports() -> set[str]:
    roots: set[str] = set()
    for path in SOURCE_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                roots.add(node.module.split(".")[0])
    return {root for root in roots if root not in sys.stdlib_module_names and root != "triage_agent"}


def test_the_scan_finds_the_third_party_imports() -> None:
    # 検査が空振りでないこと。
    assert {"agents", "openai", "pydantic", "yaml"} <= third_party_imports()
    assert {"openai", "openai-agents"} <= declared_dependencies()


@pytest.mark.parametrize("root", sorted(third_party_imports()))
def test_every_imported_package_is_declared_as_a_dependency(root: str) -> None:
    assert root in DISTRIBUTIONS, f"{root}: 配布パッケージ名が、このテストの対応表にない"
    assert normalize(DISTRIBUTIONS[root]) in declared_dependencies(), f"{root}: pyproject.toml に宣言がない"


def test_the_mapping_has_no_unused_entry() -> None:
    assert set(DISTRIBUTIONS) == third_party_imports()
