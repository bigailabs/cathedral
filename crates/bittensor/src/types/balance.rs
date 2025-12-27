//! # Balance Type
//!
//! TAO/RAO balance representation with arithmetic operations.
//!
//! The Bittensor network uses two units:
//! - **TAO**: The main currency unit (like BTC)
//! - **RAO**: The smallest unit (1 TAO = 10^9 RAO, like satoshis)

use serde::{Deserialize, Serialize};
use std::fmt;
use std::ops::{Add, AddAssign, Div, Mul, Sub, SubAssign};

/// Number of RAO per TAO (10^9)
pub const RAO_PER_TAO: u64 = 1_000_000_000;

/// Balance representation for Bittensor tokens
///
/// Internally stored as RAO (the smallest unit) for precision.
///
/// # Example
///
/// ```
/// use bittensor::types::Balance;
///
/// // Create from TAO
/// let balance = Balance::from_tao(1.5);
/// assert_eq!(balance.as_tao(), 1.5);
/// assert_eq!(balance.as_rao(), 1_500_000_000);
///
/// // Create from RAO
/// let balance2 = Balance::from_rao(500_000_000);
/// assert_eq!(balance2.as_tao(), 0.5);
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Balance {
    rao: u64,
}

impl Balance {
    /// Create a zero balance
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let zero = Balance::zero();
    /// assert_eq!(zero.as_rao(), 0);
    /// assert!(zero.is_zero());
    /// ```
    pub const fn zero() -> Self {
        Self { rao: 0 }
    }

    /// Create a balance from RAO (smallest unit)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let balance = Balance::from_rao(1_000_000_000);
    /// assert_eq!(balance.as_tao(), 1.0);
    /// ```
    pub const fn from_rao(rao: u64) -> Self {
        Self { rao }
    }

    /// Create a balance from TAO
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let balance = Balance::from_tao(2.5);
    /// assert_eq!(balance.as_rao(), 2_500_000_000);
    /// ```
    pub fn from_tao(tao: f64) -> Self {
        let rao = (tao * RAO_PER_TAO as f64) as u64;
        Self { rao }
    }

    /// Get the balance in RAO
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let balance = Balance::from_tao(1.0);
    /// assert_eq!(balance.as_rao(), 1_000_000_000);
    /// ```
    pub const fn as_rao(&self) -> u64 {
        self.rao
    }

    /// Get the balance in TAO
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let balance = Balance::from_rao(1_500_000_000);
    /// assert_eq!(balance.as_tao(), 1.5);
    /// ```
    pub fn as_tao(&self) -> f64 {
        self.rao as f64 / RAO_PER_TAO as f64
    }

    /// Check if the balance is zero
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// assert!(Balance::zero().is_zero());
    /// assert!(!Balance::from_tao(1.0).is_zero());
    /// ```
    pub const fn is_zero(&self) -> bool {
        self.rao == 0
    }

    /// Saturating addition (returns MAX on overflow instead of panicking)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let a = Balance::from_tao(1.0);
    /// let b = Balance::from_tao(2.0);
    /// let sum = a.saturating_add(b);
    /// assert_eq!(sum.as_tao(), 3.0);
    /// ```
    pub fn saturating_add(self, other: Self) -> Self {
        Self {
            rao: self.rao.saturating_add(other.rao),
        }
    }

    /// Saturating subtraction (returns 0 on underflow instead of panicking)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let a = Balance::from_tao(1.0);
    /// let b = Balance::from_tao(2.0);
    /// let diff = a.saturating_sub(b);
    /// assert!(diff.is_zero());
    /// ```
    pub fn saturating_sub(self, other: Self) -> Self {
        Self {
            rao: self.rao.saturating_sub(other.rao),
        }
    }

