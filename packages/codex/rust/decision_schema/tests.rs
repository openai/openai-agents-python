use super::*;

fn object(properties: Value) -> Value {
    let required: Vec<_> = properties.as_object().unwrap().keys().cloned().collect();
    json!({"type":"object","properties":properties,"required":required,"additionalProperties":false})
}

fn predicate(question: &Value, probability: f64) -> Value {
    json!({"type":"predicate","name":question["name"],"probability":probability})
}

fn choice(question: &Value, index: usize) -> Value {
    let choices = question["choices"].as_array().unwrap();
    json!({
        "type":"choice", "name":question["name"],
        "choice":choices[index]["value"], "confidence":1.0,
        "probabilities":choices.iter().enumerate().map(|(i,value)| {
            json!({"value":value["value"],"probability":if i==index {1.0} else {0.0}})
        }).collect::<Vec<_>>()
    })
}

#[test]
fn decodes_typed_fields_and_preserves_constant_values() {
    let schema = object(json!({
        "approved":{"type":"boolean"},
        "bucket":{"type":"string","enum":["billing","technical"]},
        "likelihood":{"type":"number","minimum":0,"maximum":1},
        "severity":{"type":"number","minimum":1,"maximum":5},
        "source":{"type":"string","const":"fixed"}
    }));
    let plan = DecisionSchema::compile(&schema, "Evaluate the support request.").unwrap();
    let questions = plan.questions();
    assert_eq!(questions.len(), 4);
    assert!(questions.iter().all(|q| {
        q["instructions"]
            .as_str()
            .unwrap()
            .contains("Evaluate the support request.")
    }));
    let score = json!({
        "type":"score", "name":questions[3]["name"], "score":2.25, "confidence":0.75,
        "probabilities":(0..5).map(|i|json!({
            "value":i, "label":format!("{}",i+1),
            "probability":match i {2=>0.75,3=>0.25,_=>0.0}
        })).collect::<Vec<_>>()
    });
    assert_eq!(
        plan.decode(&[
            predicate(&questions[0], 0.75),
            choice(&questions[1], 1),
            predicate(&questions[2], 0.2),
            score
        ])
        .unwrap(),
        json!({
            "approved":true,"bucket":"technical","likelihood":0.2,"severity":3.25,"source":"fixed"
        })
    );
}

#[test]
fn retains_nested_reference_and_choice_descriptions() {
    let mut schema = object(json!({
        "profile":{"$ref":"#/$defs/Profile","description":"Use the escalation policy."}
    }));
    schema["description"] = json!("The full ticket.");
    schema["$defs"] = json!({"Profile": {
        "type":"object","description":"Classify the customer's account.",
        "properties":{"kind":{"anyOf":[
            {"const":"ready","description":"No restrictions apply."},
            {"const":"blocked","description":"A manual check is required."}
        ]}},"required":["kind"],"additionalProperties":false
    }});
    let plan = DecisionSchema::compile(&schema, "Triage this request.").unwrap();
    let question = &plan.questions()[0];
    let instructions = question["instructions"].as_str().unwrap();
    for context in [
        "Triage this request.",
        "The full ticket.",
        "Use the escalation policy.",
        "Classify the customer's account.",
    ] {
        assert!(instructions.contains(context), "missing {context}");
    }
    assert!(
        question["choices"][1]["description"]
            .as_str()
            .unwrap()
            .contains("A manual check is required.")
    );
    assert_eq!(
        plan.decode(&[choice(question, 1)]).unwrap(),
        json!({"profile":{"kind":"blocked"}})
    );
}

#[test]
fn distinguishes_boolean_and_text_choices() {
    let plan =
        DecisionSchema::compile(&json!({"enum":[false,"false"]}), "Choose the value.").unwrap();
    assert_eq!(
        plan.decode(&[choice(&plan.questions()[0], 0)]).unwrap(),
        json!(false)
    );
    assert_eq!(
        plan.decode(&[choice(&plan.questions()[0], 1)]).unwrap(),
        json!("false")
    );
}

#[test]
fn uses_probability_threshold_for_boolean_only() {
    let plan = DecisionSchema::compile(&json!({"type":"boolean"}), "Is it ready?").unwrap();
    assert_eq!(plan.questions()[0]["type"], "predicate");
    for (probability, expected) in [(0.49, false), (0.5, true), (1.0, true)] {
        assert_eq!(
            plan.decode(&[predicate(&plan.questions()[0], probability)])
                .unwrap(),
            json!(expected)
        );
    }
}

