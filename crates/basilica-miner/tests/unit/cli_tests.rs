//! Unit tests for CLI functionality

use basilica_common::config::DatabaseConfig;
use basilica_miner::cli::{
    display_node_details, display_nodes_table, handle_command, AddNodeArgs, Command,
    GenerateConfigArgs, ListNodesArgs, MinerArgs, RemoveNodeArgs, StatusArgs,
    UpdateNodeArgs, ValidatorCommand,
};
use basilica_miner::config::{AppConfig, NodeConfig};
use basilica_miner::persistence::RegistrationDb;
use std::path::PathBuf;
use std::time::Duration;
use tempfile::NamedTempFile;

#[test]
fn test_miner_args_default() {
    let args = MinerArgs {
        config: Some(PathBuf::from("config.toml")),
        command: None,
    };

    assert_eq!(args.config, Some(PathBuf::from("config.toml")));
    assert!(args.command.is_none());
}

#[test]
fn test_command_variants() {
    // Test ListNodes command
    let cmd = Command::ListNodes(ListNodesArgs {
        active_only: true,
        format: Some("json".to_string()),
    });

    match cmd {
        Command::ListNodes(args) => {
            assert!(args.active_only);
            assert_eq!(args.format, Some("json".to_string()));
        }
        _ => panic!("Wrong command type"),
    }

    // Test AddNode command
    let cmd = Command::AddNode(AddNodeArgs {
        id: "exec1".to_string(),
        grpc_address: "127.0.0.1:50051".to_string(),
        name: Some("Test Node".to_string()),
        metadata: None,
    });

    match cmd {
        Command::AddNode(args) => {
            assert_eq!(args.id, "exec1");
            assert_eq!(args.grpc_address, "127.0.0.1:50051");
            assert_eq!(args.name, Some("Test Node".to_string()));
        }
        _ => panic!("Wrong command type"),
    }
}

#[tokio::test]
async fn test_handle_list_nodes_empty() {
    let db = create_test_db().await;
    let config = create_test_config();

    let args = ListNodesArgs {
        active_only: false,
        format: None,
    };

    // Should not panic even with empty node list
    let result = handle_command(Command::ListNodes(args), &config, &db).await;
    assert!(result.is_ok());
}

#[tokio::test]
async fn test_handle_add_node() {
    let db = create_test_db().await;
    let config = create_test_config();

    let args = AddNodeArgs {
        id: "exec1".to_string(),
        grpc_address: "127.0.0.1:50051".to_string(),
        name: Some("Test Node".to_string()),
        metadata: Some("{\"gpu\": \"RTX 4090\"}".to_string()),
    };

    let result = handle_command(Command::AddNode(args), &config, &db).await;
    assert!(result.is_ok());

    // Verify node was added
    let node = db.get_node("exec1").await.unwrap();
    assert!(node.is_some());
}

#[tokio::test]
async fn test_handle_remove_node() {
    let db = create_test_db().await;
    let config = create_test_config();

    // First add an node
    db.register_node("exec1", "127.0.0.1:50051", serde_json::json!({}))
        .await
        .unwrap();

    let args = RemoveNodeArgs {
        id: "exec1".to_string(),
        force: false,
    };

    let result = handle_command(Command::RemoveNode(args), &config, &db).await;
    assert!(result.is_ok());

    // Verify node is inactive
    let node = db.get_node("exec1").await.unwrap();
    assert!(node.is_some());
    assert!(!node.unwrap().is_active);
}

#[tokio::test]
async fn test_handle_update_node() {
    let db = create_test_db().await;
    let config = create_test_config();

    // First add an node
    db.register_node("exec1", "127.0.0.1:50051", serde_json::json!({}))
        .await
        .unwrap();

    let args = UpdateNodeArgs {
        id: "exec1".to_string(),
        grpc_address: Some("127.0.0.1:50052".to_string()),
        name: Some("Updated Node".to_string()),
        metadata: None,
    };

    let result = handle_command(Command::UpdateNode(args), &config, &db).await;
    assert!(result.is_ok());
}

#[tokio::test]
async fn test_handle_status() {
    let db = create_test_db().await;
    let config = create_test_config();

    let args = StatusArgs {
        detailed: false,
        json: false,
    };

    let result = handle_command(Command::Status(args), &config, &db).await;
    assert!(result.is_ok());
}

#[tokio::test]
async fn test_handle_generate_config() {
    let temp_file = NamedTempFile::new().unwrap();
    let output_path = temp_file.path().to_path_buf();

    let db = create_test_db().await;
    let config = create_test_config();

    let args = GenerateConfigArgs {
        output: output_path.clone(),
        force: true,
    };

    let result = handle_command(Command::GenerateConfig(args), &config, &db).await;
    assert!(result.is_ok());

    // Verify file was created
    assert!(output_path.exists());
}

#[tokio::test]
async fn test_handle_validator_list() {
    let db = create_test_db().await;
    let config = create_test_config();

    let cmd = ValidatorCommand::List;

    let result = handle_command(Command::Validator(cmd), &config, &db).await;
    assert!(result.is_ok());
}

#[test]
fn test_display_nodes_table() {
    let nodes = vec![
        basilica_miner::persistence::NodeRecord {
            id: "exec1".to_string(),
            grpc_address: "127.0.0.1:50051".to_string(),
            is_active: true,
            is_healthy: true,
            last_seen: chrono::Utc::now(),
            metadata: serde_json::json!({"name": "Node 1"}),
        },
        basilica_miner::persistence::NodeRecord {
            id: "exec2".to_string(),
            grpc_address: "127.0.0.1:50052".to_string(),
            is_active: true,
            is_healthy: false,
            last_seen: chrono::Utc::now() - chrono::Duration::minutes(5),
            metadata: serde_json::json!({"name": "Node 2"}),
        },
    ];

    // Should not panic
    display_nodes_table(&nodes);
}

#[test]
fn test_display_node_details() {
    let node = basilica_miner::persistence::NodeRecord {
        id: "exec1".to_string(),
        grpc_address: "127.0.0.1:50051".to_string(),
        is_active: true,
        is_healthy: true,
        last_seen: chrono::Utc::now(),
        metadata: serde_json::json!({
            "name": "Test Node",
            "gpu": "RTX 4090",
            "location": "US-East"
        }),
    };

    // Should not panic
    display_node_details(&node);
}

// Helper functions

async fn create_test_db() -> RegistrationDb {
    let db_config = DatabaseConfig {
        url: "sqlite::memory:".to_string(),
        max_connections: 5,
        min_connections: 1,
        connection_timeout: Duration::from_secs(10),
        idle_timeout: Duration::from_secs(300),
        max_lifetime: Duration::from_secs(3600),
    };

    RegistrationDb::new(&db_config).await.unwrap()
}

fn create_test_config() -> AppConfig {
    AppConfig::default()
}