    /// Checked addition (returns None on overflow)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let a = Balance::from_tao(1.0);
    /// let b = Balance::from_tao(2.0);
    /// let sum = a.checked_add(b).unwrap();
    /// assert_eq!(sum.as_tao(), 3.0);
    /// ```
    pub fn checked_add(self, other: Self) -> Option<Self> {
        self.rao.checked_add(other.rao).map(|rao| Self { rao })
    }

    /// Checked subtraction (returns None on underflow)
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::types::Balance;
    ///
    /// let a = Balance::from_tao(1.0);
    /// let b = Balance::from_tao(2.0);
    /// assert!(a.checked_sub(b).is_none());
    /// ```
    pub fn checked_sub(self, other: Self) -> Option<Self> {
        self.rao.checked_sub(other.rao).map(|rao| Self { rao })
    }
}

impl Default for Balance {
    fn default() -> Self {
        Self::zero()
    }
}

impl Add for Balance {
    type Output = Self;

    fn add(self, other: Self) -> Self {
        Self {
            rao: self.rao + other.rao,
        }
    }
}

impl AddAssign for Balance {
    fn add_assign(&mut self, other: Self) {
        self.rao += other.rao;
    }
}

impl Sub for Balance {
    type Output = Self;

    fn sub(self, other: Self) -> Self {
        Self {
            rao: self.rao - other.rao,
        }
    }
}

impl SubAssign for Balance {
    fn sub_assign(&mut self, other: Self) {
        self.rao -= other.rao;
    }
}

impl Mul<u64> for Balance {
    type Output = Self;

    fn mul(self, rhs: u64) -> Self {
        Self {
            rao: self.rao * rhs,
        }
    }
}

impl Div<u64> for Balance {
    type Output = Self;

    fn div(self, rhs: u64) -> Self {
        Self {
            rao: self.rao / rhs,
        }
    }
}

impl fmt::Display for Balance {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let tao = self.as_tao();
        if tao >= 1.0 {
            write!(f, "τ{:.4}", tao)
        } else if self.rao > 0 {
            write!(f, "{} RAO", self.rao)
        } else {
            write!(f, "τ0")
        }
    }
}

impl From<u64> for Balance {
    fn from(rao: u64) -> Self {
        Self::from_rao(rao)
    }
}

impl From<Balance> for u64 {
    fn from(balance: Balance) -> u64 {
        balance.rao
    }
}

/// Convert TAO to RAO
///
/// # Example
///
/// ```
/// use bittensor::types::tao_to_rao;
///
/// assert_eq!(tao_to_rao(1.0), 1_000_000_000);
/// assert_eq!(tao_to_rao(0.5), 500_000_000);
/// ```
pub fn tao_to_rao(tao: f64) -> u64 {
    (tao * RAO_PER_TAO as f64) as u64
}

