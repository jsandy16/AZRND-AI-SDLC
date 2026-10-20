"""Strict output contracts of the agent roles. Every field is required (strict structured output); field descriptions
guide the model; pydantic validates the answer again and the framework checks it deterministically (operation names
and parameters against the kind's catalogue, examples by simulation, code by tests)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------------------------- planner
class PlanStep(_M):
    id: str = Field(description="snake_case step id, unique in the plan, e.g. keep_zip_only")
    op: str = Field(description="an operation name from the catalogue in the request; 'custom' only when no catalogue "
                                "operation (or combination) can do it")
    title: str = Field(description="one line a business reader understands")
    params_json: str = Field(description="the operation's parameters as a JSON object; for custom: {\"function\": \"<same as id>\"}")
    pseudocode: str = Field(description="custom steps: numbered pseudocode in terms of the API in the request; other ops: empty string")


class ComponentPlan(_M):
    summary: str = Field(description="what the component does, 2-4 sentences, for the person approving it")
    steps: list[PlanStep] = Field(description="ordered steps")
    assumptions: list[str] = Field(description="interpretations you made where the requirement was not explicit")
    ambiguities: list[str] = Field(description="questions the owner must answer before approval (two readings give different "
                                               "results and the examples do not decide); empty if none")


# ---------------------------------------------------------------------------------------------- test writer
class StepCase(_M):
    name: str = Field(description="snake_case case name")
    purpose: str = Field(description="which rule or edge case of the pseudocode this checks")
    input_json: str = Field(description="the function's input as JSON, in the case format given in the request")
    params_json: str = Field(description="run variables the case needs, as a JSON object of strings ({} if none)")
    expected_json: str = Field(description="the expected output as JSON in the format given; 'null' when expect_error")
    expect_error: bool = Field(description="true when the function must refuse (raise) for this input")


class StepTests(_M):
    cases: list[StepCase] = Field(description="3-8 cases: the normal path, each rule in the pseudocode, and edge cases")


# ---------------------------------------------------------------------------------------------- coder
class CustomStepCode(_M):
    function_source: str = Field(description="exactly one Python function with the signature given in the request (imports "
                                             "inside the function only, no module-level code)")
    explanation: str = Field(description="how the function implements the pseudocode and which tests it passed, for the reviewer")


# ---------------------------------------------------------------------------------------------- decomposer
class ComponentDraft(_M):
    name: str = Field(description="snake_case name of the component (reuse the existing name when one is given)")  # checked in decompose (repairable)
    reuse_existing: bool = Field(description="true when an existing component already does this part unchanged")
    config_json: str = Field(description="the component's structured settings as a JSON object in the format given for its kind "
                                         "(type, connection, credentials, delivery …); unknown values as \"<REQUIRED>\"")
    requirement: str = Field(description="this component's part of the requirement, self-contained, in plain English; "
                                         "empty when plain settings are enough (no AI plan needed)")
    examples_json: str = Field(description="JSON list of examples in the format given for its kind ([] when requirement is empty)")


class PipelineSplit(_M):
    summary: str = Field(description="how the pipeline works end to end, 3-6 sentences, for the person approving the split")
    trigger: ComponentDraft
    source: ComponentDraft
    has_transformation: bool = Field(description="false when files are delivered unchanged (transformation is then ignored)")
    transformation: ComponentDraft
    destination: ComponentDraft
    package_name: str = Field(description="package name template, e.g. '{event_key_stem}' or '{source}_{ds_nodash}'")
    pipeline_examples_json: str = Field(description="JSON list of end-to-end examples in the pipeline example format")
    assumptions: list[str]
    ambiguities: list[str] = Field(description="questions the owner must answer before approval; empty if none")


# ---------------------------------------------------------------------------------------------- requirement analyst
class FinalizedRequirement(_M):
    pipeline_name: str = Field(description="snake_case pipeline name (the one in the request when given)")  # checked in analyst
    description: str = Field(description="one line: what the pipeline does")
    requirement: str = Field(description="the finalized requirement in plain English, in the sections TRIGGER, SOURCE, "
                                         "TRANSFORMATION, DESTINATION, DELIVERY BEHAVIOUR, each a list of precise bullet rules; "
                                         "infrastructure values the document does not give stay as <REQUIRED>")
    pseudocode: str = Field(description="numbered pseudocode of the whole pipeline (trigger → source → transformation → "
                                        "destination → delivery) for business and technical readers")
    examples_json: str = Field(description="JSON list of end-to-end examples in the pipeline example format given in the request")
    assumptions: list[str] = Field(description="readings you chose where the document was not explicit")
    open_questions: list[str] = Field(description="questions the owners must answer before the requirement can be approved "
                                                  "(two reasonable readings give different pipelines); empty if none")
