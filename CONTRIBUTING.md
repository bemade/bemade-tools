# Contributing

## Merge policy: merge commits only

Pull requests into a version branch (`19.0`, `18.0`, …) are merged with a
**merge commit**. Squash and rebase merges are disabled in the repository
settings. This is deliberate; please do not ask for it to be changed on a
per-PR basis.

### Why

Squash (and rebase) merges rewrite the branch's commits, so the branch tip is
never an ancestor of the target. That makes the most basic questions about
history unanswerable:

- **"Has this branch landed?"** — `git branch -r --no-merged origin/19.0`
  lists squash-merged branches forever. `git cherry` fails too: the squash
  lands on a different base, the diff context changes, and the patch-id
  differs even for a single-commit PR. Content diffs decay with target drift.
  When this was measured (2026-09), only 1 of 12 feature branches could be
  proven landed; two branches known to be merged read as unmerged by every
  automated test.
- **"Revert this PR."** — With a merge commit, `git revert -m 1 <merge>`
  backs out the whole PR in one step.
- **"Which PR broke it?"** — The merge commit is the fencepost where CI
  passed. `git bisect --first-parent` walks fenceposts only, so CI atomicity
  is preserved without squashing.

### What it costs, and how to live with it

- Mainline has roughly 2.5× as many commits. For a PR-level view use
  `git log --first-parent`.
- Cherry-picking a merged PR onto another branch needs `-m 1`:
  `git cherry-pick -m 1 <merge>`.
- The merge commit's subject is the PR title and its body is the PR
  description (repo settings `merge_commit_title: PR_TITLE`,
  `merge_commit_message: PR_BODY`), so write both as you would a commit
  message: a conventional-commit style title, and a body that explains why.

### Version branches are independent

`15.0` … `19.0` share no common ancestor. Nothing propagates between them by
any git operation, so a fix — or a document like this one — must be applied
to each version branch separately.

### Other repositories

This policy is specific to the Odoo addon repositories (`bemade-addons`,
`bemade-tools`). Tooling repositories released with release-please
(`odoo-dev`, `odoo-operator`) are **squash-only**, because merge commits
duplicate changelog entries there. Different repos, different question
asked of their history.
