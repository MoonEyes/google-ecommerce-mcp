# Releasing (maintainers)

A checklist. Versions **0.4.0** (paid media connectors, `doctor`, guided setup, client report) and **0.4.1** (fixes found by the end-to-end tests) followed it: the version bump and the installer pins are in their own commit, the README pins that commit, and the tag is pushed only after the release pull request is merged.

The previous version was `0.4.1`. Version `0.5.0` (multi-client, controlled writing) is prepared on the branch `feat/fullcontrol`: its documentation, `pyproject.toml`, `server.json` and `__init__.py` already say `0.5.0`; the installers and the README pins still name `0.4.1` until step 3 to 5 below are done.

## Checklist

1. **Branch merged.** Tests green on `main` (`pytest -q`, offline).
2. **Bump the version in the three places**, all to the same value:
   - `pyproject.toml` (`version`)
   - `server.json` (`version` **and** `packages[0].version`)
   - `src/google_ecommerce_mcp/__init__.py` (`__version__`)
3. **Bump the two installers that pin the package version:** `install.ps1` and `install.sh` (`google-ecommerce-mcp==X.Y.Z`).
4. **Commit the version bump and the installers first**, then note that commit's full hash. The README URLs pin an exact commit, and a commit cannot contain its own hash, so this is done in two commits.
5. **Refresh the README:** replace the commit hash in the four pinned installer URLs with the hash from step 4, update the version quoted in "Quick install", and replace the SHA-256 of `install.ps1` and `install.sh` with the new values (hash of the files with LF line endings; `tests/test_installer_scripts.py` computes it the same way). Do not edit these by hand without running the test.
6. **Update `CHANGELOG.md`:** rename `Unreleased` to the version and date, and start a new empty `Unreleased`.
7. **Run the tests:** `pytest -q`. `tests/test_installer_scripts.py` **fails until** the versions, the pinned commit URLs and the SHA-256 agree: that is the safety net, so treat a failure as "something is not in step", not as a test to adjust.
8. **Commit and push** the README and changelog, open the release pull request and wait for it to be merged. Then tag the merge commit on `main`: `git tag vX.Y.Z && git push origin vX.Y.Z`.
9. **The release workflow** (`.github/workflows/release.yml`) checks that the tag, `pyproject.toml` and `server.json` agree, runs the tests, builds, publishes to **PyPI** (trusted publishing, no token) and to the **official MCP Registry** (GitHub OIDC, no token). Watch it finish.
10. **Create the GitHub release** from the tag with the changelog entry as notes.
11. **Verify:** `uvx google-ecommerce-mcp==X.Y.Z check` on a clean machine, and the README one-liners against the pinned commit.

## Things to remember

- The uv installer that the scripts call is not pinned (documented limitation).
- The server entry written into Claude Desktop runs `uvx google-ecommerce-mcp` (latest) on each start.
- The paid media connectors and **every write path** are tested offline only; say so in the release notes unless that has changed.
- `tests/test_docs_fullcontrol.py` checks that the version is identical in `pyproject.toml`, `server.json` (twice) and `__init__.py`.
