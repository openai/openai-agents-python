use std::collections::{BTreeMap, BTreeSet};
use std::ops::Deref;

use codex_protocol::dynamic_tools::DynamicToolSpec;
use codex_tools::ToolExposures;
use serde::de::Error as _;
use serde::ser::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;

/// Provider programmatic permission stays separate from local execution surfaces.
#[derive(Clone, Copy, Debug, Default)]
struct CallerLimits {
    surfaces: ToolExposures,
    programmatic: bool,
}

/// Current application declarations and the optional caller limits that travel
/// with each declaration. Core protocol specs and saved history carry no authority.
#[derive(Clone, Debug, Default)]
pub(crate) struct ToolDeclarations {
    specs: Vec<DynamicToolSpec>,
    limits: BTreeMap<usize, CallerLimits>,
    configured_agent_roles: BTreeMap<usize, String>,
    managed_approvals: BTreeSet<usize>,
}

impl ToolDeclarations {
    pub(crate) fn managed_approval(&self, slot: usize) -> bool {
        self.managed_approvals.contains(&slot)
    }

    pub(crate) fn configured_agent_role(&self, slot: usize) -> Option<&str> {
        self.configured_agent_roles.get(&slot).map(String::as_str)
    }

    pub(crate) fn without_configured_agent_roles(&self) -> Self {
        let mut declarations = self.clone();
        declarations.configured_agent_roles.clear();
        // Only roster_fingerprint consumes this projection. Observer timing is
        // current application binding data, not a saved graph identity change.
        declarations.managed_approvals.clear();
        declarations
    }

    pub(crate) fn provider_callers(&self, slot: usize) -> Option<codex_tools::ProviderToolCallers> {
        let limits = self.limits.get(&slot)?;
        if !limits.programmatic {
            return None;
        }
        Some(
            codex_tools::ProviderToolCallers::PROGRAMMATIC
                | if limits.surfaces.contains(ToolExposures::DIRECT) {
                    codex_tools::ProviderToolCallers::DIRECT
                } else {
                    codex_tools::ProviderToolCallers::NONE
                },
        )
    }

    pub(crate) fn caller_limits(&self, slot: usize) -> Option<ToolExposures> {
        self.limits.get(&slot).map(|limits| limits.surfaces)
    }

    fn push_with_callers(&mut self, spec: DynamicToolSpec, limits: Option<CallerLimits>) {
        if let Some(limits) = limits {
            self.limits.insert(self.specs.len(), limits);
        }
        self.specs.push(spec);
    }

    pub(crate) fn push(&mut self, spec: DynamicToolSpec, callers: Option<ToolExposures>) {
        self.push_with_callers(
            spec,
            callers.map(|surfaces| CallerLimits {
                surfaces,
                programmatic: false,
            }),
        );
    }

    pub(crate) fn append_captured(&mut self, source: &Self, slot: usize) {
        if source.managed_approval(slot) {
            self.managed_approvals.insert(self.specs.len());
        }
        if let Some(role) = source.configured_agent_roles.get(&slot) {
            self.configured_agent_roles.insert(self.specs.len(), role.clone());
        }
        self.push_with_callers(
            source.specs[slot].clone(),
            source.limits.get(&slot).copied(),
        );
    }
    pub(crate) fn model_surfaces(&self, slot: usize) -> Option<ToolExposures> {
        self.limits.get(&slot).map(|limits| {
            limits.surfaces
                | if limits.programmatic {
                    ToolExposures::DIRECT | ToolExposures::DEFERRED
                } else {
                    ToolExposures::NONE
                }
        })
    }
}

