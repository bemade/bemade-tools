# Repository rules

- **Merge commits only.** PRs into version branches are merged with
  `gh pr merge --merge` (never `--squash` or `--rebase`). Squash and rebase
  are disabled in the repository settings. See `CONTRIBUTING.md` for the
  reasoning; do not argue for squash to "keep history clean".
- Write the PR title as a conventional-commit subject and the PR body as a
  commit body: they become the merge commit's subject and body.
- To see history at PR granularity: `git log --first-parent`. To revert or
  cherry-pick a PR: `git revert -m 1` / `git cherry-pick -m 1`.
- Version branches (`19.0`, `18.0`, …) are orphans with no common ancestor.
  A change needed on several versions is a separate PR per version branch.
