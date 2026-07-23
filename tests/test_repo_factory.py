"""Tests for gate.tools.repo_factory — deterministic template rename (local CoS).

The load-bearing test is ``test_no_trace_of_old_name_survives``: after a rename, NO
occurrence of the template's kebab OR snake name may remain anywhere in the tree. That is
the determinism guarantee AND the drift detector — if a template ever refers to itself in
a form the variants miss, this fails and names the offending file, and we extend
``name_variants`` (see memory project_repo_factory_init). The fixture mirrors the real
placeholder sites found in the audit: go.mod module path, chart dir, values snake DB name,
staging hostname, image repo, an Angular title, plus a binary + node_modules to prove they
are skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

from gate.tools import repo_factory

OLD = 'leartech-go-service-template'
OLD_SNAKE = 'leartech_go_service_template'
NEW = 'hello-go'
NEW_SNAKE = 'hello_go'


def _build_fixture(root: Path) -> None:
    """Write a mini-template tree replicating the real placeholder sites."""
    (root / 'go.mod').write_text(f'module github.com/mikelear/{OLD}\n\ngo 1.26\n')
    (root / 'README.md').write_text(
        f"# {OLD}\n\nexport DATABASE_URL='postgres://localhost:5432/{OLD_SNAKE}?sslmode=disable'\n"
    )
    (root / 'cmd' / 'server').mkdir(parents=True)
    (root / 'cmd' / 'server' / 'main.go').write_text(
        f'package main\n\nimport "github.com/mikelear/{OLD}/internal/config"\n\n'
        f'// starting {OLD}\nfunc main() {{ _ = config.X }}\n'
    )
    chart = root / 'charts' / OLD
    chart.mkdir(parents=True)
    (chart / 'Chart.yaml').write_text(f'name: {OLD}\nhome: https://github.com/mikelear/{OLD}\n')
    (chart / 'values.yaml').write_text(
        f'nameOverride: {OLD}\ndb:\n  name: {OLD_SNAKE}\nimage:\n  repository: ghcr.io/mikelear/{OLD}\n'
    )
    (root / 'end2end').mkdir()
    (root / 'end2end' / 'smoke.sh').write_text(f'URL=https://{OLD}-jx-staging.jx.leartech.com/health\n')
    # Must be SKIPPED: a binary file (NUL byte) + a toolchain dir that mentions the name.
    (root / 'logo.png').write_bytes(b'\x89PNG\x00\x00' + OLD.encode() + b'\x00binary')
    nm = root / 'node_modules' / OLD
    nm.mkdir(parents=True)
    (nm / 'index.js').write_text(f'// vendored {OLD}\n')


def _walk_text(root: Path) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in repo_factory.SKIP_DIRS]
        for f in filenames:
            p = Path(dirpath) / f
            if repo_factory._is_text_file(p):
                out.append((p, p.read_text(encoding='utf-8', errors='surrogateescape')))
    return out


def test_no_trace_of_old_name_survives(tmp_path: Path) -> None:
    """THE drift detector: no old kebab/snake name anywhere in text files post-rename."""
    _build_fixture(tmp_path)
    repo_factory.rename_tree(tmp_path, OLD, NEW)

    offenders: list[str] = []
    for path, text in _walk_text(tmp_path):
        for i, line in enumerate(text.splitlines(), 1):
            if OLD in line or OLD_SNAKE in line:
                offenders.append(f'{path.relative_to(tmp_path)}:{i}: {line.strip()}')
    assert not offenders, 'old template name survived — extend name_variants:\n' + '\n'.join(offenders)


def test_module_path_and_snake_db_rewritten(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    repo_factory.rename_tree(tmp_path, OLD, NEW)
    assert (tmp_path / 'go.mod').read_text() == f'module github.com/mikelear/{NEW}\n\ngo 1.26\n'
    values = (tmp_path / 'charts' / NEW / 'values.yaml').read_text()
    assert f'ghcr.io/mikelear/{NEW}' in values
    assert f'name: {NEW_SNAKE}' in values  # snake DB variant, not the kebab


def test_chart_directory_renamed(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    report = repo_factory.rename_tree(tmp_path, OLD, NEW)
    assert (tmp_path / 'charts' / NEW).is_dir()
    assert not (tmp_path / 'charts' / OLD).exists()
    assert (str(Path('charts') / OLD), str(Path('charts') / NEW)) in report.dirs_renamed


def test_binary_and_vendored_dirs_untouched(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    repo_factory.rename_tree(tmp_path, OLD, NEW)
    # binary keeps the old bytes (skipped by the text sniff)
    assert OLD.encode() in (tmp_path / 'logo.png').read_bytes()
    # node_modules is skipped entirely — old name AND path survive
    assert (tmp_path / 'node_modules' / OLD / 'index.js').read_text() == f'// vendored {OLD}\n'


def test_report_counts_are_sane(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    report = repo_factory.rename_tree(tmp_path, OLD, NEW)
    assert report.occurrences > 0
    assert 'go.mod' in report.files_changed
    assert 'logo.png' not in report.files_changed  # binary never rewritten


def test_render_template_from_local_dir_strips_git_and_renames(tmp_path: Path) -> None:
    """render_template on a LOCAL dir copies (offline), drops .git, and renames — the testable
    core of the push/PR modes (the git/gh orchestration on top is cluster-proven)."""
    src = tmp_path / OLD
    src.mkdir()
    _build_fixture(src)
    (src / '.git').mkdir()
    (src / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')

    dest = tmp_path / 'out'
    report = repo_factory.render_template(str(src), NEW, dest)

    assert not (dest / '.git').exists()  # history never carried over
    assert (dest / 'go.mod').read_text() == f'module github.com/mikelear/{NEW}\n\ngo 1.26\n'
    assert (dest / 'charts' / NEW).is_dir()
    assert report.occurrences > 0
    # placeholder inferred from the local dir basename
    offenders = [str(p.relative_to(dest)) for p, t in _walk_text(dest) if OLD in t or OLD_SNAKE in t]
    assert not offenders, f'old name survived: {offenders}'


def test_scaffold_working_tree_overlays_and_preserves_git(tmp_path: Path) -> None:
    """scaffold_working_tree renders the template ONTO an existing clone, keeping its .git +
    pre-existing files (the README from create-repo) — the Plan's scaffold-step mechanism."""
    template = tmp_path / OLD
    template.mkdir()
    _build_fixture(template)

    # simulate the already-cloned target repo (README on main + .git)
    workdir = tmp_path / 'hello-go-clone'
    workdir.mkdir()
    (workdir / '.git').mkdir()
    (workdir / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    (workdir / 'README.md').write_text('# hello-go\n')

    repo_factory.scaffold_working_tree(str(template), NEW, workdir)

    assert (workdir / '.git' / 'HEAD').exists()  # clone identity preserved
    assert (workdir / 'go.mod').read_text() == f'module github.com/mikelear/{NEW}\n\ngo 1.26\n'
    assert (workdir / 'charts' / NEW).is_dir()
    # the render temp dir was cleaned up
    assert not (tmp_path / '.hello-go-clone-template-render').exists()
    offenders = [str(p.relative_to(workdir)) for p, t in _walk_text(workdir) if OLD in t or OLD_SNAKE in t]
    assert not offenders, f'old name survived: {offenders}'


def test_name_variants_snake_before_kebab() -> None:
    variants = repo_factory.name_variants(OLD, NEW)
    assert variants == [(OLD_SNAKE, NEW_SNAKE), (OLD, NEW)]


def test_resolve_template_language_key_and_slug() -> None:
    assert repo_factory.resolve_template('go') == ('mikelear/leartech-go-service-template', 'leartech-go-service-template')
    assert repo_factory.resolve_template('angular') == (
        'mikelear/leartech-angular-service-template',
        'leartech-angular-service-template',
    )
    assert repo_factory.resolve_template('leartech-go-service-template') == (
        'mikelear/leartech-go-service-template',
        'leartech-go-service-template',
    )
    assert repo_factory.resolve_template('acme/custom-template') == ('acme/custom-template', 'custom-template')
