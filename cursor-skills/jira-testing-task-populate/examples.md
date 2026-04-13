# Anonymized layout snippets

These illustrate **structure only** — not real proprietary text.

## Example A — standard blocks (CLI / feature test)

```text
*Test Steps:*
# Configure <feature> on <interface>. 
# Verify <show-command> reflects expected state.
# Tear down and confirm removal.

*Pass Criteria:*
* No unexpected commit or runtime errors.
* <Observable> matches expected values.

*Commands legend:*
{noformat}
configure
  <path-from-RST> <placeholder> ...
{noformat}

*Show commands legend:*
{noformat}
show <path> | no-more
{noformat}

*Variants:*
* Different <dimension A>.
* Different <dimension B>.

*Negative Testing Flows:*
* Invalid / duplicate configuration.
* Missing dependency (expect clear error).

*Test Results:*


```

## Example B — description + Test Steps (valid/invalid / conditions)

**Description** (short):

```text
Conditions that should yield <bad-state>:
# First condition ...
# Second condition ...
```

**Test Steps field** — same header order as Example A; steps often map **one numbered condition per scenario**, with Pass Criteria listing **per-condition** expected behavior.

## Example B2 — Commands legend after negative flows

```text
*Test Steps:*
# ...

*Pass Criteria:*
* ...

*Show commands legend:*
{noformat}
show ...
{noformat}

*Variants:*
* ...

*Negative Testing Flows:*
* ...

*Commands legend:*
{noformat}
services ...
{noformat}

*Test Results:*


```
