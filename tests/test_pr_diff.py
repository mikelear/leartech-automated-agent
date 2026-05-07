"""Unit tests for gate.tools.pr_diff helpers."""

from __future__ import annotations

from gate.tools.pr_diff import added_files, added_lines

DIFF_SAMPLE = """diff --git a/src/app/home.ts b/src/app/home.ts
index 1234567..89abcde 100644
--- a/src/app/home.ts
+++ b/src/app/home.ts
@@ -1,3 +1,4 @@
 export class HomeComponent {
   title = 'home';
+  greeting = 'hello world';
 }
diff --git a/src/app/home.spec.ts b/src/app/home.spec.ts
new file mode 100644
index 0000000..fedcba9
--- /dev/null
+++ b/src/app/home.spec.ts
@@ -0,0 +1,3 @@
+describe('HomeComponent', () => {
+  it('renders', () => {});
+});
"""


def test_added_lines_excludes_diff_headers() -> None:
    lines = added_lines(DIFF_SAMPLE)
    # Should NOT include +++ header lines.
    assert all(not line.startswith('++ ') for line in lines)
    # Should include actual added content.
    assert "  greeting = 'hello world';" in lines
    assert "describe('HomeComponent', () => {" in lines


def test_added_files_returns_all_modified_paths() -> None:
    files = added_files(DIFF_SAMPLE)
    assert 'src/app/home.ts' in files
    assert 'src/app/home.spec.ts' in files


def test_added_files_filters_by_pattern() -> None:
    spec_files = added_files(DIFF_SAMPLE, pattern='.spec.ts')
    assert spec_files == ['src/app/home.spec.ts']

    ts_files = added_files(DIFF_SAMPLE, pattern='.ts')
    # `.ts` matches both home.ts and home.spec.ts (suffix match, both end in `.ts`).
    assert sorted(ts_files) == ['src/app/home.spec.ts', 'src/app/home.ts']
