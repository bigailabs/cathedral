use super::client::{K3S_TOKEN_PREFIX, MAX_DESCRIPTION_LENGTH, TOKEN_FORMAT_REGEX, TTL_REGEX};

#[test]
fn test_ttl_regex() {
    assert!(TTL_REGEX.is_match("24h"));
    assert!(TTL_REGEX.is_match("1h"));
    assert!(TTL_REGEX.is_match("30m"));
    assert!(TTL_REGEX.is_match("3600s"));
    assert!(TTL_REGEX.is_match("0h"));

    assert!(!TTL_REGEX.is_match("24"));
    assert!(!TTL_REGEX.is_match("24hours"));
    assert!(!TTL_REGEX.is_match("1d"));
    assert!(!TTL_REGEX.is_match("1H"));
    assert!(!TTL_REGEX.is_match(""));
    assert!(!TTL_REGEX.is_match("h"));
}

#[test]
fn test_description_validation() {
    let valid_desc = "GPU node test-1";
    assert!(!valid_desc.contains('\''));
    assert!(!valid_desc.contains('\\'));
    assert!(!valid_desc.contains('$'));
    assert!(!valid_desc.contains('`'));
    assert!(valid_desc.len() <= MAX_DESCRIPTION_LENGTH);

    let valid_desc2 = "GPU node for datacenter github-434149";
    assert!(!valid_desc2.contains('\''));
    assert!(valid_desc2.len() <= MAX_DESCRIPTION_LENGTH);

    let invalid_desc1 = "'; rm -rf /";
    assert!(invalid_desc1.contains('\''));

    let invalid_desc2 = "test'test";
    assert!(invalid_desc2.contains('\''));

    let invalid_desc3 = "test$var";
    assert!(invalid_desc3.contains('$'));

    let invalid_desc4 = "test`cmd`";
    assert!(invalid_desc4.contains('`'));

    let long_desc = "x".repeat(201);
    assert!(long_desc.len() > MAX_DESCRIPTION_LENGTH);
}

#[test]
fn test_token_format() {
    let token1 = "K1029085c2d48e037ece1c19cc69628dd55bca8d322de0bcdc9d988d2db28684188::abc123.def456ghi789jklm";
    assert!(token1.starts_with(K3S_TOKEN_PREFIX));
    assert!(TOKEN_FORMAT_REGEX.is_match(token1));

    let parts: Vec<&str> = token1.split("::").collect();
    assert_eq!(parts.len(), 2);
    let token_parts: Vec<&str> = parts[1].split('.').collect();
    assert_eq!(token_parts.len(), 2);
    assert_eq!(token_parts[0], "abc123");

    let token2 = "K10ed46401b634415dfdcc79352ec79f01fe2bd3b67f451c882ed014efe47a52b36::z8nqu8.p2xw47vbnm3ktsdc";
    assert!(token2.starts_with(K3S_TOKEN_PREFIX));
    assert!(TOKEN_FORMAT_REGEX.is_match(token2));

    assert!(!TOKEN_FORMAT_REGEX.is_match("invalid"));
    assert!(!TOKEN_FORMAT_REGEX.is_match("K10hash"));
    assert!(!TOKEN_FORMAT_REGEX.is_match(""));
    assert!(!TOKEN_FORMAT_REGEX.is_match("K1029085c2d48e037ece1c19cc69628dd55bca8d322de0bcdc9d988d2db28684188::server:480d1c4e8d555121c6042508e4e605d8"));
}

#[test]
fn test_token_id_validation() {
    assert!("abc123".chars().all(|c| c.is_alphanumeric() || c == '-'));
    assert!("abc123def".chars().all(|c| c.is_alphanumeric() || c == '-'));
    assert!("ABC123DEF".chars().all(|c| c.is_alphanumeric() || c == '-'));
    assert!("z8nqu8".chars().all(|c| c.is_alphanumeric() || c == '-'));
    assert!("abc-123".chars().all(|c| c.is_alphanumeric() || c == '-'));

    assert!(!"abc.123".chars().all(|c| c.is_alphanumeric() || c == '-'));
    assert!(!"abc_123".chars().all(|c| c.is_alphanumeric() || c == '-'));
    assert!(!"abc 123".chars().all(|c| c.is_alphanumeric() || c == '-'));

    assert!("abc".len() < 6);
    assert!("abcde".len() < 6);
    assert!("a".repeat(17).len() > 16);
}
