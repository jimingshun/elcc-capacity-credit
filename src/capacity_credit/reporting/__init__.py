"""T23-A: unified report mapping and machine-readable outputs.

The reporting layer is a READ-ONLY consumer of already-validated formal
results (MethodResult 1.3 / MriResult2 2.0). It never recomputes ELCC /
EFC / MRI, never re-derives result status, never fabricates point
estimates from non-converged intervals and never writes NaN/Infinity.
"""