/// Convert RAO to TAO
///
/// # Example
///
/// ```
/// use bittensor::types::rao_to_tao;
///
/// assert_eq!(rao_to_tao(1_000_000_000), 1.0);
/// assert_eq!(rao_to_tao(500_000_000), 0.5);
/// ```
pub fn rao_to_tao(rao: u64) -> f64 {
    rao as f64 / RAO_PER_TAO as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zero() {
        let zero = Balance::zero();
        assert_eq!(zero.as_rao(), 0);
        assert_eq!(zero.as_tao(), 0.0);
        assert!(zero.is_zero());
    }

    #[test]
    fn test_from_rao() {
        let balance = Balance::from_rao(1_000_000_000);
        assert_eq!(balance.as_rao(), 1_000_000_000);
        assert_eq!(balance.as_tao(), 1.0);
    }

    #[test]
    fn test_from_tao() {
        let balance = Balance::from_tao(2.5);
        assert_eq!(balance.as_rao(), 2_500_000_000);
        assert_eq!(balance.as_tao(), 2.5);
    }

    #[test]
    fn test_addition() {
        let a = Balance::from_tao(1.0);
        let b = Balance::from_tao(2.0);
        let sum = a + b;
        assert_eq!(sum.as_tao(), 3.0);
    }

    #[test]
    fn test_add_assign() {
        let mut balance = Balance::from_tao(1.0);
        balance += Balance::from_tao(2.0);
        assert_eq!(balance.as_tao(), 3.0);
    }

    #[test]
    fn test_subtraction() {
        let a = Balance::from_tao(3.0);
        let b = Balance::from_tao(1.0);
        let diff = a - b;
        assert_eq!(diff.as_tao(), 2.0);
    }

    #[test]
    fn test_sub_assign() {
        let mut balance = Balance::from_tao(3.0);
        balance -= Balance::from_tao(1.0);
        assert_eq!(balance.as_tao(), 2.0);
    }

    #[test]
    fn test_multiplication() {
        let balance = Balance::from_tao(2.0);
        let result = balance * 3;
        assert_eq!(result.as_tao(), 6.0);
    }

    #[test]
    fn test_division() {
        let balance = Balance::from_tao(6.0);
        let result = balance / 2;
        assert_eq!(result.as_tao(), 3.0);
    }

    #[test]
    fn test_saturating_add() {
        let max = Balance::from_rao(u64::MAX);
        let one = Balance::from_rao(1);
        let result = max.saturating_add(one);
        assert_eq!(result.as_rao(), u64::MAX);
    }

    #[test]
    fn test_saturating_sub() {
        let small = Balance::from_tao(1.0);
        let big = Balance::from_tao(2.0);
        let result = small.saturating_sub(big);
        assert!(result.is_zero());
    }

    #[test]
    fn test_checked_add() {
        let a = Balance::from_tao(1.0);
        let b = Balance::from_tao(2.0);
        assert!(a.checked_add(b).is_some());

        let max = Balance::from_rao(u64::MAX);
        let one = Balance::from_rao(1);
        assert!(max.checked_add(one).is_none());
    }

    #[test]
    fn test_checked_sub() {
        let a = Balance::from_tao(3.0);
        let b = Balance::from_tao(1.0);
        assert!(a.checked_sub(b).is_some());

        let small = Balance::from_tao(1.0);
        let big = Balance::from_tao(2.0);
        assert!(small.checked_sub(big).is_none());
    }

    #[test]
    fn test_display() {
        let balance = Balance::from_tao(1.5);
        let display = format!("{}", balance);
        assert!(display.contains("τ"));
        assert!(display.contains("1.5"));

        let small = Balance::from_rao(100);
        let display_small = format!("{}", small);
        assert!(display_small.contains("RAO"));

        let zero = Balance::zero();
        assert_eq!(format!("{}", zero), "τ0");
    }

    #[test]
    fn test_comparison() {
        let a = Balance::from_tao(1.0);
        let b = Balance::from_tao(2.0);
        assert!(a < b);
        assert!(b > a);
        assert_eq!(a, Balance::from_tao(1.0));
    }

    #[test]
    fn test_serialization() {
        let balance = Balance::from_tao(1.5);
        let json = serde_json::to_string(&balance).unwrap();
        let deserialized: Balance = serde_json::from_str(&json).unwrap();
        assert_eq!(balance, deserialized);
    }

    #[test]
    fn test_tao_to_rao() {
        assert_eq!(tao_to_rao(1.0), 1_000_000_000);
        assert_eq!(tao_to_rao(0.5), 500_000_000);
        assert_eq!(tao_to_rao(0.0), 0);
    }

    #[test]
    fn test_rao_to_tao() {
        assert_eq!(rao_to_tao(1_000_000_000), 1.0);
        assert_eq!(rao_to_tao(500_000_000), 0.5);
        assert_eq!(rao_to_tao(0), 0.0);
    }

    #[test]
    fn test_from_into() {
        let balance: Balance = 1_000_000_000u64.into();
        assert_eq!(balance.as_tao(), 1.0);

        let rao: u64 = balance.into();
        assert_eq!(rao, 1_000_000_000);
    }
}
