# ADR 0001 — Python and PyPI only

**Status:** accepted · 2026-08-05

## Context

The system reads dependency manifests, upgrades pins, runs a test suite and
repairs the code the upgrade broke. Every one of those four steps is
ecosystem-specific: `requirements.txt` and `pyproject.toml` versus `package.json`
and lockfiles; `pip install` versus `npm ci`; `pytest` versus `jest`; and — most
importantly — what an API break *looks like* in a traceback.

Supporting npm as well would roughly double the surface of the worker while
adding nothing to the part of the system that is actually novel.

## Decision

The fleet supports Python packages from PyPI only. Ecosystem-specific logic
lives behind an adapter interface (`Dependency.ecosystem` is carried through the
domain and persisted) so that a second ecosystem is an addition rather than a
migration.

## Alternatives considered

**Python and npm from the start.** Rejected. The repair loop is the product, and
the repair loop is where our time is scarce. Two ecosystems would mean two test
runners, two manifest formats, two dependency resolvers and two sets of failure
modes to model — paid for out of the same fortnight.

**Ecosystem-agnostic from the start, via a generic "run the project's own test
command" abstraction.** Rejected as false economy. The abstraction is easy; the
hundred details underneath it are not, and building the abstraction before
having two real implementations would mean designing it against imagination.

## Consequences

- The fork pool is Python-only, which narrows candidate selection.
- A judge may ask "does this generalise?" — the honest answer is that the
  architecture does and the adapters do not yet exist, and the `ecosystem` field
  threaded through the domain is the evidence that we meant it.
- Scope creep into another ecosystem during the hackathon window would end the
  project. This ADR exists partly to make that refusal easy to point at.
