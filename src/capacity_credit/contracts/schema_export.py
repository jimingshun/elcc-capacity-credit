"""Generation of the controlled JSON Schemas.

The schema object boundaries follow the current P0 design baseline
(eight ``schemas/*.json`` files after T18):

- ``dataset-manifest-1.0.0-draft.3.json``  -> data contract V0.3 §3
- ``import-batch-1.0.json``                -> application service contract V0.2 §2
- ``run-config-1.0.json``                  -> application service contract V0.2 §3
- ``run-config-1.1.json``                  -> methodology V0.8 §16A (MRI/rMRI)
- ``run-manifest-1.0.json``                -> application service contract V0.2 §4
- ``method-result-1.3.json``               -> methodology V0.4 §16 (Schema 1.3)
- ``method-result-2.0.json``               -> methodology V0.8 §16A (MRI/rMRI)
- ``artifact-manifest-1.0.json``           -> application service contract V0.2 §7

Conditional (if/then) constraints mirror the model-level conditional
rules of the design documents. Generation is deterministic: call
``generate()`` and the committed ``schemas/*.json`` files must not change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from capacity_credit.contracts.artifacts import ArtifactManifest, ArtifactManifest11
from capacity_credit.contracts.dataset import DatasetManifest
from capacity_credit.contracts.elcc_diagnostic_specs import ELCC_DIAGNOSTIC_REGISTRY
from capacity_credit.contracts.elcc_status import ALLOWED_METHOD_RESULT_ELCC_STATUS_COMBOS
from capacity_credit.contracts.imports import ImportBatch
from capacity_credit.contracts.mri_contracts import (
    DIAGNOSTIC_POLICY_BY_STATUS,
    MRI_DATA_ERROR_CODES,
    MRI_DIAGNOSTIC_GROUPS,
    MRI_DIAGNOSTIC_SEVERITY,
    MRI_METHOD_ERROR_CODES,
    MRI_RESULT_POLICY_BY_COMBINATION,
    RISK_CONVERGENCE_BY_STATUS,
    MriResult2,
    RunConfig11,
)
from capacity_credit.contracts.results import MethodResult
from capacity_credit.contracts.runs import RunConfig, RunConfig12, RunManifest

_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"

_SCHEMA_SPECS: dict[str, tuple[type[BaseModel], str]] = {
    "dataset-manifest-1.0.0-draft.3.json": (DatasetManifest, "1.0.0-draft.3"),
    "import-batch-1.0.json": (ImportBatch, "1.0"),
    "run-config-1.0.json": (RunConfig, "1.0"),
    "run-config-1.1.json": (RunConfig11, "1.1"),
    "run-config-1.2.json": (RunConfig12, "1.2"),
    "run-manifest-1.0.json": (RunManifest, "1.0"),
    "method-result-1.3.json": (MethodResult, "1.3"),
    "method-result-2.0.json": (MriResult2, "2.0"),
    "artifact-manifest-1.0.json": (ArtifactManifest, "1.0"),
    "artifact-manifest-1.1.json": (ArtifactManifest11, "1.1"),
}

SCHEMA_FILES: dict[str, str] = {name: version for name, (_model, version) in _SCHEMA_SPECS.items()}

# V1-T3-A: run-config 1.2 is adequacy-only with the structured
# adequacy_config section; the conditions mirror the model validators.
_RUN_CONFIG_12_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {"properties": {"run_config_schema_version": {"const": "1.2"}}},
        "then": {
            "properties": {"run_type": {"const": "adequacy"}},
            "required": ["adequacy_config"],
        },
    },
    {
        "if": {"properties": {"run_config_schema_version": {"const": "1.2"}}},
        "then": {"properties": {"capacity_value_type": {"const": "not_applicable"}}},
    },
    {
        "if": {"properties": {"chronology_mode": {"const": "continuous_multiyear"}}},
        "then": {"properties": {"soc_year_boundary_mode": {"enum": ["carry_over"]}}},
    },
    {
        "if": {"properties": {"chronology_mode": {"const": "independent_weather_years"}}},
        "then": {"properties": {"soc_year_boundary_mode": {"not": {"enum": ["carry_over"]}}}},
    },
]

_RUN_CONFIG_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {"properties": {"chronology_mode": {"const": "continuous_multiyear"}}},
        "then": {"properties": {"soc_year_boundary_mode": {"enum": ["carry_over"]}}},
    },
    {
        "if": {"properties": {"chronology_mode": {"const": "independent_weather_years"}}},
        "then": {"properties": {"soc_year_boundary_mode": {"not": {"enum": ["carry_over"]}}}},
    },
    {
        "if": {"properties": {"run_type": {"const": "report"}}},
        "then": {
            "required": ["source_run_id"],
            "properties": {"source_run_id": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"properties": {"run_type": {"enum": ["mri", "rmri"]}}},
        "then": {
            "required": ["assessment_anchor", "perturbation_dimension"],
            "properties": {
                "assessment_anchor": {"not": {"type": "null"}},
                "perturbation_dimension": {"not": {"type": "null"}},
            },
        },
    },
    {
        "if": {"not": {"properties": {"run_type": {"enum": ["mri", "rmri"]}}}},
        "then": {
            "properties": {
                "assessment_anchor": {"type": "null"},
                "perturbation_dimension": {"type": "null"},
            }
        },
    },
    {
        "if": {"properties": {"capacity_value_type": {"const": "marginal"}}},
        "then": {
            "required": ["baseline_portfolio_version"],
            "properties": {"baseline_portfolio_version": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"properties": {"allocation_type": {"const": "governance_rule"}}},
        "then": {
            "required": ["allocation_rule_id"],
            "properties": {"allocation_rule_id": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"properties": {"run_type": {"const": "elcc"}}},
        "then": {
            "required": ["method_config"],
            "properties": {"method_config": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"not": {"properties": {"run_type": {"const": "elcc"}}}},
        "then": {"properties": {"method_config": {"type": "null"}}},
    },
    # T14 storage SOC formal chain: the top-level mode and the typed
    # storage_soc section must agree; applicable modes never pair with a
    # not_applicable year boundary on independent weather years.
    {
        "if": {"properties": {"storage_soc_initialization_mode": {"const": "not_applicable"}}},
        "then": {
            "properties": {"method_config": {"properties": {"storage_soc": {"type": "null"}}}}
        },
    },
    {
        "if": {
            "properties": {
                "storage_soc_initialization_mode": {
                    "enum": ["weather_cycle_warmup", "stochastic_sample_burnin"]
                }
            }
        },
        "then": {
            "properties": {
                "method_config": {"properties": {"storage_soc": {"not": {"type": "null"}}}}
            }
        },
    },
    {
        "if": {
            "properties": {
                "storage_soc_initialization_mode": {
                    "enum": ["weather_cycle_warmup", "stochastic_sample_burnin"]
                },
                "chronology_mode": {"const": "independent_weather_years"},
            }
        },
        "then": {
            "properties": {"soc_year_boundary_mode": {"const": "reinitialize_with_configured_mode"}}
        },
    },
]

_RUN_MANIFEST_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {"properties": {"run_type": {"const": "elcc"}}},
        "then": {
            "required": ["method_config_hash", "application_config_hash"],
            "properties": {
                "method_config_hash": {"not": {"type": "null"}},
                "application_config_hash": {"not": {"type": "null"}},
            },
        },
    },
    {
        "if": {"not": {"properties": {"run_type": {"const": "elcc"}}}},
        "then": {
            "properties": {
                "method_config_hash": {"type": "null"},
                "application_config_hash": {"type": "null"},
            }
        },
    },
]


def _method_result_2_conditionals() -> list[dict[str, Any]]:
    """The CLOSED method-result 2.0 matrix, generated FROM the SAME
    declarative rule constants that the Pydantic model consumes
    (``DIAGNOSTIC_POLICY_BY_STATUS``, ``MRI_RESULT_POLICY_BY_COMBINATION``,
    ``MRI_DIAGNOSTIC_GROUPS`` / ``MRI_DIAGNOSTIC_SEVERITY``,
    ``RISK_CONVERGENCE_BY_STATUS``) - never a second hand-written matrix.
    Every conditional is a TOP-LEVEL ``if``/``then`` (a ``then`` is never
    nested inside an ``if`` object), and every ``contains`` constraint
    carries the ``required`` field it relies on so a missing field can
    never accidentally satisfy the condition. JSON Schema cannot express
    finiteness (a JSON document cannot contain NaN/Infinity), so the
    structural envelope is enforced here and the finite check lives in the
    Pydantic model."""
    method_error_codes = sorted(MRI_METHOD_ERROR_CODES)
    data_error_codes = sorted(MRI_DATA_ERROR_CODES)
    warning_codes = sorted(
        code for code, severity in MRI_DIAGNOSTIC_SEVERITY.items() if severity == "warning"
    )
    mri_global_null = {
        "perfect_capacity_mri_value": {"type": "null"},
        "rmri_value": {"type": "null"},
        "equivalent_mri_per_mw": {"type": "null"},
        "accredited_capacity_mw": {"type": "null"},
        "capacity_credit_fraction": {"type": "null"},
    }
    conditionals: list[dict[str, Any]] = [
        # Global: method=mri never forms rMRI / capacity credit, and its
        # rmri_status is always not_applicable.
        {
            "if": {"properties": {"method": {"const": "mri"}}, "required": ["method"]},
            "then": {
                "properties": {
                    "rmri_status": {"const": "not_applicable"},
                    **mri_global_null,
                }
            },
        },
        # Global: method=rmri never carries RMRI_NOT_REQUESTED.
        {
            "if": {"properties": {"method": {"const": "rmri"}}, "required": ["method"]},
            "then": {
                "properties": {
                    "diagnostics": {
                        "not": {
                            "contains": {
                                "properties": {"code": {"const": "RMRI_NOT_REQUESTED"}},
                                "required": ["code"],
                            }
                        }
                    }
                }
            },
        },
    ]
    # Generic per-status severity / category rules (DIAGNOSTIC_POLICY).
    for status in sorted(DIAGNOSTIC_POLICY_BY_STATUS, key=lambda s: s.value):
        policy = DIAGNOSTIC_POLICY_BY_STATUS[status]
        status_if = {"properties": {"result_status": {"const": status.value}}}
        for severity in sorted(policy["forbidden_severities"]):
            conditionals.append(
                {
                    "if": status_if,
                    "then": {
                        "properties": {
                            "diagnostics": {
                                "not": {
                                    "contains": {
                                        "properties": {"severity": {"const": severity}},
                                        "required": ["severity"],
                                    }
                                }
                            }
                        }
                    },
                }
            )
        for category in sorted(policy["forbidden_category"]):
            codes = method_error_codes if category == "method_error" else data_error_codes
            conditionals.append(
                {
                    "if": status_if,
                    "then": {
                        "properties": {
                            "diagnostics": {
                                "not": {
                                    "contains": {
                                        "properties": {"code": {"enum": codes}},
                                        "required": ["code"],
                                    }
                                }
                            }
                        }
                    },
                }
            )
        for category in sorted(policy["required_category"]):
            codes = method_error_codes if category == "method_error" else data_error_codes
            conditionals.append(
                {
                    "if": status_if,
                    "then": {
                        "properties": {
                            "diagnostics": {
                                "contains": {
                                    "properties": {"code": {"enum": codes}},
                                    "required": ["code", "severity"],
                                }
                            }
                        }
                    },
                }
            )
        if policy["required_warning"]:
            conditionals.append(
                {
                    "if": status_if,
                    "then": {
                        "properties": {
                            "diagnostics": {
                                "contains": {
                                    "properties": {
                                        "code": {"enum": warning_codes},
                                        "severity": {"const": "warning"},
                                    },
                                    "required": ["code", "severity"],
                                }
                            }
                        }
                    },
                }
            )
    # Full combination matrix - generated FROM the SAME declarative map the
    # Pydantic model consumes (MRI_RESULT_POLICY_BY_COMBINATION).
    for (method, status, rmri), combo in sorted(
        MRI_RESULT_POLICY_BY_COMBINATION.items(),
        key=lambda item: (
            item[0][0].value,
            item[0][1].value,
            item[0][2].value,
        ),
    ):
        combo_if: dict[str, Any] = {
            "properties": {
                "method": {"const": method.value},
                "result_status": {"const": status.value},
                "rmri_status": {"const": rmri.value},
            },
            "required": ["method", "result_status", "rmri_status"],
        }
        then_properties: dict[str, Any] = {}
        then_required: list[str] = []
        for field in sorted(combo["required_non_null_fields"]):
            then_properties[field] = {"not": {"type": "null"}}
            then_required.append(field)
        for field in sorted(combo["required_null_fields"]):
            then_properties[field] = {"type": "null"}
            then_required.append(field)
        diagnostics_conditions: list[dict[str, Any]] = []
        for code in sorted(combo["required_diagnostic_codes"]):
            diagnostics_conditions.append(
                {
                    "contains": {
                        "properties": {"code": {"const": code}},
                        "required": ["code", "severity"],
                    }
                }
            )
        if combo["required_diagnostic_group"] is not None:
            group_codes = sorted(MRI_DIAGNOSTIC_GROUPS[combo["required_diagnostic_group"]])
            diagnostics_conditions.append(
                {
                    "contains": {
                        "properties": {"code": {"enum": group_codes}},
                        "required": ["code", "severity"],
                    }
                }
            )
        if combo["forbidden_diagnostic_codes"]:
            diagnostics_conditions.append(
                {
                    "not": {
                        "contains": {
                            "properties": {
                                "code": {"enum": sorted(combo["forbidden_diagnostic_codes"])}
                            },
                            "required": ["code"],
                        }
                    }
                }
            )
        if diagnostics_conditions:
            then_properties["diagnostics"] = {"allOf": diagnostics_conditions}
        # A combination that requires specific diagnostics, a warning, or a
        # method/data-error category must have the diagnostics field PRESENT
        # (removing it is a structural violation).
        diag_policy = DIAGNOSTIC_POLICY_BY_STATUS[status]
        diagnostics_required = bool(
            combo["required_diagnostic_codes"]
            or combo["required_diagnostic_group"]
            or combo["require_warning"]
            or diag_policy["required_category"]
        )
        if diagnostics_required:
            then_required.append("diagnostics")
        if combo["require_step_evidence"]:
            then_properties["sample_count"] = {"type": "integer", "exclusiveMinimum": 0}
            then_properties["base_step"] = {"type": "number", "exclusiveMinimum": 0}
            then_properties["step_multipliers"] = {
                "type": "array",
                "minItems": 3,
                "uniqueItems": True,
                "contains": {"const": 1.0},
                "items": {"type": "number", "exclusiveMinimum": 0},
            }
            then_properties["step_evaluations"] = {
                "type": "array",
                "minItems": 3,
                "items": {
                    "allOf": [
                        {
                            "properties": {
                                "step_multiplier": {"type": "number", "exclusiveMinimum": 0}
                            },
                            "required": ["step_multiplier"],
                        }
                    ]
                },
            }
            then_required.extend(
                ["sample_count", "base_step", "step_multipliers", "step_evaluations"]
            )
        if combo["require_perfect_capacity_step_evidence"]:
            then_properties["perfect_capacity_step_evaluations"] = {
                "type": "array",
                "minItems": 3,
                "items": {
                    "allOf": [
                        {
                            "properties": {
                                "step_multiplier": {"type": "number", "exclusiveMinimum": 0}
                            },
                            "required": ["step_multiplier"],
                        }
                    ]
                },
            }
            then_required.append("perfect_capacity_step_evaluations")
        then_schema: dict[str, Any] = {"properties": then_properties}
        if then_required:
            then_schema["required"] = then_required
        conditionals.append({"if": combo_if, "then": then_schema})
        if combo["perfect_capacity_evidence_if_present"]:
            conditionals.append(
                {
                    "if": {
                        "properties": {
                            **combo_if["properties"],
                            "perfect_capacity_mri_value": {"not": {"type": "null"}},
                        },
                        "required": [
                            "method",
                            "result_status",
                            "rmri_status",
                            "perfect_capacity_mri_value",
                        ],
                    },
                    "then": {
                        "properties": {
                            "perfect_capacity_step_evaluations": {
                                "type": "array",
                                "minItems": 1,
                            }
                        },
                        "required": ["perfect_capacity_step_evaluations"],
                    },
                }
            )
    # Illegal combinations: for every (method, result_status) the only legal
    # rmri_status values are those present in the policy - anything else is
    # rejected (e.g. rmri valid + unstable / rmri valid + not_applicable).
    allowed_by_pair: dict[tuple[str, str], set[str]] = {}
    for method, status, rmri in MRI_RESULT_POLICY_BY_COMBINATION:
        allowed_by_pair.setdefault((method.value, status.value), set()).add(rmri.value)
    for (method_value, status_value), allowed in sorted(allowed_by_pair.items()):
        conditionals.append(
            {
                "if": {
                    "properties": {
                        "method": {"const": method_value},
                        "result_status": {"const": status_value},
                        "rmri_status": {"not": {"enum": sorted(allowed)}},
                    },
                    "required": ["method", "result_status", "rmri_status"],
                },
                "then": {"not": {}},
            }
        )
    # risk_convergence must be consistent with result_status - generated
    # from the SAME declarative map the Pydantic model consumes.
    for status_value, risk_value in sorted(
        RISK_CONVERGENCE_BY_STATUS.items(), key=lambda kv: kv[0].value
    ):
        conditionals.append(
            {
                "if": {"properties": {"result_status": {"const": status_value.value}}},
                "then": {"properties": {"risk_convergence": {"const": risk_value.value}}},
            }
        )
    return conditionals


def _mri_diagnostic_item_schema() -> dict[str, Any]:
    """The frozen MRI/rMRI diagnostic item schema: every code must be a
    registered ``MRI_DIAGNOSTIC_CODES`` code and its severity must EXACTLY
    equal ``MRI_DIAGNOSTIC_SEVERITY[code]`` - an arbitrary ``severity=error``
    with an unknown code can never satisfy invalid_method / invalid_data."""
    from capacity_credit.contracts.mri_contracts import (
        MRI_DIAGNOSTIC_CODES,
        MRI_DIAGNOSTIC_SEVERITY,
    )

    return {
        "anyOf": [
            {
                "properties": {
                    "code": {"const": code},
                    "severity": {"const": MRI_DIAGNOSTIC_SEVERITY[code]},
                },
                "required": ["code", "severity"],
            }
            for code in sorted(MRI_DIAGNOSTIC_CODES)
        ]
    }


def _product_status_any_of() -> list[dict[str, Any]]:
    """Generate the JSON-Schema status matrix from the Python rule source."""
    return [
        {
            "properties": {
                "risk_convergence": {"const": risk.value},
                "search_convergence": {"const": search.value},
                "result_status": {"const": status.value},
            }
        }
        for risk, search, status in sorted(
            ALLOWED_METHOD_RESULT_ELCC_STATUS_COMBOS,
            key=lambda combo: tuple(item.value for item in combo),
        )
    ]


def _diagnostic_codes(category: str) -> list[str]:
    return sorted(
        code for code, spec in ELCC_DIAGNOSTIC_REGISTRY.items() if spec.category == category
    )


_ELCC_DIAGNOSTIC_ITEM_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {
            "properties": {
                "code": {"const": code},
                "severity": {"const": spec.severity.value},
            },
            "required": ["code", "severity"],
        }
        for code, spec in sorted(ELCC_DIAGNOSTIC_REGISTRY.items())
    ]
}

_METHOD_ERROR_CODES = _diagnostic_codes("method_error")
_DATA_ERROR_CODES = _diagnostic_codes("data_error")

_METHOD_RESULT_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {"properties": {"method": {"const": "adequacy"}}},
        "then": {
            "properties": {
                "resource_or_portfolio_id": {"not": {"type": "null"}},
                "candidate_risk": {"type": "null"},
                "matched_risk": {"type": "null"},
                "risk_difference": {"type": "null"},
                "reference_capacity_mw": {"type": "null"},
                "accredited_capacity_mw": {"type": "null"},
                "capacity_credit_fraction": {"type": "null"},
                "search_bracket_low_mw": {"type": "null"},
                "search_bracket_high_mw": {"type": "null"},
                "search_bracket_low_risk": {"type": "null"},
                "search_bracket_high_risk": {"type": "null"},
                "empirical_risk_resolution": {"type": "null"},
                "assessment_anchor": {"type": "null"},
                "anchor_risk_metric": {"type": "null"},
                "anchor_risk_value": {"type": "null"},
                "anchor_calibration_method": {"type": "null"},
                "anchor_standard_source_id": {"type": "null"},
                "perturbation_dimension": {"type": "null"},
                "perturbation_vector": {"type": "null"},
                "normalization_basis_mwh_per_mw": {"type": "null"},
                "capacity_value_type": {"const": "not_applicable"},
                "baseline_portfolio_version": {"type": "null"},
                "allocation_type": {"const": "none"},
                "allocation_rule_id": {"type": "null"},
                "rmri_status": {"const": "not_applicable"},
                "search_convergence": {"const": "not_applicable"},
                "interpolation_method": {"const": "not_applicable"},
                "risk_metrics": {
                    "properties": {
                        "candidate": {"type": "null"},
                        "matched": {"type": "null"},
                    }
                },
            }
        },
    },
    {
        "if": {"properties": {"method": {"enum": ["mri", "rmri"]}}},
        "then": {
            "required": [
                "assessment_anchor",
                "anchor_risk_metric",
                "anchor_risk_value",
                "anchor_calibration_method",
                "perturbation_dimension",
            ],
            "properties": {
                "assessment_anchor": {"enum": ["current_system", "calibrated_standard"]},
                "perturbation_dimension": {
                    "enum": ["fixed_duration", "power_only", "energy_only", "custom_vector"]
                },
            },
        },
    },
    {
        "if": {"not": {"properties": {"method": {"enum": ["mri", "rmri"]}}}},
        "then": {
            "properties": {
                "assessment_anchor": {"type": "null"},
                "perturbation_dimension": {"type": "null"},
            }
        },
    },
    {
        "if": {"properties": {"assessment_anchor": {"const": "calibrated_standard"}}},
        "then": {
            "required": ["anchor_standard_source_id"],
            "properties": {"anchor_standard_source_id": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"properties": {"assessment_anchor": {"const": "current_system"}}},
        "then": {"properties": {"anchor_standard_source_id": {"type": "null"}}},
    },
    {
        "if": {"properties": {"capacity_value_type": {"const": "marginal"}}},
        "then": {
            "required": ["baseline_portfolio_version"],
            "properties": {"baseline_portfolio_version": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"properties": {"allocation_type": {"const": "governance_rule"}}},
        "then": {
            "required": ["allocation_rule_id"],
            "properties": {"allocation_rule_id": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {"properties": {"method": {"enum": ["elcc", "efc"]}}},
        "then": {
            "allOf": [
                {"anyOf": _product_status_any_of()},
                {"properties": {"diagnostics": {"items": _ELCC_DIAGNOSTIC_ITEM_SCHEMA}}},
                {
                    "if": {
                        "properties": {"result_status": {"enum": ["valid", "valid_with_warning"]}}
                    },
                    "then": {
                        "properties": {
                            "accredited_capacity_mw": {"not": {"type": "null"}},
                            "matched_risk": {"not": {"type": "null"}},
                            "risk_difference": {"not": {"type": "null"}},
                            "interpolation_method": {"not": {"const": "not_applicable"}},
                            "risk_metrics": {
                                "required": ["matched"],
                                "properties": {"matched": {"not": {"type": "null"}}},
                            },
                        }
                    },
                },
                {
                    "if": {"properties": {"result_status": {"const": "valid"}}},
                    "then": {
                        "properties": {
                            "diagnostics": {
                                "not": {
                                    "contains": {
                                        "properties": {"severity": {"enum": ["warning", "error"]}}
                                    }
                                }
                            }
                        }
                    },
                },
                {
                    "if": {"properties": {"result_status": {"const": "valid_with_warning"}}},
                    "then": {
                        "properties": {
                            "diagnostics": {
                                "contains": {"properties": {"severity": {"const": "warning"}}},
                                "not": {
                                    "contains": {"properties": {"severity": {"const": "error"}}}
                                },
                            }
                        }
                    },
                },
                {
                    "if": {
                        "properties": {
                            "result_status": {"enum": ["not_converged", "low_resolution"]}
                        }
                    },
                    "then": {
                        "properties": {
                            "accredited_capacity_mw": {"type": "null"},
                            "capacity_credit_fraction": {"type": "null"},
                            "matched_risk": {"type": "null"},
                            "risk_difference": {"type": "null"},
                            "interpolation_method": {"const": "not_applicable"},
                            "risk_metrics": {"properties": {"matched": {"type": "null"}}},
                            "diagnostics": {
                                "not": {
                                    "contains": {"properties": {"severity": {"const": "error"}}}
                                }
                            },
                        }
                    },
                },
                {
                    "if": {"properties": {"result_status": {"const": "invalid_method"}}},
                    "then": {
                        "properties": {
                            "accredited_capacity_mw": {"type": "null"},
                            "capacity_credit_fraction": {"type": "null"},
                            "matched_risk": {"type": "null"},
                            "risk_difference": {"type": "null"},
                            "interpolation_method": {"const": "not_applicable"},
                            "risk_metrics": {"properties": {"matched": {"type": "null"}}},
                            "diagnostics": {
                                "contains": {
                                    "properties": {"code": {"enum": _METHOD_ERROR_CODES}},
                                    "required": ["code"],
                                },
                                "not": {
                                    "contains": {
                                        "properties": {"code": {"enum": _DATA_ERROR_CODES}},
                                        "required": ["code"],
                                    }
                                },
                            },
                        }
                    },
                },
                {
                    "if": {"properties": {"result_status": {"const": "invalid_data"}}},
                    "then": {
                        "properties": {
                            "accredited_capacity_mw": {"type": "null"},
                            "capacity_credit_fraction": {"type": "null"},
                            "matched_risk": {"type": "null"},
                            "risk_difference": {"type": "null"},
                            "search_bracket_low_mw": {"type": "null"},
                            "search_bracket_high_mw": {"type": "null"},
                            "search_bracket_low_risk": {"type": "null"},
                            "search_bracket_high_risk": {"type": "null"},
                            "empirical_risk_resolution": {"type": "null"},
                            "interpolation_method": {"const": "not_applicable"},
                            "risk_metrics": {"properties": {"matched": {"type": "null"}}},
                            "diagnostics": {
                                "contains": {
                                    "properties": {"code": {"enum": _DATA_ERROR_CODES}},
                                    "required": ["code"],
                                },
                                "not": {
                                    "contains": {
                                        "properties": {"code": {"enum": _METHOD_ERROR_CODES}},
                                        "required": ["code"],
                                    }
                                },
                            },
                        }
                    },
                },
            ]
        },
    },
]

_GRADE_ORDER = [
    "G0_teaching",
    "G1_preliminary",
    "G2_research",
    "G3_rule_support",
]

_IMPORT_BATCH_GRADE_RELATION: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {
                "grades": {
                    "properties": {
                        "declared_data_grade": {"const": grade},
                    }
                }
            }
        },
        "then": {
            "properties": {
                "grades": {
                    "properties": {
                        "effective_data_grade": {"enum": _GRADE_ORDER[: index + 1]},
                        "computed_max_data_grade": {"enum": _GRADE_ORDER[index:]},
                    }
                }
            }
        },
    }
    for index, grade in enumerate(_GRADE_ORDER)
]


def _artifact_manifest_10_conditions() -> list[dict[str, Any]]:
    """artifact-manifest 1.0 strict historical boundary: the global
    ArtifactType enum must NOT pollute the 1.0 schema. The 1.0 manifest
    must not carry a non-1.0 artifact type (currently analysis_summary;
    future coordination types must be gated here identically)."""
    from capacity_credit.contracts.artifacts import ARTIFACT_TYPES_1_0
    from capacity_credit.contracts.enums import ArtifactType

    non_1_0 = sorted({t.value for t in ArtifactType} - ARTIFACT_TYPES_1_0)
    return [
        {
            "if": {"required": ["artifacts"]},
            "then": {
                "properties": {
                    "artifacts": {
                        "not": {
                            "contains": {
                                "properties": {"artifact_type": {"enum": non_1_0}},
                                "required": ["artifact_type"],
                            }
                        }
                    }
                }
            },
        }
    ]


def _artifact_manifest_11_conditions() -> list[dict[str, Any]]:
    """artifact-manifest 1.1 JSON Schema conditions, equivalent to the
    ArtifactManifest11 model validator:

    - coordination: manifest_kind=coordination, run_type in
      {portfolio, sensitivity}, result_id null, at least one
      analysis_summary, and NO json_result entry;
    - result: manifest_kind=result, result_id a non-null UUID, and
      EXACTLY one json_result entry (minContains=maxContains=1)."""
    return [
        {
            "if": {"properties": {"manifest_kind": {"const": "coordination"}}},
            "then": {
                "required": ["artifacts"],
                "properties": {
                    "run_type": {"enum": ["portfolio", "sensitivity"]},
                    "result_id": {"type": "null"},
                    "artifacts": {
                        "allOf": [
                            {
                                "contains": {
                                    "properties": {"artifact_type": {"const": "analysis_summary"}},
                                    "required": ["artifact_type"],
                                }
                            },
                            {
                                "not": {
                                    "contains": {
                                        "properties": {"artifact_type": {"const": "json_result"}},
                                        "required": ["artifact_type"],
                                    }
                                }
                            },
                        ]
                    },
                },
            },
        },
        {
            "if": {"properties": {"manifest_kind": {"const": "result"}}},
            "then": {
                "required": ["result_id", "artifacts"],
                "properties": {
                    "result_id": {"not": {"type": "null"}},
                    "artifacts": {
                        "contains": {
                            "properties": {"artifact_type": {"const": "json_result"}},
                            "required": ["artifact_type"],
                        },
                        "minContains": 1,
                        "maxContains": 1,
                    },
                }
            },
        },
    ]


_CONDITIONALS: dict[str, list[dict[str, Any]]] = {
    "run-config-1.0.json": _RUN_CONFIG_CONDITIONALS,
    "run-config-1.2.json": _RUN_CONFIG_12_CONDITIONALS,
    "run-manifest-1.0.json": _RUN_MANIFEST_CONDITIONALS,
    "method-result-1.3.json": _METHOD_RESULT_CONDITIONALS,
    "method-result-2.0.json": _method_result_2_conditionals(),
    "import-batch-1.0.json": _IMPORT_BATCH_GRADE_RELATION,
    "artifact-manifest-1.0.json": _artifact_manifest_10_conditions(),
    "artifact-manifest-1.1.json": _artifact_manifest_11_conditions(),
}

# pydantic does not propagate `pattern` into the anyOf branches of
# nullable fields; the manifest lineage hashes must be expressed as
# SHA-256 hex digests in the JSON Schema too (model <-> schema parity)
_SHA256_SCHEMA = {"type": "string", "pattern": r"[0-9a-fA-F]{64}"}

_MANIFEST_HASH_FIELDS = ("method_config_hash", "application_config_hash")


def generate_dicts() -> dict[str, dict[str, Any]]:
    """Return filename -> JSON Schema document (deterministic order)."""
    out: dict[str, dict[str, Any]] = {}
    for filename, (model, version) in _SCHEMA_SPECS.items():
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        root: dict[str, Any] = {
            "$schema": _SCHEMA_DRAFT,
            "$id": f"schemas/{filename}",
            "x-contract-version": version,
            **schema,
            "title": filename,
        }
        if filename == "run-manifest-1.0.json":
            for field in _MANIFEST_HASH_FIELDS:
                root["properties"][field] = {
                    "oneOf": [_SHA256_SCHEMA, {"type": "null"}],
                    "default": None,
                    "title": root["properties"][field].get("title", field),
                }
        if filename == "method-result-2.0.json":
            # Freeze every MRI/rMRI diagnostic item: code in
            # MRI_DIAGNOSTIC_CODES and severity exactly equal to the
            # registered MRI_DIAGNOSTIC_SEVERITY[code].
            root["properties"]["diagnostics"]["items"] = _mri_diagnostic_item_schema()
        conditionals = _CONDITIONALS.get(filename)
        if conditionals:
            root["allOf"] = conditionals
        out[filename] = root
    return out


def generate_serialized() -> dict[str, str]:
    """Return filename -> serialized JSON Schema text (byte stable)."""
    return {
        name: json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for name, doc in generate_dicts().items()
    }


def generate_to(directory: Path) -> None:
    """Write all six schemas into ``directory`` (created if missing)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in generate_serialized().items():
        (directory / name).write_text(text, encoding="utf-8")


def generate() -> None:
    """Write all six schemas into the repository ``schemas/`` directory."""
    schemas_dir = Path(__file__).resolve().parents[3] / "schemas"
    generate_to(schemas_dir)


if __name__ == "__main__":
    generate()
