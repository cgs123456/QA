//! Linux encrypted-file key fallback (F6.3 Phase 2).
//!
//! Trigger: the libsecret/D-Bus backend is unavailable (keyring backend error
//! in [`crate::security::keychain`]). The file store is strictly second choice:
//! a healthy keychain is always source of truth and the file is never consulted.
//!
//! NEVER plaintext: every failure mode returns `Err` — missing machine-id,
//! corrupt file, tag mismatch, unwritable dir. There is deliberately no
//! "store it anyway" path.
//!
//! Crypto without new crates: HKDF-CTR+HMAC-SHA256 built on `sha2`, which was
//! promoted from dev-dependencies to a real dependency for this (it was already
//! in the lockfile via dev-deps — `Cargo.lock` gains **zero** new crates).
//! Per-secret random 16 B salt from `/dev/urandom`; keys bound to the OS
//! machine-id (`/etc/machine-id`, else `/var/lib/dbus/machine-id`).
//! Key schedule: `kEnc`/`kMac` are `SHA256(machine_id || salt || label)`;
//! keystream blocks are `SHA256(kEnc || be64(counter))` XORed into the
//! plaintext; the tag is `SHA256(kMac || ct)[..16]`, verified in constant
//! time on read.
//! A copied file is useless off-machine (wrong machine-id ⇒ tag mismatch).
//! This is NOT AES-GCM — it is the best construction available under the
//! no-new-deps constraint, and strictly better than the forbidden plaintext.
//! The OS keychain remains primary wherever it exists.
//!
//! R8: provider names at most in errors/logs — never key material, and never
//! the machine-id either (it is key-equivalent).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// On-disk format marker (not secret; lets future readers reject stale files).
const MAGIC: &str = "IC-KF1";
const FORMAT_VERSION: u32 = 1;
/// File name under the app data dir.
pub const FILE_NAME: &str = "keys.enc.json";
const SALT_LEN: usize = 16;
const TAG_LEN: usize = 16;

// ---------------------------------------------------------------------------
// Machine ID (OS-provided — the "no new dependency" half of the constraint).
// ---------------------------------------------------------------------------

/// Production candidate paths, in order (Linux only; elsewhere empty).
#[cfg(target_os = "linux")]
fn default_machine_id_paths() -> Vec<PathBuf> {
    vec![
        PathBuf::from("/etc/machine-id"),
        PathBuf::from("/var/lib/dbus/machine-id"),
    ]
}

/// Non-Linux builds never reach the file store in production (the fallback is
/// only wired in on Linux); unit tests inject explicit paths instead.
#[cfg(not(target_os = "linux"))]
fn default_machine_id_paths() -> Vec<PathBuf> {
    Vec::new()
}

fn read_machine_id_from(candidates: &[PathBuf]) -> Result<String, String> {
    for path in candidates {
        match std::fs::read_to_string(path) {
            Ok(text) => {
                let id = text.trim().to_string();
                if !id.is_empty() {
                    return Ok(id);
                }
            }
            Err(_) => continue,
        }
    }
    Err("no OS machine-id available; refusing to use the encrypted-file store".to_string())
}

/// Production machine-id lookup.
///
/// `INTERVIEWCOPILOT_MACHINE_ID_FILE`, when set to a non-empty path, is tried
/// first — primarily a unit-test seam (parallel-safe only behind the test
/// modules' env mutex), secondarily an admin escape hatch on machines whose
/// OS id files are unreadable.
pub fn machine_id() -> Result<String, String> {
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::var("INTERVIEWCOPILOT_MACHINE_ID_FILE") {
        if !path.trim().is_empty() {
            candidates.push(PathBuf::from(path));
        }
    }
    candidates.extend(default_machine_id_paths());
    read_machine_id_from(&candidates)
}

// ---------------------------------------------------------------------------
// Crypto (sha2 only).
// ---------------------------------------------------------------------------

