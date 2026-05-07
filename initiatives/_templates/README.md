# Initiative templates

Reusable initiative shapes — copy one, adjust the specific bits, save under `initiatives/`. Each template is a runnable YAML in its own right (with placeholder values), so you can also use `--repo-root` to dry-run them against a fixture.

Any initiative file is just a YAML matching the `Initiative` schema in
`gate/initiatives/loader.py`:

```
name: <kebab-case-id>
description: <human-facing context>
repo: <repo-name-or-owner/name>
branch: <branch-name>
base: <branch-to-fork-from>
goal: |
  <free-form goal — constraints belong here verbatim>
gate_marks: [<unit|integration|e2e|playwright|...>]
max_iterations: 5
```

## Templates

| Template | Use when |
|---|---|
| `component-spec.yaml` | Adding a Karma/Jasmine spec for an Angular component (auth-ui pattern from PR #37) |
| `dep-bump.yaml` | Bumping a single npm/Go/Python dep with explicit constraints |
| `refactor.yaml` | Mechanical refactor (e.g. NgModule → standalone, constructor DI → `inject()`) |

## Conventions

- **Branch names**: `agent/<short-slug>` so they're filterable from human work
- **Commit messages**: conventional commits (`test(home):`, `refactor(auth-ui):`, `chore(deps):`); always cite the failing criterion in the body if you're responding to one
- **`gate_marks`**: scope the initial gate run to the tier you're changing; the full gate runs anyway via the catalog's PR pipeline
- **`max_iterations`**: 5 is the default ceiling — bump higher only for deliberately complex initiatives

## Worked examples in `initiatives/`

| File | Status | What it did |
|---|---|---|
| `auth-ui-home-component-spec.yaml` | Shipped → PR #37 | First worked example. 46 turns, $1.38, full success including emergent chatops recovery. |
| `auth-ui-login-component-spec.yaml` | Pending | Direct comparison run — same shape as #37, calibrations from lessons catalog should make iteration faster. |
