# Ki-PIDA test conventions

The default local and CI command is:

```powershell
python -m unittest discover -s tests -q
```

Tests follow these boundaries while the suite is migrated into subdirectories:

- `test_analysis_contract.py`, `test_analysis_registry.py`, and
  `test_analysis_adapters.py` protect the public result boundary shared by the
  UI, history, and exporters.
- Application-controller tests verify background execution, progress,
  completion, errors, cancellation, and rejection of concurrent jobs without
  importing wxPython.
- Numerical unit tests use small deterministic meshes and explicit tolerances.
- Integration tests use temporary directories and must not depend on test
  execution order.
- Optional hardware/external-backend tests use `skipUnless`; they must never
  silently replace the requested backend with a different implementation.
- UI compatibility tests should prefer source/AST checks when wxPython is not
  available in the runner. Tests that require a real wx event loop belong in a
  separate interactive smoke-test job.
- Result filtering and text presentation belong in pure application modules so
  severity/search behavior, recommendations, provenance, and model limitations
  remain testable without a wx event loop.
- Tests must not print routine diagnostics. Capture logs and assert on them
  when diagnostic text is part of the behavior under test.

Every new analysis must register an `AnalysisDescriptor`, publish an
`AnalysisResult`, round-trip through JSON, and have at least one adapter test
covering PASS/WARN/FAIL or NO_DATA behavior as applicable.
