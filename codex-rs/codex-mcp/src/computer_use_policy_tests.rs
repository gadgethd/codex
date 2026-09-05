use super::*;
use crate::PreparedMcpCall;
use crate::connection_manager::McpConnectionSet;
use crate::rmcp_client::ManagedClient;
use codex_config::ConfigLayerEntry;
use codex_config::ConfigLayerSource;
use codex_config::ConfigRequirements;
use codex_protocol::models::PermissionProfile;
use codex_rmcp_client::InProcessTransportFactory;
use codex_rmcp_client::RmcpClient;
use futures::FutureExt;
use futures::future::BoxFuture;
use pretty_assertions::assert_eq;
use rmcp::ServerHandler;
use rmcp::ServiceExt;
use rmcp::model::CallToolRequestParams;
use rmcp::model::CallToolResponse;
use rmcp::model::CallToolResult;
use rmcp::model::ClientCapabilities;
use rmcp::model::Implementation;
use rmcp::model::InitializeRequestParams;
use rmcp::model::MetaObject;
use rmcp::model::ServerCapabilities;
use rmcp::model::ServerInfo;
use rmcp::model::Tool;
use rmcp::service::RequestContext;
use rmcp::service::RoleServer;
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tokio::io::DuplexStream;
use tokio::sync::RwLock;

fn layers(config: Value, requirements: Value) -> ConfigLayerStack {
    ConfigLayerStack::new(
        vec![ConfigLayerEntry::new(
            ConfigLayerSource::SessionFlags,
            serde_json::from_value(config).expect("config layer"),
        )],
        ConfigRequirements::default(),
        serde_json::from_value(requirements).expect("requirements"),
    )
    .expect("layer stack")
}

#[test]
fn application_overrides_respect_both_user_and_managed_defaults() {
    let policy = ComputerUsePolicy::from_layers(&layers(
        json!({"computer_use": {
            "default_app_access": "deny",
            "linux": {"desktop_ids": {"editor.desktop": "allow", "blocked.desktop": "allow"}}
        }}),
        json!({"computer_use": {
            "default_app_access": "allow",
            "linux": {"desktop_ids": {"blocked.desktop": "deny", "managed.desktop": "allow"}},
            "allow_locked_computer_use": true
        }}),
    ));
    assert_eq!(
        serde_json::to_value(policy).expect("policy"),
        json!({
            "version": 1, "enabled": true, "defaultAppAccess": "deny",
            "desktopIds": {"editor.desktop": "allow", "blocked.desktop": "deny", "managed.desktop": "deny"},
            "allowLockedComputerUse": true
        })
    );
}

#[test]
fn invalid_or_oversized_policies_fail_closed() {
    let apps: Map<String, Value> = (0..=MAX_APPLICATIONS)
        .map(|index| (format!("app{index}.desktop"), json!("allow")))
        .collect();
    let long_id = "x".repeat(MAX_DESKTOP_ID_BYTES + 1);
    for (config, requirements) in [
        (json!({}), json!({"allow_browser_and_computer_use": false})),
        (
            json!({"computer_use": {"default_app_access": "invalid"}}),
            json!({}),
        ),
        (
            json!({"computer_use": {"linux": {"desktop_ids": apps}}}),
            json!({}),
        ),
        (
            json!({}),
            json!({"computer_use": {"linux": {"desktop_ids": {long_id: "allow"}}}}),
        ),
    ] {
        assert_eq!(
            serde_json::to_value(ComputerUsePolicy::from_layers(&layers(
                config,
                requirements
            )))
            .expect("policy"),
            json!({"version": 1, "enabled": false, "defaultAppAccess": "deny", "desktopIds": {}, "allowLockedComputerUse": false})
        );
    }
}

#[derive(Clone)]
struct RecordingServer(Arc<Mutex<Vec<Value>>>);

impl ServerHandler for RecordingServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
    }

    async fn call_tool(
        &self,
        _request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, rmcp::ErrorData> {
        self.0
            .lock()
            .expect("record metadata")
            .push(serde_json::to_value(context.meta).expect("metadata"));
        Ok(CallToolResult::success(Vec::new()).into())
    }
}

