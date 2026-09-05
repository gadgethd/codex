use anyhow::Result;
use app_test_support::TestAppServer;
use codex_app_server_protocol::AllowDenyRequirement;
use codex_app_server_protocol::ComputerUseConfig;
use codex_app_server_protocol::ComputerUseLinuxConfig;
use codex_app_server_protocol::ComputerUseLinuxRequirements;
use codex_app_server_protocol::ComputerUseRequirements;
use codex_app_server_protocol::ConfigBatchWriteParams;
use codex_app_server_protocol::ConfigEdit;
use codex_app_server_protocol::ConfigReadParams;
use codex_app_server_protocol::ConfigReadResponse;
use codex_app_server_protocol::ConfigRequirementsReadResponse;
use codex_app_server_protocol::ConfigWriteResponse;
use codex_app_server_protocol::MergeStrategy;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::collections::BTreeMap;
use std::time::Duration;
use tempfile::TempDir;
use tokio::time::timeout;

const READ_TIMEOUT: Duration = Duration::from_secs(/*secs*/ 60);

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn linux_computer_use_rules_round_trip_through_config_rpcs() -> Result<()> {
    let codex_home = TempDir::new()?;
    std::fs::write(
        codex_home.path().join("requirements.toml"),
        "[computer_use.linux.desktop_ids]\n\"code.desktop\" = \"deny\"",
    )?;
    let mut server = TestAppServer::builder()
        .with_codex_home(codex_home.path())
        .build_initialized_with_timeout(READ_TIMEOUT)
        .await?;

    let request_id = server
        .send_config_batch_write_request(ConfigBatchWriteParams {
            edits: vec![ConfigEdit {
                key_path: "computer_use.linux.desktop_ids".to_string(),
                value: json!({"code.desktop": "allow", "org.gnome.TextEditor.desktop": "deny"}),
                merge_strategy: MergeStrategy::Replace,
            }],
            file_path: None,
            expected_version: None,
            reload_user_config: false,
        })
        .await?;
    let _: ConfigWriteResponse = timeout(READ_TIMEOUT, server.read_response(request_id)).await??;

    let request_id = server
        .send_config_read_request(ConfigReadParams {
            include_layers: false,
            cwd: None,
        })
        .await?;
    let response: ConfigReadResponse =
        timeout(READ_TIMEOUT, server.read_response(request_id)).await??;
    assert_eq!(
        response.config.computer_use,
        Some(ComputerUseConfig {
            default_app_access: None,
            linux: Some(ComputerUseLinuxConfig {
                desktop_ids: Some(BTreeMap::from([
                    ("code.desktop".to_string(), AllowDenyRequirement::Allow),
                    (
                        "org.gnome.TextEditor.desktop".to_string(),
                        AllowDenyRequirement::Deny
                    ),
                ]))
            }),
            macos: None,
            windows: None,
        })
    );
    let persisted: toml::Value = toml::from_str(&std::fs::read_to_string(
        codex_home.path().join("config.toml"),
    )?)?;
    assert_eq!(
        serde_json::to_value(&persisted["computer_use"]["linux"])?,
        json!({
            "desktop_ids": {"code.desktop": "allow", "org.gnome.TextEditor.desktop": "deny"}
        })
    );

    let request_id = server.send_config_requirements_read_request().await?;
    let response: ConfigRequirementsReadResponse =
        timeout(READ_TIMEOUT, server.read_response(request_id)).await??;
    assert_eq!(
        response
            .requirements
            .expect("Linux policy must not be treated as empty")
            .computer_use,
        Some(ComputerUseRequirements {
            allow_locked_computer_use: None,
            allow_persistent_approval: None,
            default_app_access: None,
            linux: Some(ComputerUseLinuxRequirements {
                desktop_ids: Some(BTreeMap::from([(
                    "code.desktop".to_string(),
                    AllowDenyRequirement::Deny
                ),]))
            }),
            macos: None,
            windows: None,
        })
    );
    Ok(())
}
