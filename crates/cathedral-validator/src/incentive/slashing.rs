#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RentalLossClassification {
    NodeLoss { reason: String },
    NonSlashable { reason: String },
}

pub fn classify_terminal_rental_loss(
    reason: &str,
    is_container_healthy_failure: bool,
) -> RentalLossClassification {
    if is_container_healthy_failure {
        return RentalLossClassification::NonSlashable {
            reason: reason.to_string(),
        };
    }

    RentalLossClassification::NodeLoss {
        reason: reason.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timeout_style_failures_are_slashable() {
        let classification = classify_terminal_rental_loss("Health check timeout", false);
        assert!(matches!(
            classification,
            RentalLossClassification::NodeLoss { .. }
        ));
    }

    #[test]
    fn unhealthy_container_is_not_slashable() {
        let classification = classify_terminal_rental_loss("Container unhealthy", true);
        assert!(matches!(
            classification,
            RentalLossClassification::NonSlashable { .. }
        ));
    }
}
