# v2 Canonical JSON and Fingerprint Contract

Status: Phase A persistence contract

## Purpose

`canonical_json()` is used anywhere Manga Autopilot needs deterministic JSON bytes for persistent hashes, fingerprints, or canonical state snapshots. The representation must therefore be defined by the application rather than delegated to runtime-specific float formatting.

## Contract version

- Canonical JSON contract: `v2` (`CANONICAL_JSON_VERSION = 2`).
- Newly written fingerprints use `sha256:v2:<64 lowercase hex digits>`.
- Historical `sha256:<64 lowercase hex digits>` values are v1 fingerprints. They are not byte-compatible with v2 and must not be compared as if they used the same canonicalization contract.
- Phase A has not yet introduced the later Work-schema fingerprint columns, so no database migration/backfill is required for the v2 switch. Future persisted fingerprint readers must branch on the prefix instead of guessing the canonicalization version.

## JSON structure

- Object keys are strings only and are sorted by Unicode code-point order.
- Arrays preserve element order.
- Strings are emitted as JSON strings with UTF-8-safe non-ASCII characters (`ensure_ascii=False`).
- Whitespace is omitted outside strings.
- `null`, `true`, and `false` use normal JSON literals.
- Unsupported Python/container types fail with `TypeError`; they are never stringified implicitly.

## Numeric canonicalization

Supported number types are exactly built-in `int` and built-in `float`.

### Integers

Integers are emitted as their exact base-10 value with no exponent and no unnecessary leading zeroes.

### Floats

Finite Python floats are treated as IEEE-754 binary64 values. Canonicalization verifies the runtime uses binary radix, 53-bit precision, and the binary64 exponent range before serializing a float; an incompatible future runtime fails explicitly instead of silently producing another fingerprint contract. It then uses `float.as_integer_ratio()` and renders the exact mathematical decimal value of that binary64 number. It does not call `repr()`, `format()`, or the JSON encoder for number rendering.

Consequences:

- `1` and `1.0` both serialize as `1`.
- `0`, `0.0`, and `-0.0` all serialize as `0`.
- `1.5` serializes as `1.5`.
- `0.1` serializes as `0.1000000000000000055511151231257827021181583404541015625`, the exact decimal value of the binary64 float.
- very small and very large finite floats are emitted without exponent notation; the representation may be long, but it is deterministic and independent of runtime float display policy.
- `NaN`, positive Infinity, and negative Infinity are rejected with `ValueError`.

### Decimal-like/custom numeric types

`decimal.Decimal`, `fractions.Fraction`, NumPy scalar types, and custom numeric classes are not part of the v2 canonical JSON number domain and fail explicitly with `TypeError`. Callers that need decimal-domain semantics must first map them to a domain representation with an explicit schema (for example a decimal string plus scale) instead of relying on implicit coercion.

## Compatibility rule

Canonical JSON text written under an older contract remains valid historical JSON and is not rewritten merely to look v2-canonical. When a value must be fingerprinted under v2, parse the semantic value and serialize it under v2; do not relabel an old digest.

Persistent fingerprints must be compared only when their algorithm/canonicalization prefixes match. A version change is a cache/staleness boundary, not evidence that the semantic source data changed.

## Rationale

Dependency and input fingerprints participate in resume, staleness, QA caching, and reproducibility. Exact numeric canonicalization avoids false cache misses or false equality caused solely by `1` versus `1.0`, signed zero, or changes in runtime float-to-text formatting.