#[test]
fn refuses_or_rejects_bad_answers_without_partial_output() {
    let plan = DecisionSchema::compile(&json!({"type":"boolean"}), "Is it ready?").unwrap();
    assert_eq!(
        plan.decode(&[json!({"type":"refusal","name":"field_1"})]),
        Err(DecisionError::Refusal)
    );
    for answers in [
        vec![],
        vec![predicate(&plan.questions()[0], 1.01)],
        vec![json!({"type":"predicate","name":"wrong","probability":1})],
    ] {
        assert_eq!(plan.decode(&answers), Err(DecisionError::InvalidAnswer));
    }
    let plan = DecisionSchema::compile(&json!({"enum":["a","b"]}), "Choose.").unwrap();
    let mut answer = choice(&plan.questions()[0], 0);
    answer["probabilities"][1]["value"] = answer["probabilities"][0]["value"].clone();
    assert_eq!(plan.decode(&[answer]), Err(DecisionError::InvalidAnswer));
}

#[test]
fn identifies_unbounded_or_unsupported_schemas_before_requesting() {
    for schema in [
        json!({"type":"string"}),
        json!({"type":"array","items":{"type":"boolean"}}),
        json!({"type":"string","enum":["a","b"],"pattern":"a"}),
        json!({"$ref":"#/$defs/Loop","$defs":{"Loop":{"$ref":"#/$defs/Loop"}}}),
    ] {
        assert!(DecisionSchema::compile(&schema, "Evaluate.").is_none());
    }
}

#[test]
fn bounds_question_count_and_decodes_constants_without_questions() {
    let fields: Map<String, Value> = (0..401)
        .map(|i| (format!("field_{i}"), json!({"type":"boolean"})))
        .collect();
    assert!(DecisionSchema::compile(&object(Value::Object(fields)), "Evaluate.").is_none());
    let plan =
        DecisionSchema::compile(&object(json!({"status":{"const":"fixed"}})), "Evaluate.").unwrap();
    assert!(plan.questions().is_empty());
    assert_eq!(plan.decode(&[]).unwrap(), json!({"status":"fixed"}));
}

#[test]
fn constrained_boolean_union_branches_preserve_their_finite_values() {
    for keyword in ["anyOf", "oneOf"] {
        for selected in [false, true] {
            for constraint in [json!({"const":selected}), json!({"enum":[selected]})] {
                let mut branch = json!({"type":"boolean"});
                branch
                    .as_object_mut()
                    .unwrap()
                    .extend(constraint.as_object().unwrap().clone());
                let schema = json!({keyword:[branch,{"type":"string","const":"unknown"}]});
                let plan = DecisionSchema::compile(&schema, "Choose the exact value.").unwrap();
                let questions = plan.questions();
                assert_eq!(questions.len(), 1);
                assert_eq!(questions[0]["type"], "choice");
                assert_eq!(questions[0]["choices"].as_array().unwrap().len(), 2);
                assert_eq!(
                    plan.decode(&[choice(&questions[0], 0)]).unwrap(),
                    json!(selected)
                );
                assert_eq!(
                    plan.decode(&[choice(&questions[0], 1)]).unwrap(),
                    json!("unknown")
                );
            }
        }
        let schema = json!({keyword:[{"type":"boolean"},{"type":"string","const":"unknown"}]});
        let plan = DecisionSchema::compile(&schema, "Choose the exact value.").unwrap();
        let questions = plan.questions();
        assert_eq!(questions[0]["choices"].as_array().unwrap().len(), 3);
        for (index, expected) in [json!(true), json!(false), json!("unknown")]
            .into_iter()
            .enumerate()
        {
            assert_eq!(
                plan.decode(&[choice(&questions[0], index)]).unwrap(),
                expected
            );
        }
    }
}

#[test]
fn boolean_union_constraints_keep_rejecting_invalid_or_ambiguous_branches() {
    for branch in [
        json!({"type":"boolean","enum":"invalid"}),
        json!({"type":"boolean","enum":[false,"invalid"]}),
        json!({"type":"boolean","const":"invalid"}),
        json!({"type":"boolean","const":false,"not":{"const":true}}),
    ] {
        for keyword in ["anyOf", "oneOf"] {
            let schema = json!({keyword:[branch,{"type":"string","const":"unknown"}]});
            assert!(DecisionSchema::compile(&schema, "Choose.").is_none());
        }
    }
    assert!(
        DecisionSchema::compile(
            &json!({"oneOf":[
                {"type":"boolean","const":false},{"type":"boolean","enum":[false]}
            ]}),
            "Choose."
        )
        .is_none()
    );
}
