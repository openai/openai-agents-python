//! Declaration lineage for explicitly adopted runtime handoff targets.
//!
//! This data never carries callback bindings, approvals, or live factory claims.
//! Restoration still requires actual reachable declarations from the caller.
use super::*;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Enrollment {
    version: u32,
    root_id: String,
    promoted: Vec<Promoted>,
    graphs: Vec<Graph>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Promoted {
    name: String,
    source_id: String,
    tool_name: String,
    profile_id: String,
    fingerprint: String,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Graph {
    fingerprint: String,
    ids: Vec<String>,
    handoffs: BTreeMap<String, BTreeMap<String, String>>,
}

impl Graph {
    fn capture(opening: &OpeningProfiles) -> Self {
        Self {
            fingerprint: opening.fingerprint.clone(),
            ids: opening
                .roster
                .iter()
                .map(|profile| profile.id.clone())
                .collect(),
            handoffs: opening
                .roster
                .iter()
                .map(|profile| (profile.id.clone(), profile.handoffs.clone()))
                .collect(),
        }
    }
}

impl Enrollment {
    /// Call only after the controller checks the exact current native owner.
    pub(super) fn adopt(
        previous: Option<&Self>,
        registry: &AgentProfiles,
        current: &AgentProfileReceipt,
        id: &str,
        name: &str,
    ) -> Result<Self> {
        ensure!(
            current.profile_id == id,
            "Runtime target must be the current Agent"
        );
        ensure!(
            !name.trim().is_empty(),
            "Runtime target requires a nonempty Agent.name"
        );
        ensure!(
            registry.environment_policy_digest.is_none(),
            "Runtime target enrollment does not support environment grants"
        );
        let target = registry
            .profiles
            .get(id)
            .context("Runtime target is outside this Session")?;
        ensure!(
            registry.runtime_handoffs
                && !registry
                    .opening
                    .roster
                    .iter()
                    .any(|profile| profile.id == id),
            "Target is not a newly accepted runtime Agent"
        );
        let origins = registry
            .profiles
            .values()
            .flat_map(|profile| {
                profile
                    .declaration
                    .decision_handoffs
                    .iter()
                    .filter_map(move |(name, tool)| {
                        (!tool.factories.is_empty()
                            && tool.targets.iter().any(|target| target == id))
                        .then_some((&profile.declaration.id, name))
                    })
            })
            .collect::<Vec<_>>();
        ensure!(
            origins.len() == 1,
            "Runtime target has no unique accepted factory source"
        );
        ensure!(
            origins[0].0 == &registry.opening.root_id,
            "Runtime enrollment currently requires the opening root factory"
        );
        let mut result = previous.cloned().unwrap_or_else(|| Self {
            version: 1,
            root_id: registry.opening.root_id.clone(),
            promoted: Vec::new(),
            graphs: Vec::new(),
        });
        ensure!(
            result.version == 1 && result.root_id == registry.opening.root_id,
            "Runtime enrollment has a different opening owner"
        );
        for enrolled in &result.promoted {
            let actual = registry
                .profiles
                .get(&enrolled.profile_id)
                .context("Previously enrolled runtime target has expired")?;
            ensure!(
                declaration_fingerprint(&actual.declaration)? == enrolled.fingerprint,
                "Previously enrolled runtime target declaration has changed"
            );
        }
        if let Some(enrolled) = result.promoted.iter().find(|item| item.profile_id == id) {
            ensure!(
                enrolled.name == name,
                "Runtime target enrollment name has changed"
            );
        } else {
            ensure!(
                !result.promoted.iter().any(|item| item.name == name),
                "Runtime targets require distinct enrollment names"
            );
            let declaration = &target.declaration;
            ensure!(
                declaration.handoffs.is_empty()
                    && declaration.decision_handoffs.is_empty()
                    && declaration.mcp_servers.is_empty()
                    && declaration.environment.is_none(),
                "Runtime target enrollment requires ordinary callback declarations"
            );
            result.promoted.push(Promoted {
                name: name.into(),
                source_id: origins[0].0.clone(),
                tool_name: origins[0].1.clone(),
                profile_id: id.into(),
                fingerprint: declaration_fingerprint(declaration)?,
            });
        }
        if !result
            .graphs
            .iter()
            .any(|graph| graph.fingerprint == registry.opening.fingerprint)
        {
            result.graphs.push(Graph::capture(&registry.opening));
        }
        Ok(result)
    }

    /// Validate every historical graph against current application declarations.
    /// Extra edges may name only explicitly enrolled, currently reachable targets.
    pub(super) fn validate(&self, opening: &OpeningProfiles) -> Result<()> {
        ensure!(
            self.version == 1 && self.root_id == opening.root_id,
            "Runtime enrollment requires the original opening graph"
        );
        let current = opening
            .roster
            .iter()
            .map(|profile| (profile.id.as_str(), profile))
            .collect::<BTreeMap<_, _>>();
        ensure!(
            current.len() == opening.roster.len(),
            "Duplicate current Agent profile"
        );
        let mut promoted = HashSet::new();
        let mut names = HashSet::new();
        for enrolled in &self.promoted {
            ensure!(
                enrolled.source_id == self.root_id,
                "Runtime enrollment requires the original root factory"
            );
            ensure!(
                !enrolled.name.trim().is_empty()
                    && names.insert(enrolled.name.as_str())
                    && promoted.insert(enrolled.profile_id.as_str()),
                "Duplicate runtime enrollment owner"
            );
            let actual = current
                .get(enrolled.profile_id.as_str())
                .context("Runtime enrollment requires a currently reachable target")?;
            ensure!(
                actual.handoffs.is_empty()
                    && actual.decision_handoffs.is_empty()
                    && actual.environment.is_none()
                    && actual.environment_tools.is_none()
                    && actual.mcp_servers.is_empty()
                    && actual.mcp_tools.is_empty()
                    && actual.tools.iter().enumerate().all(|(slot, tool)| {
                        matches!(tool, DynamicToolSpec::Function(_))
                            && actual.tools.configured_agent_role(slot).is_none()
                    }),
                "Runtime enrollment requires ordinary current callback declarations"
            );
            ensure!(
                declaration_fingerprint(actual)? == enrolled.fingerprint,
                "Runtime enrollment target declaration differs from the current Agent"
            );
            let source = current
                .get(enrolled.source_id.as_str())
                .context("Runtime enrollment source is outside the current graph")?;
            ensure!(
                source
                    .decision_handoffs
                    .get(&enrolled.tool_name)
                    .is_some_and(|tool| !tool.factories.is_empty()),
                "Runtime enrollment source requires its current factory declaration"
            );
        }
        ensure!(
            !self.promoted.is_empty() && !self.graphs.is_empty(),
            "Runtime enrollment has no native declaration lineage"
        );
        let base = current
            .keys()
            .copied()
            .filter(|id| !promoted.contains(id))
            .collect::<HashSet<_>>();
        ensure!(
            self.graphs[0]
                .ids
                .iter()
                .map(String::as_str)
                .collect::<HashSet<_>>()
                == base,
            "Runtime enrollment requires its original base graph first"
        );
        for graph in &self.graphs {
            let ids = graph.ids.iter().map(String::as_str).collect::<HashSet<_>>();
            ensure!(
                ids.len() == graph.ids.len()
                    && base.is_subset(&ids)
                    && ids.contains(self.root_id.as_str())
                    && graph.handoffs.len() == ids.len(),
                "Runtime enrollment cannot remove opening Agents"
            );
            let mut projected = Vec::with_capacity(graph.ids.len());
            for id in &graph.ids {
                let actual = current
                    .get(id.as_str())
                    .context("Historical Agent requires its current declaration")?;
                let handoffs = graph
                    .handoffs
                    .get(id)
                    .context("Historical Agent has no captured handoff declarations")?;
                ensure!(
                    handoffs
                        .iter()
                        .all(|(name, target)| actual.handoffs.get(name) == Some(target)),
                    "Historical handoff requires its current declaration"
                );
                ensure!(
                    actual.handoffs.iter().all(|(name, target)| {
                        handoffs.get(name) == Some(target)
                            || (promoted.contains(target.as_str())
                                && !actual.handoff_callbacks.contains_key(name))
                    }),
                    "Runtime enrollment cannot change opening handoffs"
                );
                let mut profile = (*actual).clone();
                profile.handoffs = handoffs.clone();
                projected.push(profile);
            }
            ensure!(
                roster_fingerprint(&projected, &self.root_id)? == graph.fingerprint,
                "Runtime enrollment requires the exact historical graph declarations"
            );
        }
        Ok(())
    }

    pub(super) fn admits_graph(
        &self,
        opening: &OpeningProfiles,
        fingerprint: &str,
    ) -> Result<bool> {
        self.validate(opening)?;
        Ok(self
            .graphs
            .iter()
            .any(|graph| graph.fingerprint == fingerprint))
    }

    pub(super) fn admits_historical_handoff(
        &self,
        source_id: &str,
        tool_name: &str,
        target_id: &str,
    ) -> bool {
        self.promoted.iter().any(|item| {
            item.source_id == source_id
                && item.tool_name == tool_name
                && item.profile_id == target_id
        })
    }

    pub(super) fn check_native_registry(&self, registry: &AgentProfiles) -> Result<()> {
        for enrolled in &self.promoted {
            let actual = registry
                .profiles
                .get(&enrolled.profile_id)
                .context("Enrolled runtime Agent is outside the current native registry")?;
            ensure!(
                declaration_fingerprint(&actual.declaration)? == enrolled.fingerprint,
                "Enrolled runtime Agent declaration has changed"
            );
        }
        Ok(())
    }

    pub(super) fn capture_opening(&mut self, opening: &OpeningProfiles) {
        if !self
            .graphs
            .iter()
            .any(|graph| graph.fingerprint == opening.fingerprint)
        {
            self.graphs.push(Graph::capture(opening));
        }
    }

    pub(super) fn approval_graph(&self) -> &str {
        &self.graphs[0].fingerprint
    }

    pub(super) fn names(&self) -> BTreeMap<&str, &str> {
        self.promoted
            .iter()
            .map(|item| (item.name.as_str(), item.profile_id.as_str()))
            .collect()
    }
}

fn declaration_fingerprint(profile: &ProfileSpec) -> Result<String> {
    roster_fingerprint(&[profile.clone()], &profile.id)
}
