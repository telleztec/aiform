import ast
import json
from pathlib import Path

import anthropic

from aiform import llm
from aiform.config import PROVIDER_TOKEN_ENV_VARS
from aiform.models import DriverReview, LLMConfig, ResourceSpec

SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"

# specs/ is a shared flat namespace: specs/<module>.md for hand-written
# aiform/*.py module specs (specs/README.md's convention) alongside
# specs/<provider>_<resource>.md for generated-driver acceptance specs. A
# contrived (provider, resource) pair -- e.g. provider="driver",
# resource="gen" -- would otherwise resolve to one of the former and get
# pasted into a generation prompt as if it were authoritative for that
# resource. Reserved names are excluded explicitly rather than trusted to
# never collide with a filename match.
#
# Only a module spec filename containing an underscore can ever actually
# collide: f"{provider}_{resource}.md" always inserts a literal "_"
# between the two (ResourceSpec.provider/resource are non-empty per
# RESOURCE_OR_PROVIDER_PATTERN, aiform/models.py), so an underscore-free
# name -- "README.md", "config.md", "driver.md", "exceptions.md",
# "llm.md", "models.md", "planner.md", "state.md" -- can never be
# produced by that pattern no matter what provider/resource values
# exist, and listing them here would be dead code giving false
# confidence the collision surface is covered. Add a new entry only if a
# future aiform/*.py module's own name contains an underscore.
RESERVED_MODULE_SPEC_NAMES = frozenset({"driver_gen.md"})

EXPECTED_METHOD_PARAMS: dict[str, list[str]] = {
    "create": ["self", "params", "credentials"],
    "read": ["self", "id", "credentials"],
    "update": ["self", "id", "current", "desired", "credentials"],
    "delete": ["self", "id", "credentials"],
}

MAX_DRAFT_ATTEMPTS = 2


class DriverValidationError(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class DriverGenerationFailed(Exception):
    def __init__(self, source: str, review: DriverReview | None, reasons: list[str]):
        self.source = source
        self.review = review
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _base_names(class_def: ast.ClassDef) -> list[str]:
    names = []
    for base in class_def.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _has_class_attr(class_def: ast.ClassDef, name: str) -> bool:
    for node in class_def.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
    return False


def _find_method(class_def: ast.ClassDef, name: str) -> ast.FunctionDef | None:
    for node in class_def.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _imports_anthropic(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "anthropic" or alias.name.startswith("anthropic.")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "anthropic" or node.module.startswith("anthropic.")):
                return True
    return False


def _references_anthropic_string(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "ANTHROPIC" in node.value
        ):
            return True
    return False


def validate_driver_source(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise DriverValidationError([f"syntax error: {e}"]) from e

    reasons: list[str] = []

    driver_class = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Driver"),
        None,
    )
    if driver_class is None:
        reasons.append("no class named 'Driver' found at module top level")
    else:
        if "ResourceDriver" not in _base_names(driver_class):
            reasons.append("class 'Driver' does not subclass ResourceDriver")
        if not _has_class_attr(driver_class, "PARAM_SCHEMA"):
            reasons.append("class 'Driver' is missing a 'PARAM_SCHEMA' class attribute")
        for method_name, expected_params in EXPECTED_METHOD_PARAMS.items():
            method = _find_method(driver_class, method_name)
            if method is None:
                reasons.append(f"missing method '{method_name}'")
            else:
                actual_params = [arg.arg for arg in method.args.args]
                if actual_params != expected_params:
                    reasons.append(
                        f"method '{method_name}' has unexpected parameters: "
                        f"expected {expected_params}, got {actual_params}"
                    )

    if _imports_anthropic(tree):
        reasons.append("imports the 'anthropic' package")
    if _references_anthropic_string(tree):
        reasons.append("references 'ANTHROPIC' in a string literal")

    if reasons:
        raise DriverValidationError(reasons)


def _format_feedback(reasons: list[str]) -> str:
    return "\n".join(f"- {r}" for r in reasons)


def draft_driver(
    spec: ResourceSpec,
    *,
    feedback: str | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str:
    system_prompt = (llm.PROMPTS_DIR / "generate_driver.md").read_text(encoding="utf-8")
    user_content = (
        f"provider: {spec.provider}\n"
        f"resource: {spec.resource}\n"
        f"example params:\n{json.dumps(spec.params, indent=2)}\n"
    )

    credentials_env_var = PROVIDER_TOKEN_ENV_VARS.get(spec.provider)
    if credentials_env_var:
        user_content += (
            f'\ncredentials will always be exactly {{"{credentials_env_var}": "<token>"}} -- '
            "read the token via that exact key, no other.\n"
        )

    spec_path = SPECS_DIR / f"{spec.provider}_{spec.resource}.md"
    if spec_path.name not in RESERVED_MODULE_SPEC_NAMES and spec_path.is_file():
        user_content += (
            "\nAn acceptance-criteria spec already exists for this exact "
            "(provider, resource) pair -- it is authoritative ground truth, "
            "more specific and more trustworthy than general training "
            "knowledge about this provider's API. Follow it exactly:\n\n"
            + spec_path.read_text(encoding="utf-8")
        )

    if feedback:
        user_content += (
            "\nThe previous draft was rejected for the following reasons -- "
            "address all of them in this redraft:\n" + feedback + "\n"
        )
    return llm.implementation_call(
        system_prompt,
        user_content,
        max_tokens=8192,
        client=client,
        llm_config=llm_config,
    )


def generate_driver(
    spec: ResourceSpec,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[str, DriverReview]:
    feedback: str | None = None

    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        source = draft_driver(spec, feedback=feedback, client=client, llm_config=llm_config)

        try:
            validate_driver_source(source)
        except DriverValidationError as e:
            if attempt == MAX_DRAFT_ATTEMPTS:
                raise DriverGenerationFailed(source, None, e.reasons) from e
            feedback = _format_feedback(e.reasons)
            continue

        review = llm.review_driver(source, client=client, llm_config=llm_config)
        if not review.approved:
            reasons = (
                review.blocking_issues or review.concerns or ["review did not approve the driver"]
            )
            if attempt == MAX_DRAFT_ATTEMPTS:
                raise DriverGenerationFailed(source, review, reasons)
            feedback = _format_feedback(reasons)
            continue

        return source, review
