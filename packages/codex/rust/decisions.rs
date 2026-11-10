//! Pure schema adaptation for the standalone Decisions provider.

use codex_api::decisions::{DecisionError, DecisionSchema};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::{Value, json};

fn schema(value: &str) -> PyResult<Value> {
    serde_json::from_str(value)
        .map_err(|_| PyValueError::new_err("invalid Decisions output schema"))
}

#[pyfunction]
pub(crate) fn _decision_questions(schema_json: &str, question: &str) -> PyResult<String> {
    let schema = schema(schema_json)?;
    let result = match DecisionSchema::compile(&schema, question) {
        Some(plan) => json!({"supported":true,"questions":plan.questions()}),
        None => json!({"supported":false,"questions":[]}),
    };
    serde_json::to_string(&result)
        .map_err(|_| PyValueError::new_err("invalid Decisions output schema"))
}

#[pyfunction]
pub(crate) fn _decision_result(
    schema_json: &str,
    question: &str,
    answers_json: &str,
) -> PyResult<String> {
    let schema = schema(schema_json)?;
    let plan = DecisionSchema::compile(&schema, question)
        .ok_or_else(|| PyValueError::new_err(DecisionError::UnsupportedSchema.to_string()))?;
    let answers: Vec<Value> = serde_json::from_str(answers_json)
        .map_err(|_| PyValueError::new_err(DecisionError::InvalidAnswer.to_string()))?;
    let value = plan
        .decode(&answers)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    serde_json::to_string(&value)
        .map_err(|_| PyValueError::new_err(DecisionError::InvalidAnswer.to_string()))
}
