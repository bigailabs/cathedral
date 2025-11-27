//! Deterministic WireGuard IP allocation from node_id
//!
//! Uses SHA-256 hash to map node_id to a unique IP in the 10.200.x.y range.
//! This approach requires no database state and is completely deterministic.

use sha2::{Digest, Sha256};

/// Validate that a WireGuard public key has valid base64 format.
///
/// WireGuard public keys are 32 bytes encoded as base64, resulting in exactly
/// 44 characters. This validation ensures the key contains only valid base64
/// characters (A-Z, a-z, 0-9, +, /, =) to prevent injection attacks.
///
/// # Arguments
/// * `public_key` - The WireGuard public key to validate
///
/// # Returns
/// `true` if the key is exactly 44 characters and contains only valid base64 characters
pub fn is_valid_wireguard_public_key(public_key: &str) -> bool {
    if public_key.len() != 44 {
        return false;
    }
    public_key
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '+' || c == '/' || c == '=')
}

/// Allocate a deterministic WireGuard IP from node_id.
///
/// Uses SHA-256 hash to map node_id to IP in 10.200.X.Y range:
/// - X (third octet): 1-254 (avoids 0 which is reserved for server subnet)
/// - Y (fourth octet): 1-254 (avoids .0 network and .255 broadcast)
///
/// # Arguments
/// * `node_id` - The unique identifier for the GPU node
///
/// # Returns
/// A string in the format "10.200.X.Y"
///
/// # Example
/// ```
/// use basilica_api::wireguard::allocate_wireguard_ip;
///
/// let ip = allocate_wireguard_ip("evan-test-40");
/// assert!(ip.starts_with("10.200."));
/// ```
pub fn allocate_wireguard_ip(node_id: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(node_id.as_bytes());
    let hash = hasher.finalize();

    // Use first two bytes for IP octets
    // Third octet: 1-254 (avoid 0 to not conflict with server subnet 10.200.0.x)
    let third_octet = (hash[0] % 254) + 1; // 1-254
                                           // Fourth octet: 1-254 (avoid .0 network and .255 broadcast)
    let fourth_octet = (hash[1] % 254) + 1; // 1-254

    format!("10.200.{}.{}", third_octet, fourth_octet)
}

