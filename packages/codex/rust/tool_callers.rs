use std::collections::BTreeMap;
use std::ops::Deref;

use codex_protocol::dynamic_tools::DynamicToolSpec;
use codex_tools::ToolExposures;
use serde::de::Error as _;
use serde::ser::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;

/// Current application declarations and the optional caller limits that travel
/// with each declaration. Core protocol specs and saved history carry no authority.
#[derive(Clone, Debug, Default)]
pub(crate) struct ToolDeclarations {
    specs: Vec<DynamicToolSpec>,
    limits: BTreeMap<usize, ToolExposures>,
}

impl ToolDeclarations {
    pub(crate) fn caller_limits(&self, slot: usize) -> Option<ToolExposures> {
        self.limits.get(&slot).copied()
    }

    pub(crate) fn push(&mut self, spec: DynamicToolSpec, callers: Option<ToolExposures>) {
        if let Some(callers) = callers {
            self.limits.insert(self.specs.len(), callers);
        }
        self.specs.push(spec);
    }
}

impl From<Vec<DynamicToolSpec>> for ToolDeclarations {
    fn from(specs: Vec<DynamicToolSpec>) -> Self {
        Self {
            specs,
            limits: BTreeMap::new(),
        }
    }
}

impl Deref for ToolDeclarations {
    type Target = [DynamicToolSpec];

    fn deref(&self) -> &Self::Target {
        &self.specs
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Caller {
    Direct,
    Code,
}

impl<'de> Deserialize<'de> for ToolDeclarations {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let values = Vec::<Value>::deserialize(deserializer)?;
        let mut result = Self::default();
        for mut value in values {
            let callers = value
                .as_object_mut()
                .and_then(|object| object.remove("allowedCallers"));
            let spec: DynamicToolSpec = serde_json::from_value(value).map_err(D::Error::custom)?;
            let limits = if let Some(callers) = callers {
                if !matches!(
                    spec,
                    DynamicToolSpec::Function(_) | DynamicToolSpec::Namespace(_)
                ) {
                    return Err(D::Error::custom(
                        "allowedCallers requires a function declaration",
                    ));
                }
                let callers: Vec<Caller> =
                    serde_json::from_value(callers).map_err(D::Error::custom)?;
                if callers.is_empty() {
                    return Err(D::Error::custom(
                        "allowedCallers requires at least one route",
                    ));
                }
                let mut limits = ToolExposures::NONE;
                for caller in callers {
                    let route = match caller {
                        Caller::Direct => ToolExposures::DIRECT | ToolExposures::DEFERRED,
                        Caller::Code => ToolExposures::CODE_MODE,
                    };
                    if limits.intersects(route) {
                        return Err(D::Error::custom(
                            "allowedCallers contains a duplicate route",
                        ));
                    }
                    limits |= route;
                }
                Some(limits)
            } else {
                None
            };
            result.push(spec, limits);
        }
        Ok(result)
    }
}

impl Serialize for ToolDeclarations {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if self.limits.is_empty() {
            return self.specs.serialize(serializer);
        }
        let mut values: Vec<Value> = self
            .specs
            .iter()
            .map(serde_json::to_value)
            .collect::<serde_json::Result<_>>()
            .map_err(S::Error::custom)?;
        for (slot, limits) in &self.limits {
            let mut callers = Vec::with_capacity(2);
            if limits.contains(ToolExposures::DIRECT) {
                callers.push("direct");
            }
            if limits.contains(ToolExposures::CODE_MODE) {
                callers.push("code");
            }
            values[*slot]["allowedCallers"] = serde_json::json!(callers);
        }
        values.serialize(serializer)
    }
}
