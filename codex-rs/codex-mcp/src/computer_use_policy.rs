//! Host-owned Linux application policy for native stdio computer-use tools.
//!
//! Policy follows the exact configuration captured by a prepared call. It is
//! transport metadata, never a model argument or a model-visible context item.

use crate::server::McpServerMetadata;
use crate::server::McpServerOrigin;
use crate::tools::ToolInfo;
use codex_config::AllowDenyRequirementToml;
use codex_config::ComputerUseConfigToml;
use codex_config::ConfigLayerStack;
use serde::Serialize;
use serde_json::Map;
use serde_json::Value;
use std::collections::BTreeMap;

pub(crate) const POLICY_KEY: &str = "codex/linuxComputerUsePolicy";
const REQUEST_POLICY_KEY: &str = "codex/linuxComputerUse";
const MAX_APPLICATIONS: usize = 256;
const MAX_DESKTOP_ID_BYTES: usize = 512;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ComputerUsePolicy {
    version: u32,
    enabled: bool,
    default_app_access: AllowDenyRequirementToml,
    desktop_ids: BTreeMap<String, AllowDenyRequirementToml>,
    allow_locked_computer_use: bool,
}

impl ComputerUsePolicy {
    fn denied() -> Self {
        Self {
            version: 1,
            enabled: false,
            default_app_access: AllowDenyRequirementToml::Deny,
            desktop_ids: BTreeMap::new(),
            allow_locked_computer_use: false,
        }
    }

    fn from_layers(layers: &ConfigLayerStack) -> Self {
        let requirements = layers.requirements_toml();
        if requirements.allow_browser_and_computer_use == Some(false) {
            return Self::denied();
        }
        let config: ComputerUseConfigToml = match layers.effective_config().get("computer_use") {
            Some(value) => match value.clone().try_into() {
                Ok(config) => config,
                Err(_) => return Self::denied(),
            },
            None => ComputerUseConfigToml::default(),
        };
        let managed = requirements.computer_use.clone().unwrap_or_default();
        let config_default = config
            .default_app_access
            .unwrap_or(AllowDenyRequirementToml::Allow);
        let managed_default = managed
            .default_app_access
            .unwrap_or(AllowDenyRequirementToml::Allow);
        let config_apps = config
            .linux
            .and_then(|linux| linux.desktop_ids)
            .unwrap_or_default();
        let managed_apps = managed
            .linux
            .and_then(|linux| linux.desktop_ids)
            .unwrap_or_default();
        let mut desktop_ids = BTreeMap::new();
        for id in config_apps.keys().chain(managed_apps.keys()) {
            if id.len() > MAX_DESKTOP_ID_BYTES {
                return Self::denied();
            }
            let access = if config_apps.get(id).copied().unwrap_or(config_default)
                == AllowDenyRequirementToml::Allow
                && managed_apps.get(id).copied().unwrap_or(managed_default)
                    == AllowDenyRequirementToml::Allow
            {
                AllowDenyRequirementToml::Allow
            } else {
                AllowDenyRequirementToml::Deny
            };
            desktop_ids.insert(id.clone(), access);
            if desktop_ids.len() > MAX_APPLICATIONS {
                return Self::denied();
            }
        }
        Self {
            version: 1,
            enabled: true,
            default_app_access: if config_default == AllowDenyRequirementToml::Allow
                && managed_default == AllowDenyRequirementToml::Allow
            {
                AllowDenyRequirementToml::Allow
            } else {
                AllowDenyRequirementToml::Deny
            },
            desktop_ids,
            allow_locked_computer_use: managed.allow_locked_computer_use == Some(true),
        }
    }
}

pub(crate) fn add_computer_use_policy(
    tool: &ToolInfo,
    server: &McpServerMetadata,
    layers: &ConfigLayerStack,
    meta: Option<Value>,
) -> Option<Value> {
    let requested = tool
        .tool
        .meta
        .as_deref()
        .and_then(|meta| meta.get(REQUEST_POLICY_KEY))
        .and_then(Value::as_bool)
        == Some(true)
        && matches!(server.origin, Some(McpServerOrigin::Stdio));
    if meta.is_none() && !requested {
        return None;
    }
    let mut meta = match meta {
        Some(Value::Object(meta)) => meta,
        None => Map::new(),
        other => return other,
    };
    // Callers cannot override the captured host configuration, even on tools
    // that do not request policy or on remote HTTP servers.
    meta.remove(POLICY_KEY);
    if requested {
        meta.insert(
            POLICY_KEY.to_string(),
            serde_json::json!(ComputerUsePolicy::from_layers(layers)),
        );
    }
    Some(Value::Object(meta))
}

#[cfg(test)]
#[path = "computer_use_policy_tests.rs"]
mod tests;
