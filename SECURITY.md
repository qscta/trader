# Security policy

## Secrets

Never commit OKX API keys, passphrases, DingTalk webhooks, `.env` files, runtime
state, logs, or deployment backups. CI runs a repository-hygiene regression test
for these artifact classes; `.gitignore` is only the first line of defense.

Environment variables are preferred for credentials. Runtime persistence must
redact the in-memory environment overrides before writing configuration to disk,
and credential/state files must be owner-readable only (`0600`).
`FLASK_SECRET_KEY` must be a random value of at least 32 bytes; generate one with
`python3 -c 'import secrets; print(secrets.token_hex(32))'` and keep it only in
the deployment secret store or environment.

## If a secret enters Git history

Treat it as compromised even after deleting the current file:

1. Revoke and recreate the OKX API key and DingTalk webhook first.
2. Preserve an offline incident record without copying secret values into issues,
   logs, or chat.
3. Rewrite every affected branch and tag with `git filter-repo`, removing the
   credential file and backup archives.
4. Force-push the rewritten refs only during a coordinated maintenance window.
5. Ask every clone owner to re-clone; old clones still contain the objects.

History rewriting cannot revoke a credential and therefore never replaces step 1.

The current worktree passing the hygiene test does **not** prove that Git history is
clean. Early repository history contained runtime artifacts. Before any history
rewrite, revoke and rotate every credential that could have appeared there; then
audit all branches and tags, coordinate the rewrite, and invalidate old clones.
This repository does not claim that those external rotation/rewrite steps have
already been completed.

## Reporting

Do not open a public issue containing credentials or exploitable account details.
Contact the repository owner privately and include only the minimum reproduction
needed to locate the defect.
