"""T24-B business demonstration package (thin orchestration only).

The demo entry NEVER re-implements ELCC/EFC/risk logic, report field
mappings, result parsing or templates: it reads a demo config, reuses
the formal application services (import_project / approve_and_freeze /
scenario_build / run_elcc_pipeline), the formal T23-A machine writer
(write_machine_outputs), the shared ReportView and the T24-A report
export (export_report), then renders the Chinese business analysis from
the SAME ReportView. Output lands in a stable, user-facing artifacts
directory (never tmp).
"""

from capacity_credit.demo.business_demo import run_demo

__all__ = ["run_demo"]
