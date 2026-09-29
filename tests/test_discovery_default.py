"""Enumeration must be discovery-only unless a caller asks for previews.

Why this gate exists
--------------------
``discovery_only`` was introduced in v0.9.31 to stop the 16 adapters from
re-parsing every transcript just to build list-card previews that most callers
throw away. It worked, but it shipped as something *each call site had to
remember*: ``cross_tool_list_sessions(limit=0)`` measured 1.53 s outside a
``discovery_only`` block against 0.158 s inside one, returning identical rows.
A 10x gap that only closes if you know to wrap the call is a default waiting to
be re-broken by the next caller.

The flag is now inverted — enumeration is discovery-only unless ``previews=True``
— and this file pins that. Removing the default turns every future listing into
the slow path again, silently.

Red-green for the default
-------------------------
* Drop the ``discovery_only()`` in ``cross_tool_list_sessions``'s fan-out →
  ``test_enumeration_defaults_to_discovery_only`` fails.
* Hard-code ``previews = False`` (ignoring the ambient context) →
  ``test_ambient_discovery_only_block_is_honoured`` fails.

The last adapter that ignored the flag (``trae_cn``, which stores summary cards
only and so had no other content source) is covered by
``test_every_registered_list_adapter_honours_discovery_only`` — the same shape
test that caught the kimi adapter in v0.9.31.
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters  # noqa: E402


class DiscoveryDefaultTest(unittest.TestCase):
    def test_enumeration_defaults_to_discovery_only(self):
        """No flag, no block → workers see discovery-only (the cheap default)."""
        self.assertFalse(
            echolib.discovery_only_active(),
            "test must start outside a discovery_only block",
        )
        seen = []
        dsh = _adapters.ADAPTER_REGISTRY["dsh"]
        real = dsh.get("list_sessions")
        dsh["list_sessions"] = lambda **kw: (seen.append(
            echolib.discovery_only_active()) or [{"session_id": "s",
                                                  "full_path": "/tmp/s",
                                                  "created": ""}])
        try:
            rows = _adapters.cross_tool_list_sessions(limit=1,
                                                      agent_filter="dsh")
        finally:
            dsh["list_sessions"] = real
        self.assertTrue(rows, "listing returned nothing; gate cannot judge")
        self.assertTrue(seen, "probe adapter was never called")
        self.assertTrue(seen[0],
                        "a bare cross_tool_list_sessions() must enumerate in "
                        "discovery-only mode without the caller wrapping it")

    def test_ambient_discovery_only_block_is_honoured(self):
        """An explicit block still wins — topic_classify and the builder use it."""
        with echolib.discovery_only():
            self.assertTrue(echolib.discovery_only_active())
            rows = _adapters.cross_tool_list_sessions(limit=1)
        self.assertTrue(rows)

    def test_previews_opt_in_still_produces_previews(self):
        """The flag must cross the thread boundary, in both directions.

        A ContextVar set in the caller does not reach a worker on its own — the
        fan-out re-enters a copied context per submission. Getting that wrong is
        exactly how v0.9.31's first attempt failed (the block evaporated and
        every worker did the work it was meant to skip), so this asserts the
        observed value *inside a worker* rather than reading the source.
        """
        seen = []
        dsh = _adapters.ADAPTER_REGISTRY["dsh"]
        real = dsh.get("list_sessions")
        dsh["list_sessions"] = lambda **kw: (seen.append(
            echolib.discovery_only_active()) or [])
        try:
            _adapters.cross_tool_list_sessions(limit=1, agent_filter="dsh")
            self.assertTrue(seen, "probe adapter was never called")
            self.assertTrue(seen[0],
                            "default must reach workers as discovery_only=True")
            seen.clear()
            _adapters.cross_tool_list_sessions(limit=1, agent_filter="dsh",
                                               previews=True)
            self.assertTrue(seen, "probe adapter was never called")
            self.assertFalse(seen[0], "previews=True must clear the flag")
        finally:
            dsh["list_sessions"] = real


class AdapterComplianceTest(unittest.TestCase):
    """Every registered ``list_sessions`` must honour the flag.

    The audit is by *transcript read*, not by preview emptiness: several
    adapters legitimately fall back to a cheap label (``summary = sid``), so a
    non-empty preview does not prove a re-parse. Counting reads is the signal
    that actually tracks the cost.
    """

    # Adapters that still read transcripts during enumeration, each with the
    # reason it cannot simply take the cheap path. Declared here rather than
    # tolerated silently — the failure this replaces was a gate that read
    # nothing and still passed, which is worse than no gate.
    #
    # Both were tried with a guard and reverted, because each one moved a real
    # row in ``sessions`` output. The exemption is per-adapter and therefore
    # also tolerates a *new* stray read inside one of these; keep the list
    # short, and prefer reverting a guard over growing this.
    STRUCTURAL_READERS = {
        "dim": "backfill.jsonl packs one session per record, so the row "
               "boundary exists only in the file; guarding the plain branch "
               "too moved created from session start to last write",
        "trae_cn": "multi-shard sessions merge here, so the scan picks the "
                   "canonical shard and the timestamps; substituting mtime "
                   "reordered rows because the in-transcript clock crosses "
                   "midnight where the file mtime does not",
    }

    def test_every_registered_list_adapter_honours_discovery_only(self):
        offenders = []
        exempted = []
        for name, spec in echolib.ADAPTER_REGISTRY.items():
            fn = spec.get("list_sessions")
            if not fn:
                continue
            reads = []
            with self._counted_reads(fn, reads):
                try:
                    with echolib.discovery_only():
                        fn(limit=3)
                except Exception as exc:  # reported, not raised: a broken
                    # adapter is a finding here, not a reason to abort the sweep
                    offenders.append(
                        f"{name}: raised {type(exc).__name__}: {exc}")
                    continue
            if not reads:
                continue
            if name in self.STRUCTURAL_READERS:
                exempted.append(f"{name} ({len(reads)} read(s), declared)")
            else:
                offenders.append(f"{name}: read {len(reads)} transcript(s) "
                                 f"under discovery_only")
        self.assertEqual(offenders, [],
                         "adapters that re-read transcripts during discovery:\n  "
                         + "\n  ".join(offenders))
        # Exemptions are a budget, not a loophole: a new one has to be written
        # down here, and the count is reported so drift is visible in review.
        self.assertLessEqual(
            len(exempted), len(self.STRUCTURAL_READERS),
            "more adapters are reading than are declared as structural")
        if exempted:
            sys.stderr.write("declared structural readers: "
                             + ", ".join(exempted) + "\n")

    @contextlib.contextmanager
    def _counted_reads(self, fn, sink):
        """Count transcript reads reached through *fn*'s module namespace.

        Restored per call, not per test: several adapters share a module, and a
        wrapper left installed from the previous one silently exempts the next
        (``claude``/``grok``/``trae_cn`` all live in ``_adapters``) — the gate
        then passes while measuring nothing, which is the exact failure mode a
        gate exists to prevent.
        """
        module = sys.modules[fn.__module__]
        saved = {}
        try:
            for attr in ("_iter_jsonl", "_read_jsonl"):
                original = getattr(module, attr, None)
                if original is None:
                    continue
                saved[attr] = original

                def wrapper(path, *a, _orig=original, **k):
                    sink.append(path)
                    return _orig(path, *a, **k)

                setattr(module, attr, wrapper)
            yield
        finally:
            for attr, original in saved.items():
                setattr(module, attr, original)


if __name__ == "__main__":
    unittest.main()