fn sha256(data: &[u8]) -> [u8; 32] {
    use sha2::{Digest, Sha256};

    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finalize().into()
}

fn derive_key(machine_id: &str, salt: &[u8], label: &[u8]) -> [u8; 32] {
    let mut buf = Vec::with_capacity(machine_id.len() + salt.len() + label.len());
    buf.extend_from_slice(machine_id.as_bytes());
    buf.extend_from_slice(salt);
    buf.extend_from_slice(label);
    sha256(&buf)
}

fn keystream(key: &[u8; 32], len: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(len);
    let mut counter: u64 = 0;
    while out.len() < len {
        let mut block = Vec::with_capacity(32 + 8);
        block.extend_from_slice(key);
        block.extend_from_slice(&counter.to_be_bytes());
        out.extend_from_slice(&sha256(&block));
        counter += 1;
    }
    out.truncate(len);
    out
}

fn xor_in_place(data: &mut [u8], pad: &[u8]) {
    for (byte, key) in data.iter_mut().zip(pad.iter()) {
        *byte ^= *key;
    }
}

/// Constant-time equality (no `subtle` crate — this loop is the whole trick,
///
/// and it must stay branch-free over the bytes).
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

fn unhex(text: &str) -> Result<Vec<u8>, String> {
    fn nibble(c: u8) -> Result<u8, String> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err("key file contains non-hex data".to_string()),
        }
    }
    let bytes = text.as_bytes();
    if !bytes.len().is_multiple_of(2) {
        return Err("key file contains odd-length hex".to_string());
    }
    let mut out = Vec::with_capacity(bytes.len() / 2);
    let (pairs, _) = bytes.as_chunks::<2>();
    for pair in pairs {
        out.push(nibble(pair[0])? << 4 | nibble(pair[1])?);
    }
    Ok(out)
}

fn seal(machine_id: &str, salt: &[u8], plaintext: &[u8]) -> (String, String) {
    let k_enc = derive_key(machine_id, salt, b"enc");
    let k_mac = derive_key(machine_id, salt, b"mac");
    let pad = keystream(&k_enc, plaintext.len());
    let mut ct = plaintext.to_vec();
    xor_in_place(&mut ct, &pad);
    let mut mac_input = Vec::with_capacity(k_mac.len() + ct.len());
    mac_input.extend_from_slice(&k_mac);
    mac_input.extend_from_slice(&ct);
    let tag = sha256(&mac_input);
    (hex(&ct), hex(&tag[..TAG_LEN]))
}

fn open(machine_id: &str, salt: &[u8], ct: &[u8], tag: &[u8]) -> Result<Vec<u8>, String> {
    let k_mac = derive_key(machine_id, salt, b"mac");
    let mut mac_input = Vec::with_capacity(k_mac.len() + ct.len());
    mac_input.extend_from_slice(&k_mac);
    mac_input.extend_from_slice(ct);
    let expect = sha256(&mac_input);
    if !ct_eq(&expect[..TAG_LEN], tag) {
        return Err("key file authentication failed (wrong machine or tampered file)".to_string());
    }
    let k_enc = derive_key(machine_id, salt, b"enc");
    let pad = keystream(&k_enc, ct.len());
    let mut pt = ct.to_vec();
    xor_in_place(&mut pt, &pad);
    Ok(pt)
}

#[cfg(target_os = "linux")]
fn random_salt() -> Result<[u8; SALT_LEN], String> {
    use std::io::Read;

    let mut file =
        std::fs::File::open("/dev/urandom").map_err(|e| format!("no OS randomness: {e}"))?;
    let mut salt = [0u8; SALT_LEN];
    file.read_exact(&mut salt)
        .map_err(|e| format!("no OS randomness: {e}"))?;
    Ok(salt)
}

