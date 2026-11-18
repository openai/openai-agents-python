//! Convert one bounded output schema into Decisions questions and a JSON value.
//!
//! The SDK owns transport, provider selection, typed validation and execution.
use serde_json::{Map, Value, json};
use std::collections::HashSet;

#[derive(Debug, thiserror::Error, PartialEq)]
pub enum DecisionError {
    #[error("unsupported Decisions output schema")]
    UnsupportedSchema,
    #[error("invalid Decisions answer")]
    InvalidAnswer,
    #[error("Decisions refused a question")]
    Refusal,
}

/// A bounded, immutable schema plan shared by the language bindings.
#[derive(Debug)]
pub struct DecisionSchema {
    root: Node,
    questions: Vec<Value>,
}

impl DecisionSchema {
    /// Return None when the full output cannot be expressed as bounded questions.
    pub fn compile(schema: &Value, instructions: &str) -> Option<Self> {
        let mut root = compile(schema)?;
        root.prepend_context(instructions);
        let mut questions = Vec::new();
        root.questions(&mut questions);
        Some(Self { root, questions })
    }

    pub fn questions(&self) -> &[Value] {
        &self.questions
    }

    /// Validate the answer envelope before reconstructing the requested JSON value.
    pub fn decode(&self, answers: &[Value]) -> Result<Value, DecisionError> {
        validate_answers(&self.questions, answers)?;
        self.root.fill(&mut answers.iter())
    }
}

#[derive(Debug)]
enum Node {
    Constant(Value),
    Object(Vec<(String, Node)>),
    Field { question: Value, kind: FieldKind },
}

#[derive(Debug)]
enum FieldKind {
    Boolean,
    Probability,
    Choice(Vec<Value>),
    Score { minimum: f64, width: usize },
}

impl Node {
    fn prepend_context(&mut self, context: &str) {
        match self {
            Self::Constant(_) => {}
            Self::Object(fields) => {
                for (_, node) in fields {
                    node.prepend_context(context);
                }
            }
            Self::Field { question, .. } => {
                let instructions = question["instructions"].as_str().unwrap_or("");
                question["instructions"] = json!(format!("{context}\n{instructions}"));
            }
        }
    }

    fn questions(&self, out: &mut Vec<Value>) {
        match self {
            Self::Constant(_) => {}
            Self::Object(fields) => {
                for (_, node) in fields {
                    node.questions(out);
                }
            }
            Self::Field { question, .. } => out.push(question.clone()),
        }
    }

    fn fill<'a>(
        &self,
        answers: &mut impl Iterator<Item = &'a Value>,
    ) -> Result<Value, DecisionError> {
        match self {
            Self::Constant(value) => Ok(value.clone()),
            Self::Object(fields) => {
                let mut value = Map::new();
                for (name, node) in fields {
                    value.insert(name.clone(), node.fill(answers)?);
                }
                Ok(Value::Object(value))
            }
            Self::Field { kind, .. } => {
                let answer = answers.next().ok_or(DecisionError::InvalidAnswer)?;
                match kind {
                    FieldKind::Boolean => Ok(json!(
                        answer["probability"]
                            .as_f64()
                            .ok_or(DecisionError::InvalidAnswer)?
                            >= 0.5
                    )),
                    FieldKind::Probability => Ok(answer["probability"].clone()),
                    FieldKind::Choice(values) => {
                        let index = (0..values.len())
                            .find(|i| answer["choice"] == format!("value_{i}"))
                            .ok_or(DecisionError::InvalidAnswer)?;
                        Ok(values[index].clone())
                    }
                    FieldKind::Score { minimum, width } => {
                        let score = answer["score"]
                            .as_f64()
                            .filter(|v| v.is_finite() && *v >= 0.0 && *v <= *width as f64)
                            .ok_or(DecisionError::InvalidAnswer)?;
                        Ok(json!(score + minimum))
                    }
                }
            }
        }
    }
}

fn compile(schema: &Value) -> Option<Node> {
    // Bound traversal before resolving references or finite unions. This also
    // covers nested union branches, which do not create output containers.
    fn bounded(value: &Value, depth: usize, remaining: &mut usize) -> bool {
        if depth > 32 || *remaining == 0 {
            return false;
        }
        *remaining -= 1;
        match value {
            Value::Array(values) => values.iter().all(|v| bounded(v, depth + 1, remaining)),
            Value::Object(values) => values.values().all(|v| bounded(v, depth + 1, remaining)),
            _ => true,
        }
    }
    if !bounded(schema, 0, &mut 10000) {
        return None;
    }
    let mut count = 0;
    compile_node(
        schema,
        schema,
        "$",
        &mut Vec::new(),
        &mut count,
        &mut 10000,
        0,
    )
    .ok()
}