impl From<Vec<DynamicToolSpec>> for ToolDeclarations {
    fn from(specs: Vec<DynamicToolSpec>) -> Self {
        Self {
            specs,
            limits: BTreeMap::new(),
            configured_agent_roles: BTreeMap::new(),
            managed_approvals: BTreeSet::new(),
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
    Programmatic,
}

impl<'de> Deserialize<'de> for ToolDeclarations {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let values = Vec::<Value>::deserialize(deserializer)?;
        let mut result = Self::default();
        for mut value in values {
            let callers = value
                .as_object_mut()
                .and_then(|object| object.remove("allowedCallers"));
            let configured_role = value
                .as_object_mut()
                .and_then(|object| object.remove("configuredAgentRole"));
            let managed_approval = value.as_object_mut()
                .and_then(|object| object.remove("_sdkManagedApproval"));
            let spec: DynamicToolSpec = serde_json::from_value(value).map_err(D::Error::custom)?;
            if let Some(managed) = managed_approval {
                let managed = bool::deserialize(managed).map_err(D::Error::custom)?;
                if managed {
                    if !matches!(spec, DynamicToolSpec::Function(_) | DynamicToolSpec::Namespace(_)) {
                        return Err(D::Error::custom("Managed approval requires a captured function"));
                    }
                    result.managed_approvals.insert(result.specs.len());
                }
            }
            if let Some(role) = configured_role {
                let role = String::deserialize(role).map_err(D::Error::custom)?;
                if role.trim().is_empty() || !matches!(spec, DynamicToolSpec::Function(_)) {
                    return Err(D::Error::custom("Configured Agent origin requires a named function"));
                }
                result.configured_agent_roles.insert(result.specs.len(), role);
            }
            let limits = if let Some(callers) = callers {
                if !matches!(
                    spec,
                    DynamicToolSpec::Function(_)
                        | DynamicToolSpec::Custom(_)
                        | DynamicToolSpec::Namespace(_)
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
                let mut programmatic = false;
                for caller in callers {
                    let route = match caller {
                        Caller::Direct => ToolExposures::DIRECT | ToolExposures::DEFERRED,
                        Caller::Code => ToolExposures::CODE_MODE,
                        Caller::Programmatic => {
                            if programmatic {
                                return Err(D::Error::custom(
                                    "allowedCallers contains a duplicate caller",
                                ));
                            }
                            programmatic = true;
                            continue;
                        }
                    };
                    if limits.intersects(route) {
                        return Err(D::Error::custom(
                            "allowedCallers contains a duplicate route",
                        ));
                    }
                    limits |= route;
                }
                if programmatic && matches!(&spec, DynamicToolSpec::Custom(_)) {
                    return Err(D::Error::custom(
                        "Custom string tools do not support programmatic callers",
                    ));
                }
                Some(CallerLimits {
                    surfaces: limits,
                    programmatic,
                })
            } else {
                None
            };
            result.push_with_callers(spec, limits);
        }
        Ok(result)
    }
}

impl Serialize for ToolDeclarations {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if self.limits.is_empty() && self.configured_agent_roles.is_empty() && self.managed_approvals.is_empty() {
            return self.specs.serialize(serializer);
        }
        let mut values: Vec<Value> = self
            .specs
            .iter()
            .map(serde_json::to_value)
            .collect::<serde_json::Result<_>>()
            .map_err(S::Error::custom)?;
        for (slot, limits) in &self.limits {
            let mut callers = Vec::with_capacity(3);
            if limits.surfaces.contains(ToolExposures::DIRECT) {
                callers.push("direct");
            }
            if limits.surfaces.contains(ToolExposures::CODE_MODE) {
                callers.push("code");
            }
            if limits.programmatic {
                callers.push("programmatic");
            }
            values[*slot]["allowedCallers"] = serde_json::json!(callers);
        }
        for (slot, role) in &self.configured_agent_roles {
            values[*slot]["configuredAgentRole"] = serde_json::json!(role);
        }
        for slot in &self.managed_approvals {
            values[*slot]["_sdkManagedApproval"] = serde_json::json!(true);
        }
        values.serialize(serializer)
    }
}

#[cfg(test)]
mod programmatic_declaration_tests {
    use super::*;
    fn declaration(callers: Value) -> Value {
        serde_json::json!([{"type":"function","name":"lookup","description":"Synthetic.","inputSchema":{"type":"object","properties":{}},"allowedCallers":callers}])
    }
    #[test]
    fn programmatic_permission_does_not_grant_local_surfaces() {
        let captured: ToolDeclarations =
            serde_json::from_value(declaration(serde_json::json!(["programmatic"]))).unwrap();
        assert!(
            !captured
                .provider_callers(0)
                .unwrap()
                .contains(codex_tools::ProviderToolCallers::DIRECT)
        );
        assert!(
            captured
                .provider_callers(0)
                .unwrap()
                .contains(codex_tools::ProviderToolCallers::PROGRAMMATIC)
        );
        assert_eq!(
            serde_json::to_value(&captured).unwrap()[0]["allowedCallers"],
            serde_json::json!(["programmatic"])
        );
    }
    #[test]
    fn invalid_or_duplicate_programmatic_permissions_are_rejected() {
        for callers in [
            serde_json::json!([]),
            serde_json::json!(["programmatic", "programmatic"]),
            serde_json::json!(["unknown"]),
        ] {
            assert!(serde_json::from_value::<ToolDeclarations>(declaration(callers)).is_err());
        }
    }
}

#[cfg(test)]
mod managed_approval_declaration_tests {
    use super::*;
    #[test]
    fn managed_approval_is_captured_separately_from_protocol_specs() {
        let value = serde_json::json!([{
            "type":"function", "name":"read", "description":"Read.",
            "inputSchema":{"type":"object","properties":{}},
            "_sdkManagedApproval":true
        }]);
        let declarations: ToolDeclarations = serde_json::from_value(value).unwrap();
        assert!(declarations.managed_approval(0));
        assert!(serde_json::to_value(&declarations[0]).unwrap().get("_sdkManagedApproval").is_none());
        let mut appended = ToolDeclarations::default();
        appended.append_captured(&declarations, 0);
        assert!(appended.managed_approval(0));
        let plain: ToolDeclarations = serde_json::from_value(serde_json::json!([{
            "type":"function", "name":"read", "description":"Read.",
            "inputSchema":{"type":"object","properties":{}}
        }])).unwrap();
        assert!(!plain.managed_approval(0));
        assert_eq!(serde_json::to_value(declarations.without_configured_agent_roles()).unwrap(),
                   serde_json::to_value(&plain).unwrap());
        assert_eq!(serde_json::to_value(&declarations).unwrap()[0]["_sdkManagedApproval"], true);
    }
}