/// Non-Linux salt source is test-only quality (time+pid+counter): the
/// production path is always Linux+urandom (see module docs).
#[cfg(not(target_os = "linux"))]
fn random_salt() -> Result<[u8; SALT_LEN], String> {
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| format!("no clock: {e}"))?;
    let seed = format!(
        "{}-{}-{}-{MAGIC}",
        now.subsec_nanos(),
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let digest = sha256(seed.as_bytes());
    let mut salt = [0u8; SALT_LEN];
    salt.copy_from_slice(&digest[..SALT_LEN]);
    Ok(salt)
}

// ---------------------------------------------------------------------------
// File store (JSON map account -> sealed entry, mode 0o600, atomic rename).
// ---------------------------------------------------------------------------

#[derive(serde::Serialize, serde::Deserialize)]
struct FileEntry {
    magic: String,
    v: u32,
    salt: String,
    ct: String,
    tag: String,
}

type FileMap = BTreeMap<String, FileEntry>;

/// Base dir is injected (production: app data dir; tests: tempdir) so file
/// placement stays testable without globals.
pub fn store_path(base: &Path) -> PathBuf {
    base.join(FILE_NAME)
}

/// Production base dir (Linux user-data dir). Non-Linux always errors —
/// the fallback is only wired in on Linux; tests pass explicit dirs.
pub fn default_base_dir() -> Result<PathBuf, String> {
    #[cfg(target_os = "linux")]
    {
        let data = std::env::var("XDG_DATA_HOME")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var("HOME")
                    .ok()
                    .map(|home| PathBuf::from(home).join(".local/share"))
            });
        data.map(|dir| dir.join("interview-copilot"))
            .ok_or_else(|| "no home directory for key file".to_string())
    }
    #[cfg(not(target_os = "linux"))]
    {
        Err("encrypted-file fallback is Linux-only".to_string())
    }
}

fn read_map(path: &Path) -> Result<FileMap, String> {
    match std::fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text).map_err(|e| format!("key file corrupt: {e}")),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
        Err(e) => Err(format!("key file unreadable: {e}")),
    }
}

fn write_map(path: &Path, map: &FileMap) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("key dir not writable: {e}"))?;
    }
    let text =
        serde_json::to_string_pretty(map).map_err(|e| format!("key file encode failed: {e}"))?;
    // Atomic rename so a killed writer never leaves a torn file（R6 融合写法：
    // 短事务精神——要么旧文件完整，要么新文件完整）。
    let tmp = path.with_extension("tmp");
    {
        use std::io::Write;

        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts
            .open(&tmp)
            .map_err(|e| format!("key file not writable: {e}"))?;
        file.write_all(text.as_bytes())
            .map_err(|e| format!("key file write failed: {e}"))?;
        file.sync_all()
            .map_err(|e| format!("key file sync failed: {e}"))?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // umask 可能吃掉 create-mode 的位：写完再强制一次，拒绝弱权限文件存在。
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("key file chmod failed: {e}"))?;
    }
    std::fs::rename(&tmp, path).map_err(|e| format!("key file replace failed: {e}"))?;
    Ok(())
}

fn check_account(provider: &str) -> Result<String, String> {
    let account = provider.trim().to_string();
    if account.is_empty() {
        return Err("provider 必填".to_string());
    }
    Ok(account)
}

/// Store (or overwrite) a key in the encrypted file. Fails loudly on any
/// problem — there is no plaintext path, not even as a last resort.
pub fn set_api_key_fallback(provider: &str, secret: &str, base: &Path) -> Result<(), String> {
    let account = check_account(provider)?;
    if secret.is_empty() {
        return Err("api_key 必填".to_string());
    }
    let machine = machine_id()?;
    let salt = random_salt()?;
    let (ct, tag) = seal(&machine, &salt, secret.as_bytes());
    let path = store_path(base);
    let mut map = read_map(&path)?;
    map.insert(
        account,
        FileEntry {
            magic: MAGIC.to_string(),
            v: FORMAT_VERSION,
            salt: hex(&salt),
            ct,
            tag,
        },
    );
    write_map(&path, &map)
}