fn compile_node(
    schema: &Value,
    root: &Value,
    path: &str,
    refs: &mut Vec<String>,
    count: &mut usize,
    remaining_nodes: &mut usize,
    depth: usize,
) -> Result<Node, ()> {
    if depth > 32 || *remaining_nodes == 0 {
        return Err(());
    }
    *remaining_nodes -= 1;
    let object = schema.as_object().ok_or(())?;
    // Reject constraints we cannot enforce, rather than silently weakening the
    // schema. Annotation keywords do not alter the set of valid output values.
    const KEYS: &[&str] = &[
        "$ref",
        "$defs",
        "definitions",
        "$schema",
        "title",
        "description",
        "default",
        "examples",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
    ];
    if object.keys().any(|key| !KEYS.contains(&key.as_str())) {
        return Err(());
    }
    if let Some(reference) = schema.get("$ref") {
        let reference = reference.as_str().ok_or(())?;
        if !reference.starts_with("#/")
            || refs.iter().any(|r| r == reference)
            || object.keys().any(|k| {
                !matches!(
                    k.as_str(),
                    "$ref" | "$defs" | "definitions" | "$schema" | "title" | "description"
                )
            })
        {
            return Err(());
        }
        let target = root.pointer(&reference[1..]).ok_or(())?;
        refs.push(reference.into());
        let result = compile_node(target, root, path, refs, count, remaining_nodes, depth + 1);
        refs.pop();
        let mut node = result?;
        let annotations = schema_annotations(schema);
        if !annotations.is_empty() {
            node.prepend_context(&format!("Field {path} at this reference. {annotations}"));
        }
        return Ok(node);
    }
    if let Some(values) = finite_values(schema) {
        if values.is_empty() || values.len() > 255 {
            return Err(());
        }
        let mut unique = Vec::new();
        for value in values {
            if !scalar_matches(schema, &value) || unique.contains(&value) {
                return Err(());
            }
            unique.push(value);
        }
        if unique.len() == 1 {
            return Ok(Node::Constant(unique.remove(0)));
        }
        return field(count, path, schema, FieldKind::Choice(unique));
    }
    if schema.get("enum").is_some()
        || schema.get("const").is_some()
        || schema.get("anyOf").is_some()
        || schema.get("oneOf").is_some()
    {
        return Err(());
    }
    match schema["type"].as_str() {
        Some("object") => {
            if schema["additionalProperties"] != false {
                return Err(());
            }
            let empty_properties = Map::new();
            let properties = match schema.get("properties") {
                Some(value) => value.as_object().ok_or(())?,
                None => &empty_properties,
            };
            let required = match schema.get("required") {
                Some(value) => value.as_array().ok_or(())?.as_slice(),
                None => &[],
            };
            if required.len() != properties.len()
                || required
                    .iter()
                    .any(|v| v.as_str().is_none_or(|k| !properties.contains_key(k)))
                || required.iter().collect::<HashSet<_>>().len() != required.len()
            {
                return Err(());
            }
            if schema.get("minimum").is_some() || schema.get("maximum").is_some() {
                return Err(());
            }
            let mut fields = Vec::new();
            for (name, property) in properties {
                fields.push((
                    name.clone(),
                    compile_node(
                        property,
                        root,
                        &format!("{path}/{}", name.replace('~', "~0").replace('/', "~1")),
                        refs,
                        count,
                        remaining_nodes,
                        depth + 1,
                    )?,
                ));
            }
            let mut node = Node::Object(fields);
            let annotations = schema_annotations(schema);
            if !annotations.is_empty() {
                node.prepend_context(&format!("Field {path}. {annotations}"));
            }
            Ok(node)
        }
        Some("boolean") => field(count, path, schema, FieldKind::Boolean),
        Some("number") | Some("integer") => {
            let min = schema["minimum"]
                .as_f64()
                .filter(|v| v.is_finite())
                .ok_or(())?;
            let max = schema["maximum"]
                .as_f64()
                .filter(|v| v.is_finite())
                .ok_or(())?;
            if schema["type"] == "number" && min == 0.0 && max == 1.0 {
                field(count, path, schema, FieldKind::Probability)
            } else if min.fract() == 0.0
                && max.fract() == 0.0
                && max > min
                && (max - min) <= 254.0
                && min.abs() <= 9_007_199_254_740_991.0
                && max.abs() <= 9_007_199_254_740_991.0
            {
                let width = (max - min) as usize;
                if schema["type"] == "number" && width <= 9 {
                    field(
                        count,
                        path,
                        schema,
                        FieldKind::Score {
                            minimum: min,
                            width,
                        },
                    )
                } else if schema["type"] == "integer" {
                    field(
                        count,
                        path,
                        schema,
                        FieldKind::Choice(
                            (0..=width).map(|i| json!(min as i64 + i as i64)).collect(),
                        ),
                    )
                } else {
                    Err(())
                }
            } else {
                Err(())
            }
        }
        _ => Err(()),
    }
}

