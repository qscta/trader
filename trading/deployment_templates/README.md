# Deployment input schemas

Only three host-specific, credential-free inputs are accepted.  All executable
code comes from the reviewed release.

## `remove-one-confirmed-config-key.spec.json`

Generate this file with the tracked tool; never hand-calculate its hashes:

```text
/usr/bin/python3 -I -B trading/remove-one-confirmed-config-key.py \
  --generate-spec --release-sha <40-hex-release-sha> \
  --config <canonical-private-config.json> \
  --key <parent-key> --key <confirmed-obsolete-leaf-key> \
  --reason '<single-line review decision>' \
  --output <protected-input-dir>/remove-one-confirmed-config-key.spec.json
```

The tool reads but never rewrites the source config in this mode.  The output
contains only the JSON path and SHA-256 commitments to the complete before
file, removed value, and deterministic after file.  Deployment refuses a
single-byte preimage change.

## `writer-inventory.json`

Copy `writer-inventory.template.json`, replace every `__...__` placeholder,
and list concrete people, UI surfaces, host processes/units/containers, API
credentials, and credential consumers.  Do not put credential values in this
file. Validate it with:

```text
/usr/bin/python3 -I -B trading/deployment_evidence.py \
  validate-writer-inventory --file <protected-input-dir>/writer-inventory.json \
  --release-sha <40-hex-release-sha>
```

## `backup-script.patch`

Take a root-readable snapshot of `/usr/local/sbin/trading-state-backup`, make
the minimum reviewed edit in a separate copy, and generate a labelled unified
diff.  Verify it against a fresh original with `patch --batch --fuzz=0`, then
run `bash -n` on the patched result.  The patch must contain no credential,
host token, private endpoint, or runtime state.  `prepare_deployment.py`
repeats exact application and syntax validation before publishing the stage.
