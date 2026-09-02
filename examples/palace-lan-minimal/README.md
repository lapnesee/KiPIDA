# Minimal Palace LAN smoke project

This project is selected automatically when no Palace configuration has been
chosen in Ki-PIDA. It verifies the full LAN path: SSH authentication, project
upload, Palace schema/mesh validation, MPI wrapper execution, and artifact
retrieval.

The model is deliberately small: a 1 mm vacuum cube, five tetrahedra, one
terminal face (mesh attribute 3), and grounded outer faces (attribute 2). It is
an electrostatic transport smoke test, not a PCB or EMC result.

Required server-side command:

```text
palace -np 1 minimal-electrostatic.json
```

Ki-PIDA first adds `-serial --dry-run`, then performs the configured MPI run.
Replace this example with the JSON of a reviewed Palace project for engineering
analysis.