/// Read a key from the encrypted file. `Ok(None)` = no entry stored.
pub fn get_api_key_fallback(provider: &str, base: &Path) -> Result<Option<String>, String> {
    let account = check_account(provider)?;
    let map = read_map(&store_path(base))?;
    let entry = match map.get(&account) {
        None => return Ok(None),
        Some(entry) => entry,
    };
    if entry.magic != MAGIC || entry.v != FORMAT_VERSION {
        return Err("key file entry version mismatch".to_string());
    }
    let machine = machine_id()?;
    let salt = unhex(&entry.salt)?;
    let ct = unhex(&entry.ct)?;
    let tag = unhex(&entry.tag)?;
    let pt = open(&machine, &salt, &ct, &tag)?;
    String::from_utf8(pt)
        .map(Some)
        .map_err(|_| "key file decrypt yielded non-utf8".to_string())
}

/// Test-only machine-id injection (shared by this module's and keychain's
/// tests — ONE mutex for the whole process, since env is process-global and
/// Rust runs tests on threads of a single process).
#[cfg(test)]
pub(crate) mod test_support {
    use std::sync::{
        atomic::{AtomicU64, Ordering},
        Mutex, MutexGuard,
    };

    static ENV_LOCK: Mutex<()> = Mutex::new(());
    static SEQ: AtomicU64 = AtomicU64::new(0);

    pub(crate) struct MachineIdGuard {
        _lock: MutexGuard<'static, ()>,
        path: std::path::PathBuf,
    }

    impl Drop for MachineIdGuard {
        fn drop(&mut self) {
            std::env::remove_var("INTERVIEWCOPILOT_MACHINE_ID_FILE");
            let _ = std::fs::remove_file(&self.path);
        }
    }

