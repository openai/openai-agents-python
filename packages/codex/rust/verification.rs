//! Explicit application capabilities, captured once before native thread publication.
use codex_config::{McpServerAuth, McpServerTransportConfig};
use codex_core_api::Config;
use codex_extension_api::{
    ExtensionFuture, McpServerContribution, McpServerContributionContext, McpServerContributor,
};
use codex_mcp::{APPLICATION_APPS_CONTRIBUTOR_ID, ApplicationApps};

pub struct Contributor;
impl McpServerContributor<Config> for Contributor {
    fn id(&self) -> &'static str {
        APPLICATION_APPS_CONTRIBUTOR_ID
    }
    fn contribute<'a>(
        &'a self,
        context: McpServerContributionContext<'a, Config>,
    ) -> ExtensionFuture<'a, Vec<McpServerContribution>> {
        Box::pin(async move {
            let Some(application) = context
                .thread_init()
                .and_then(|init| init.get::<ApplicationApps>())
            else {
                return Vec::new();
            };
            let Some(current) = context
                .config()
                .mcp_servers
                .get()
                .get("codex_apps")
                .cloned()
            else {
                return Vec::new();
            };
            let mut current = current;
            if application.personal_auth().is_some() {
                // Public ChatGPTAppsMCP has one fixed native target and no header/bearer knobs.
                let McpServerTransportConfig::StreamableHttp {
                    url,
                    bearer_token_env_var: None,
                    http_headers: None,
                    env_http_headers: None,
                    http_headers_helper: None,
                } = &current.transport
                else {
                    return Vec::new();
                };
                if current.auth != McpServerAuth::ChatGpt
                    || !current.is_local_environment()
                    || url != "https://chatgpt.com/backend-api/ps/mcp"
                {
                    return Vec::new();
                }
                current.transport = application.registration().transport.clone();
                current.auth = application.registration().auth.clone();
                current.environment_id = application.registration().environment_id.clone();
            } else if current.transport != application.registration().transport
                || current.auth != application.registration().auth
                || current.environment_id != application.registration().environment_id
            {
                return Vec::new();
            }
            vec![McpServerContribution::HostedApps {
                config: Box::new(current),
                protocol_mode: None,
            }]
        })
    }
}