fn finite_values(schema: &Value) -> Option<Vec<Value>> {
    if let Some(value) = schema.get("const") {
        return Some(vec![value.clone()]);
    }
    if let Some(values) = schema.get("enum") {
        return values.as_array().cloned();
    }
    if schema["type"].as_str() == Some("null") {
        return Some(vec![Value::Null]);
    }
    if let Some(types) = schema["type"].as_array() {
        let mut values = Vec::new();
        for kind in types {
            match kind.as_str()? {
                "null" => values.push(Value::Null),
                "boolean" => {
                    values.push(json!(true));
                    values.push(json!(false));
                }
                _ => return None,
            }
        }
        return Some(values);
    }
    if let Some(branches) = schema
        .get("anyOf")
        .or_else(|| schema.get("oneOf"))
        .and_then(Value::as_array)
    {
        let mut values = Vec::new();
        for branch in branches {
            let branch_values = finite_values(branch).or_else(|| {
                (branch["type"] == "boolean").then(|| vec![json!(true), json!(false)])
            })?;
            // Union branches must be finite scalar schemas, with no hidden constraints.
            for value in branch_values {
                if !scalar_matches(branch, &value) {
                    return None;
                }
                values.push(value);
            }
        }
        return Some(values);
    }
    None
}

fn scalar_matches(schema: &Value, value: &Value) -> bool {
    if value.is_array() || value.is_object() {
        return false;
    }
    let Some(object) = schema.as_object() else {
        return false;
    };
    if object.keys().any(|k| {
        !matches!(
            k.as_str(),
            "type"
                | "const"
                | "enum"
                | "minimum"
                | "maximum"
                | "title"
                | "description"
                | "default"
                | "examples"
                | "anyOf"
                | "oneOf"
                | "$defs"
                | "definitions"
                | "$schema"
        )
    }) {
        return false;
    }
    let matches_type = |kind: &str| match kind {
        "null" => value.is_null(),
        "boolean" => value.is_boolean(),
        "string" => value.is_string(),
        "number" => value.is_number(),
        "integer" => value.as_f64().is_some_and(|v| v.fract() == 0.0),
        _ => false,
    };
    if let Some(kind) = schema.get("type") {
        if let Some(kind) = kind.as_str() {
            if !matches_type(kind) {
                return false;
            }
        } else if let Some(types) = kind.as_array() {
            if !types.iter().any(|t| t.as_str().is_some_and(matches_type)) {
                return false;
            }
        } else {
            return false;
        }
    }
    if schema.get("const").is_some_and(|v| v != value) {
        return false;
    }
    if schema
        .get("enum")
        .is_some_and(|v| v.as_array().is_none_or(|values| !values.contains(value)))
    {
        return false;
    }
    for (key, lower) in [("minimum", true), ("maximum", false)] {
        if let Some(bound) = schema.get(key) {
            let Some(bound) = bound.as_f64() else {
                return false;
            };
            if let Some(number) = value.as_f64() {
                if !bound.is_finite() || (lower && number < bound) || (!lower && number > bound) {
                    return false;
                }
            }
        }
    }
    for keyword in ["anyOf", "oneOf"] {
        if let Some(branches) = schema.get(keyword) {
            let Some(branches) = branches.as_array() else {
                return false;
            };
            let matches = branches
                .iter()
                .filter(|branch| scalar_matches(branch, value))
                .count();
            if matches == 0 || (keyword == "oneOf" && matches != 1) {
                return false;
            }
        }
    }
    true
}

fn schema_annotations(schema: &Value) -> String {
    let mut annotations = Vec::new();
    for key in ["title", "description"] {
        if let Some(value) = schema[key]
            .as_str()
            .filter(|value| !value.trim().is_empty())
        {
            annotations.push(format!("{key}: {value}"));
        }
    }
    for key in ["default", "examples"] {
        if let Some(value) = schema.get(key) {
            annotations.push(format!("{key}: {value}"));
        }
    }
    annotations.join("\n")
}