impl InProcessTransportFactory for RecordingServer {
    fn open(&self) -> BoxFuture<'static, std::io::Result<DuplexStream>> {
        let server = self.clone();
        async move {
            let (client, transport) = tokio::io::duplex(/*max_buf_size*/ 4096);
            tokio::spawn(async move {
                server
                    .serve(transport)
                    .await
                    .expect("serve")
                    .waiting()
                    .await
                    .expect("wait");
            });
            Ok(client)
        }
        .boxed()
    }
}

#[tokio::test]
async fn prepared_calls_replace_forged_metadata_only_for_native_stdio_tools() -> anyhow::Result<()>
{
    let calls = Arc::new(Mutex::new(Vec::new()));
    let client = Arc::new(
        RmcpClient::new_in_process_client(Arc::new(RecordingServer(Arc::clone(&calls)))).await?,
    );
    client
        .initialize(
            InitializeRequestParams::new(
                ClientCapabilities::default(),
                Implementation::new("test", "1"),
            ),
            Some(Duration::from_secs(/*secs*/ 5)),
            Box::new(|_, _| async { anyhow::bail!("unexpected elicitation") }.boxed()),
        )
        .await?;
    let mut config = crate::mcp::tests::test_mcp_config(std::env::temp_dir());
    config.config_layer_stack = layers(json!({}), json!({"allow_browser_and_computer_use": false}));
    config
        .server_permission_profiles
        .insert("native".to_string(), PermissionProfile::default());
    let config = Arc::new(config);
    let manager = Arc::new(McpConnectionSet::empty(/*prefix_mcp_tool_names*/ true));
    let mut tool = Tool::new("native", "native desktop", Arc::new(Map::new()));
    let mut tool_meta = MetaObject::new();
    tool_meta.insert(REQUEST_POLICY_KEY.to_string(), json!(true));
    tool.meta = Some(tool_meta);
    let tool = ToolInfo {
        server_name: "native".to_string(),
        supports_parallel_tool_calls: false,
        server_origin: None,
        callable_name: "native".to_string(),
        callable_namespace: "native".to_string(),
        namespace_description: None,
        tool,
        openai_file_input_optional_fields: HashMap::new(),
        connector_id: None,
        connector_name: None,
        plugin_display_names: Vec::new(),
    };
    let managed_client = Arc::new(ManagedClient {
        client: Arc::clone(&client),
        server_info: codex_protocol::mcp::McpServerInfo {
            name: "native".to_string(),
            title: None,
            version: "1".to_string(),
            description: None,
            icons: None,
            website_url: None,
        },
        tools: vec![tool.clone()],
        tool_timeout: None,
        server_instructions: None,
        server_supports_sandbox_state_meta_capability: false,
        codex_apps_tools_cache_context: None,
    });
    for origin in [
        McpServerOrigin::Stdio,
        McpServerOrigin::StreamableHttp("https://example.test".to_string()),
    ] {
        let prepared = PreparedMcpCall::new(
            Arc::clone(&manager),
            Arc::clone(&managed_client),
            Arc::clone(&config),
            /*catalog_revision*/ 0,
            Arc::new(RwLock::new(/*value*/ 0)),
            tool.clone(),
            McpServerMetadata {
                environment_id: "local".to_string(),
                pollutes_memory: true,
                origin: Some(origin),
                supports_parallel_tool_calls: false,
                default_tools_approval_mode: None,
                tool_approval_modes: HashMap::new(),
            },
            /*plugin_id*/ None,
            /*selected_plugin_server*/ false,
        )
        .expect("prepared call");
        prepared
            .call(
                /*arguments*/ None,
                Some(json!({POLICY_KEY: {"enabled": true}, "caller": "preserved"})),
                Some(Duration::from_secs(/*secs*/ 5)),
            )
            .await?;
    }
    client.shutdown().await;
    assert_eq!(
        *calls.lock().expect("captured calls"),
        vec![
            json!({POLICY_KEY: {"version": 1, "enabled": false, "defaultAppAccess": "deny", "desktopIds": {}, "allowLockedComputerUse": false}, "caller": "preserved", "progressToken": 0}),
            json!({"caller": "preserved", "progressToken": 1}),
        ]
    );
    Ok(())
}
