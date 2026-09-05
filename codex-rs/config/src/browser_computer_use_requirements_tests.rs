use crate::AllowDenyRequirementToml;
use crate::BrowserUseRequirementsToml;
use crate::ComputerUseLinuxRequirementsToml;
use crate::ComputerUseRequirementsToml;
use crate::ConfigRequirementsToml;
use pretty_assertions::assert_eq;
use std::collections::BTreeMap;

#[test]
fn linux_requirements_preserve_rules_and_empty_tables() {
    for (contents, expected_linux, empty) in [
        ("[computer_use]", None, true),
        (
            "[computer_use.linux]",
            Some(ComputerUseLinuxRequirementsToml { desktop_ids: None }),
            true,
        ),
        (
            "[computer_use.linux.desktop_ids]",
            Some(ComputerUseLinuxRequirementsToml {
                desktop_ids: Some(BTreeMap::new()),
            }),
            true,
        ),
        (
            "[computer_use.linux.desktop_ids]\n\"code.desktop\" = \"deny\"\n\"org.gnome.TextEditor.desktop\" = \"allow\"",
            Some(ComputerUseLinuxRequirementsToml {
                desktop_ids: Some(BTreeMap::from([
                    ("code.desktop".to_string(), AllowDenyRequirementToml::Deny),
                    (
                        "org.gnome.TextEditor.desktop".to_string(),
                        AllowDenyRequirementToml::Allow,
                    ),
                ])),
            }),
            false,
        ),
    ] {
        let requirements: ConfigRequirementsToml = toml::from_str(contents).unwrap();
        assert_eq!(
            requirements,
            ConfigRequirementsToml {
                computer_use: Some(ComputerUseRequirementsToml {
                    linux: expected_linux,
                    ..Default::default()
                }),
                ..Default::default()
            }
        );
        assert_eq!(requirements.is_empty(), empty, "{contents}");
    }
}

#[test]
fn linux_requirements_reject_invalid_access() {
    for value in ["true", "1", "\"prompt\"", "[]"] {
        let contents = format!("[computer_use.linux.desktop_ids]\n\"code.desktop\" = {value}");
        assert!(toml::from_str::<ConfigRequirementsToml>(&contents).is_err());
    }
}

#[test]
fn webmcp_requirements_preserve_explicit_values_and_omission() {
    for (contents, expected_browser_use, expected_empty) in [
        ("", None, true),
        ("[browser_use]", Some(None), true),
        (
            "[browser_use]\nallow_webmcp = true",
            Some(Some(true)),
            false,
        ),
        (
            "[browser_use]\nallow_webmcp = false",
            Some(Some(false)),
            false,
        ),
    ] {
        let requirements: ConfigRequirementsToml =
            toml::from_str(contents).expect("parse managed WebMCP policy");
        assert_eq!(
            requirements,
            ConfigRequirementsToml {
                browser_use: expected_browser_use.map(|allow_webmcp| BrowserUseRequirementsToml {
                    allow_webmcp,
                    ..Default::default()
                }),
                ..Default::default()
            },
        );
        assert_eq!(requirements.is_empty(), expected_empty, "{contents}");
    }
}

#[test]
fn webmcp_requirements_reject_non_booleans() {
    for value in ["\"true\"", "1", "[]"] {
        let contents = format!("[browser_use]\nallow_webmcp = {value}");
        let error = toml::from_str::<ConfigRequirementsToml>(&contents)
            .expect_err("WebMCP policy must be a boolean");
        assert!(error.to_string().contains("allow_webmcp"));
    }
}