fn choice_description(schema: &Value, value: &Value) -> String {
    fn append_branches(schema: &Value, value: &Value, description: &mut String) {
        for keyword in ["anyOf", "oneOf"] {
            if let Some(branches) = schema[keyword].as_array() {
                for branch in branches
                    .iter()
                    .filter(|branch| scalar_matches(branch, value))
                {
                    let annotations = schema_annotations(branch);
                    if !annotations.is_empty() {
                        description.push('\n');
                        description.push_str(&annotations);
                    }
                    append_branches(branch, value, description);
                }
            }
        }
    }
    let mut description = value.to_string();
    append_branches(schema, value, &mut description);
    description
}

fn field(count: &mut usize, path: &str, schema: &Value, kind: FieldKind) -> Result<Node, ()> {
    *count += 1;
    if *count > 400 {
        return Err(());
    }
    let context = format!("Field {path}. {}", schema_annotations(schema));
    let mut question = json!({"name":format!("field_{}", *count), "instructions":context});
    match &kind {
        FieldKind::Boolean => {
            question["type"] = json!("predicate");
            question["instructions"] = json!(format!(
                "Determine whether this boolean field is true. {context}"
            ));
        }
        FieldKind::Probability => {
            question["type"] = json!("predicate");
            question["instructions"] = json!(format!(
                "Estimate the probability represented by this field. {context}"
            ));
        }
        FieldKind::Choice(values) => {
            question["type"] = json!("choice");
            question["choices"] = json!(
                values
                    .iter()
                    .enumerate()
                    .map(
                        |(i, v)| json!({"value":format!("value_{i}"), "description":choice_description(schema, v)})
                    )
                    .collect::<Vec<_>>()
            );
        }
        FieldKind::Score { minimum, width } => {
            question["type"] = json!("score");
            question["levels"] = json!(
                (0..=*width)
                    .map(|i| json!({"label":format!("{}", *minimum + i as f64)}))
                    .collect::<Vec<_>>()
            );
        }
    }
    Ok(Node::Field { question, kind })
}

fn unit(value: f64) -> bool {
    value.is_finite() && (0.0..=1.0).contains(&value)
}

fn validate_answers(questions: &[Value], answers: &[Value]) -> Result<(), DecisionError> {
    if questions.len() != answers.len() {
        return Err(DecisionError::InvalidAnswer);
    }
    for (question, answer) in questions.iter().zip(answers) {
        if answer["name"] != question["name"] {
            return Err(DecisionError::InvalidAnswer);
        }
        if answer["type"] == "refusal" {
            return Err(DecisionError::Refusal);
        }
        if answer["type"] != question["type"] {
            return Err(DecisionError::InvalidAnswer);
        }
        if answer["type"] == "predicate" {
            if !answer["probability"].as_f64().is_some_and(unit) {
                return Err(DecisionError::InvalidAnswer);
            }
            continue;
        }
        if !answer["confidence"].as_f64().is_some_and(unit) {
            return Err(DecisionError::InvalidAnswer);
        }
        let entries = answer["probabilities"]
            .as_array()
            .ok_or(DecisionError::InvalidAnswer)?;
        let expected: Vec<Value> = if question["type"] == "choice" {
            question["choices"]
                .as_array()
                .ok_or(DecisionError::InvalidAnswer)?
                .iter()
                .map(|c| c["value"].clone())
                .collect()
        } else {
            (0..question["levels"]
                .as_array()
                .ok_or(DecisionError::InvalidAnswer)?
                .len())
                .map(|i| json!(i))
                .collect()
        };
        if entries.len() != expected.len() {
            return Err(DecisionError::InvalidAnswer);
        }
        let mut seen = Vec::new();
        let mut total = 0.0;
        for entry in entries {
            let value = &entry["value"];
            if !expected.contains(value) || seen.contains(value) {
                return Err(DecisionError::InvalidAnswer);
            }
            seen.push(value.clone());
            total += entry["probability"]
                .as_f64()
                .filter(|v| unit(*v))
                .ok_or(DecisionError::InvalidAnswer)?;
            if question["type"] == "score" {
                let index = value.as_u64().ok_or(DecisionError::InvalidAnswer)? as usize;
                if entry["label"] != question["levels"][index]["label"] {
                    return Err(DecisionError::InvalidAnswer);
                }
            }
        }
        if (total - 1.0).abs() > 1e-5 {
            return Err(DecisionError::InvalidAnswer);
        }
        if question["type"] == "choice" {
            if !expected.contains(&answer["choice"]) {
                return Err(DecisionError::InvalidAnswer);
            }
        } else if !answer["score"]
            .as_f64()
            .is_some_and(|v| v.is_finite() && v >= 0.0 && v <= (expected.len() - 1) as f64)
        {
            return Err(DecisionError::InvalidAnswer);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
