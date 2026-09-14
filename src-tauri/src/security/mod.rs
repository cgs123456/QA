//! OS keychain for LLM API keys (PRD F6.3) + secret push channel.
//!
//! R8: key material never appears in logs — provider names at most.

pub mod fallback;
pub mod keychain;
pub mod push;