/// Validate that an IP is within the WireGuard GPU node range
pub fn is_valid_gpu_node_ip(ip: &str) -> bool {
    let parts: Vec<&str> = ip.split('.').collect();
    if parts.len() != 4 {
        return false;
    }

    let octets: Result<Vec<u8>, _> = parts.iter().map(|s| s.parse::<u8>()).collect();
    match octets {
        Ok(o) if o.len() == 4 => {
            // Must be in 10.200.x.y range
            // Third octet must be 1-254 (0 is server subnet, 255 reserved)
            // Fourth octet must be 1-254 (0 is network, 255 is broadcast)
            o[0] == 10 && o[1] == 200 && o[2] >= 1 && o[2] <= 254 && o[3] >= 1 && o[3] <= 254
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn test_allocate_wireguard_ip_deterministic() {
        let ip1 = allocate_wireguard_ip("evan-test-40");
        let ip2 = allocate_wireguard_ip("evan-test-40");
        assert_eq!(ip1, ip2, "Same node_id should produce same IP");
    }

    #[test]
    fn test_allocate_wireguard_ip_different_nodes() {
        let ip1 = allocate_wireguard_ip("node-a");
        let ip2 = allocate_wireguard_ip("node-b");
        assert_ne!(ip1, ip2, "Different node_ids should produce different IPs");
    }

    #[test]
    fn test_allocate_wireguard_ip_valid_range() {
        // Test many node IDs to verify range constraints
        for i in 0..1000 {
            let node_id = format!("test-node-{}", i);
            let ip = allocate_wireguard_ip(&node_id);

            let parts: Vec<u8> = ip.split('.').map(|s| s.parse().unwrap()).collect();

            assert_eq!(parts[0], 10, "First octet should be 10");
            assert_eq!(parts[1], 200, "Second octet should be 200");
            assert!(
                parts[2] >= 1 && parts[2] <= 254,
                "Third octet should be 1-254, got {}",
                parts[2]
            );
            assert!(
                parts[3] >= 1 && parts[3] <= 254,
                "Fourth octet should be 1-254, got {}",
                parts[3]
            );
        }
    }

    #[test]
    fn test_allocate_wireguard_ip_distribution() {
        // Test that IPs are reasonably distributed (not all in same subnet)
        let mut third_octets: HashSet<u8> = HashSet::new();

        for i in 0..100 {
            let node_id = format!("distribution-test-{}", i);
            let ip = allocate_wireguard_ip(&node_id);
            let parts: Vec<u8> = ip.split('.').map(|s| s.parse().unwrap()).collect();
            third_octets.insert(parts[2]);
        }

        // Should have at least 20 different third octets from 100 nodes
        assert!(
            third_octets.len() >= 20,
            "IP distribution should be spread across multiple subnets, got {} unique third octets",
            third_octets.len()
        );
    }

    #[test]
    fn test_allocate_wireguard_ip_specific_nodes() {
        // Test some specific node IDs for reproducibility
        let ip = allocate_wireguard_ip("evan-test-40");
        assert!(ip.starts_with("10.200."), "IP should start with 10.200.");

        // Verify it's valid
        assert!(is_valid_gpu_node_ip(&ip));
    }

    #[test]
    fn test_is_valid_gpu_node_ip() {
        // Valid IPs
        assert!(is_valid_gpu_node_ip("10.200.1.1"));
        assert!(is_valid_gpu_node_ip("10.200.254.254"));
        assert!(is_valid_gpu_node_ip("10.200.100.50"));

        // Invalid IPs
        assert!(!is_valid_gpu_node_ip("10.200.0.1")); // Third octet is 0 (server subnet)
        assert!(!is_valid_gpu_node_ip("10.200.255.1")); // Third octet is 255 (reserved)
        assert!(!is_valid_gpu_node_ip("10.200.1.0")); // Fourth octet is 0 (network)
        assert!(!is_valid_gpu_node_ip("10.200.1.255")); // Fourth octet is 255 (broadcast)
        assert!(!is_valid_gpu_node_ip("10.201.1.1")); // Wrong second octet
        assert!(!is_valid_gpu_node_ip("192.168.1.1")); // Wrong network
        assert!(!is_valid_gpu_node_ip("invalid")); // Not an IP
        assert!(!is_valid_gpu_node_ip("10.200.1")); // Missing octet
    }

    #[test]
    fn test_is_valid_wireguard_public_key() {
        // Valid WireGuard public keys (44 char base64)
        // Real WireGuard public key format: 32 bytes -> 44 base64 chars with padding
        assert!(is_valid_wireguard_public_key(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn+/=="
        ));
        assert!(is_valid_wireguard_public_key(
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcd+/=="
        ));

        // Invalid: wrong length
        assert!(!is_valid_wireguard_public_key("tooshort"));
        assert!(!is_valid_wireguard_public_key(
            "dGVzdGtleXRoYXRpc3dheXRvb2xvbmdhbmRub3R2YWxpZGJhc2U2NA=="
        ));

        // Invalid: contains shell metacharacters
        assert!(!is_valid_wireguard_public_key(
            "AAAA'$(rm -rf /)AAAAAAAAAAAAAAAAAAAAAAA"
        ));
        assert!(!is_valid_wireguard_public_key(
            "AAAA`whoami`AAAAAAAAAAAAAAAAAAAAAAAAAA"
        ));
        assert!(!is_valid_wireguard_public_key(
            "AAAA;rm -rf /AAAAAAAAAAAAAAAAAAAAAAAA"
        ));
        assert!(!is_valid_wireguard_public_key(
            "AAAA|cat /etc/passwdAAAAAAAAAAAAAAAAA"
        ));

        // Invalid: empty
        assert!(!is_valid_wireguard_public_key(""));
    }
}
