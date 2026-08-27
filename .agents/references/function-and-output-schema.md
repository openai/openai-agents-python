# Function and Output Schema

Use this reference for changes to function-tool signature inspection, parameter metadata, strict JSON schema conversion, tool argument reconstruction, or structured agent output schemas.

Schema behavior is a compatibility boundary shared by Python callables, Pydantic, model providers, and runtime validation. Keep schema generation and invocation aligned rather than fixing one representation in isolation.

## Function Schema Ownership

- Explicit decorator arguments for a function name or description override values inferred from the callable and its docstring.
- Parameter descriptions from parsed docstrings take precedence over description strings carried by `Annotated`. Preserve `Field` constraints, aliases, and defaults when merging `Annotated` metadata.
- A run context parameter is special only in the first parameter position. Exclude it from the model-visible schema while still supplying it during invocation; do not silently treat later context-typed parameters as injected context.
- Keep the inspected signature, generated Pydantic model, JSON schema, and `to_call_args()` reconstruction consistent. Cover positional-only parameters, keyword-only parameters, `*args`, and `**kwargs` when changing this path.
- Reject unsupported callable shapes or invalid schemas when the tool is constructed so failures do not depend on whether a particular model later selects the tool.

## Strict JSON Schema Conversion

- Strict conversion closes object schemas with `additionalProperties: false` and marks their declared properties required. Reject an explicit `additionalProperties: true` instead of silently changing its meaning.
- Preserve the meaning of unions, intersections, definitions, and references. Normalize `oneOf` where required, process `allOf`, retain chained references, and merge a referenced schema with sibling keys without discarding the siblings.
- Remove defaults that only encode Python `None`; a nullable type must remain represented by its type schema rather than by an unsupported default.
- `ensure_strict_json_schema()` may mutate a non-empty input dictionary. Copy caller-owned schemas at public boundaries before conversion. Empty-schema conversion must return a fresh object rather than shared mutable state.
- Keep strictness explicit. If a tool or output schema opts out of strict mode, preserve that choice through provider conversion instead of partially applying strict normalization.

## Structured Output Schemas

- Plain `str` output and no declared output type use the plain-text path. Pydantic models and dictionary-shaped outputs expose their object schema directly; other Python types use the SDK's wrapper object with the `response` key.
- Keep generated output names stable and descriptive for nested generics, unions, and `Literal` types. These names are observable in provider requests and diagnostics.
- Parse model output as JSON and validate it through the output type adapter. Convert JSON or validation failures to the SDK's model-behavior error boundary rather than leaking provider- or Pydantic-specific exceptions.
- Streaming and non-streaming adapters must carry the same schema, strictness flag, wrapper behavior, and validation result.

## Review Checklist

1. Test precedence among explicit metadata, docstrings, `Annotated`, and `Field` values.
2. Test invocation reconstruction for positional-only, keyword-only, variadic, and context-bearing callables.
3. Test nested objects, unions, intersections, sibling and chained references, nullable fields, and caller-owned schema mutation.
4. Test plain text, direct object output, wrapped scalar or generic output, invalid JSON, and validation failure.
5. Verify every provider adapter receives the same normalized schema and strictness decision.

## Sources

- `src/agents/function_schema.py`
- `src/agents/strict_schema.py`
- `src/agents/tool.py`
- `src/agents/agent_output.py`
- `src/agents/models/`
- `tests/test_function_schema.py`
- `tests/test_function_tool_decorator.py`
- `tests/test_strict_schema.py`
- `tests/test_strict_schema_oneof.py`
- `tests/test_output_tool.py`
- `tests/models/`