    fn workdir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("ic-kf-test-{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create test dir");
        dir
    }

    /// Point machine-id lookup at a temp file; hold the guard whole test.
    /// Tests needing isolation must use distinct `name`s (dirs are per-name).
    pub(crate) fn use_machine_id(
        name: &str,
        contents: &str,
    ) -> (MachineIdGuard, std::path::PathBuf) {
        let lock = ENV_LOCK.lock().expect("env lock");
        let dir = workdir(name);
        let n = SEQ.fetch_add(1, Ordering::Relaxed);
        let path = dir.join(format!("machine-id-{n}"));
        std::fs::write(&path, contents).expect("write fake machine-id");
        std::env::set_var("INTERVIEWCOPILOT_MACHINE_ID_FILE", &path);
        (
            MachineIdGuard {
                _lock: lock,
                path: path.clone(),
            },
            dir,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::use_machine_id;
    use super::*;

    #[test]
    fn roundtrip_and_missing_account() {
        let (_guard, dir) = use_machine_id("roundtrip", "test-machine-001\n");
        set_api_key_fallback("openai", "sk-test-abc", &dir).unwrap();
        assert_eq!(
            get_api_key_fallback("openai", &dir).unwrap(),
            Some("sk-test-abc".to_string())
        );
        assert_eq!(get_api_key_fallback("gemini", &dir).unwrap(), None);
    }

    #[test]
    fn overwrite_keeps_latest_only() {
        let (_guard, dir) = use_machine_id("overwrite", "m\n");
        set_api_key_fallback("openai", "old", &dir).unwrap();
        set_api_key_fallback("openai", "new", &dir).unwrap();
        assert_eq!(
            get_api_key_fallback("openai", &dir).unwrap(),
            Some("new".to_string())
        );
    }

    #[test]
    fn wrong_machine_id_fails_loudly() {
        let (guard_a, dir) = use_machine_id("machine", "machine-A\n");
        set_api_key_fallback("openai", "sk-x", &dir).unwrap();
        drop(guard_a); // 释放 env 互斥并清变量（key 文件保留在 dir 里）
        let (_guard_b, _) = use_machine_id("machine-b", "machine-B\n");
        let err = get_api_key_fallback("openai", &dir).unwrap_err();
        assert!(err.contains("authentication failed"), "unexpected: {err}");
    }

    #[test]
    fn tampered_ciphertext_fails_loudly() {
        let (_guard, dir) = use_machine_id("tamper", "m\n");
        set_api_key_fallback("openai", "sk-x", &dir).unwrap();
        let path = store_path(&dir);
        // 只动 tag 值里的一个 hex 字符（账号名/结构不动 → 必走认证校验）。
        let text = std::fs::read_to_string(&path).unwrap();
        let anchor = "\"tag\": \"";
        let pos = text.find(anchor).expect("tag field present") + anchor.len();
        let mut chars: Vec<char> = text.chars().collect();
        chars[pos] = if chars[pos] == 'a' { 'b' } else { 'a' };
        std::fs::write(&path, chars.into_iter().collect::<String>()).unwrap();
        let err = get_api_key_fallback("openai", &dir).unwrap_err();
        assert!(err.contains("authentication failed"), "unexpected: {err}");
    }

    #[test]
    fn corrupt_file_fails_loudly() {
        let (_guard, dir) = use_machine_id("corrupt", "m\n");
        std::fs::write(store_path(&dir), "{not json").unwrap();
        let err = get_api_key_fallback("openai", &dir).unwrap_err();
        assert!(err.contains("corrupt"), "unexpected: {err}");
    }

    #[test]
    fn plaintext_is_never_written() {
        let (_guard, dir) = use_machine_id("plaintext", "m\n");
        let secret = "sk-UNIQUEPLAINTEXT7z9q";
        set_api_key_fallback("openai", secret, &dir).unwrap();
        let raw = std::fs::read(store_path(&dir)).unwrap();
        assert!(
            raw.windows(secret.len()).all(|w| w != secret.as_bytes()),
            "secret appears verbatim in the key file"
        );
    }

    #[test]
    fn rejects_empty_inputs() {
        let (_guard, dir) = use_machine_id("empty", "m\n");
        assert!(set_api_key_fallback("", "sk-x", &dir).is_err());
        assert!(set_api_key_fallback("openai", "", &dir).is_err());
        assert!(get_api_key_fallback("", &dir).is_err());
    }

    #[test]
    fn missing_machine_id_fails_loudly() {
        // Guard holds the env mutex but points at a nonexistent file.
        let (_guard, dir) = use_machine_id("nomid", "m\n");
        std::env::set_var(
            "INTERVIEWCOPILOT_MACHINE_ID_FILE",
            dir.join("does-not-exist"),
        );
        let err = set_api_key_fallback("openai", "sk-x", &dir).unwrap_err();
        assert!(err.contains("machine-id"), "unexpected: {err}");
    }

    #[test]
    #[cfg(unix)]
    fn file_mode_is_owner_only() {
        use std::os::unix::fs::PermissionsExt;

        let (_guard, dir) = use_machine_id("mode", "m\n");
        set_api_key_fallback("openai", "sk-x", &dir).unwrap();
        let mode = std::fs::metadata(store_path(&dir))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600, "key file must be owner-only, got {mode:o}");
    }

    #[test]
    fn crypto_unit_seal_open_and_tamper() {
        let (ct, tag) = seal("mid", b"0123456789abcdef", b"hello");
        let pt = open(
            "mid",
            b"0123456789abcdef",
            &unhex(&ct).unwrap(),
            &unhex(&tag).unwrap(),
        )
        .unwrap();
        assert_eq!(pt, b"hello");
        assert!(open(
            "other",
            b"0123456789abcdef",
            &unhex(&ct).unwrap(),
            &unhex(&tag).unwrap()
        )
        .is_err());
        let mut bad = unhex(&tag).unwrap();
        bad[0] ^= 0x01;
        assert!(open("mid", b"0123456789abcdef", &unhex(&ct).unwrap(), &bad).is_err());
        assert!(unhex("zz").is_err() && unhex("abc").is_err());
    }
}
