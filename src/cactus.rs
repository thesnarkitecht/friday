//! The Cactus engine - Needle 3 (tool calls) and Mirage (vision) - through its C ABI.
//!
//! It is one shared library holding every loaded model, and it is not thread-safe, so all calls
//! go through one lock. The library and the weights are downloaded on first use into the same
//! cache the `cactus` Python package uses, and every file is checked against a pinned SHA-256
//! before it is loaded: this is native code fetched from the network.

use anyhow::{anyhow, bail, Context, Result};
use libloading::Library;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::ffi::{c_char, c_float, c_int, CStr, CString};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const ENGINE_VERSION: &str = "4.1.0";
const ENGINE_REPO: &str = "Cactus-Compute/cactus";
const ENGINE_REVISION: &str = "4e822c9036be2eb0e47ef556df32a818bb1572ef";
const MANIFEST_SHA256: &str = "32e182f6f811acde1109084ee5f5c9a606098b95cc8774f27ece8bc1a506de3b";

pub struct Model {
    name: &'static str,
    repo: &'static str,
    revision: &'static str,
    sha256: &'static str,
    bit: c_int, // its flag in cactus_models()
}

pub const NEEDLE: Model = Model {
    name: "needle3",
    repo: "Cactus-Compute/needle3",
    revision: "d6c7d6a2e9655505e0f8e23339356dd1705900eb",
    sha256: "c9d915eca282ed42d1a09b143b592adb4cc6744ffe2d294adf5cfc5548170c38",
    bit: 1,
};
pub const MIRAGE: Model = Model {
    name: "mirage",
    repo: "Cactus-Compute/mirage",
    revision: "76c1e9e53381aca8fc3843c636216c96ff220122",
    sha256: "4249a0f7515272ff76b9414bec17d7e7daf84b15b66c7fb4f17fce1e66f9f237",
    bit: 4,
};

struct Engine {
    lib: Library,
    loaded: Vec<PathBuf>,
    buffer: Vec<u8>,
}

fn engine() -> Result<&'static Mutex<Engine>> {
    static ENGINE: OnceLock<std::result::Result<Mutex<Engine>, String>> = OnceLock::new();
    ENGINE
        .get_or_init(|| {
            let path = library_path().map_err(|e| format!("{e:#}"))?;
            // SAFETY: the file is the pinned, checksum-verified Cactus engine (or the user's
            // explicit CACTUS_LIB_PATH); loading it runs its initialisers.
            let lib = unsafe { Library::new(&path) }.map_err(|e| format!("{}: {e}", path.display()))?;
            Ok(Mutex::new(Engine { lib, loaded: Vec::new(), buffer: vec![0; 16 << 20] }))
        })
        .as_ref()
        .map_err(|e| anyhow!("Cactus engine unavailable: {e}"))
}

impl Engine {
    fn last_error(&self) -> String {
        // SAFETY: cactus_last_error returns a NUL-terminated string owned by the engine.
        unsafe {
            match self.lib.get::<unsafe extern "C" fn() -> *const c_char>(b"cactus_last_error") {
                Ok(f) if !f().is_null() => CStr::from_ptr(f()).to_string_lossy().into_owned(),
                _ => "unknown engine error".into(),
            }
        }
    }

    fn load(&mut self, weights: &Path, expect_bit: c_int) -> Result<()> {
        if self.loaded.iter().any(|p| p == weights) {
            return Ok(());
        }
        let data = std::fs::read(weights).with_context(|| format!("reading {}", weights.display()))?;
        // SAFETY: signatures match cactus 4.1's ABI (agent/runtime.py); the buffer outlives the call.
        unsafe {
            let load = self.lib.get::<unsafe extern "C" fn(*const u8, u64) -> c_int>(b"cactus_load")?;
            if load(data.as_ptr(), data.len() as u64) < 0 {
                bail!("cactus_load failed for {}: {}", weights.display(), self.last_error());
            }
            let models = self.lib.get::<unsafe extern "C" fn() -> c_int>(b"cactus_models")?;
            if models() & expect_bit == 0 {
                bail!("{} is not the expected kind of model", weights.display());
            }
        }
        self.loaded.push(weights.to_path_buf());
        Ok(())
    }

    fn reply(&self) -> Result<Value> {
        let end = self.buffer.iter().position(|&b| b == 0).unwrap_or(self.buffer.len());
        serde_json::from_slice(&self.buffer[..end]).context("the engine returned unparseable JSON")
    }
}

/// One stateless Needle completion: `text` against `tools_json` (a JSON array of
/// {name, description, parameters}). Returns the engine's envelope: function_calls, confidence...
pub fn complete(system: &str, tools_json: &str, text: &str, tuned: Option<&Path>) -> Result<Value> {
    let weights = match tuned {
        Some(path) => path.to_path_buf(),
        None => weights_path(&NEEDLE)?,
    };
    let mut e = engine()?.lock().unwrap();
    e.load(&weights, NEEDLE.bit)?;
    let (system, tools, text) = (CString::new(system)?, CString::new(tools_json)?, CString::new(text)?);
    let len = e.buffer.len() as c_int;
    // SAFETY: signatures match cactus 4.1's ABI; every pointer is valid for the call, and the
    // engine writes at most `len` bytes of NUL-terminated JSON into the buffer.
    let code = unsafe {
        let init = e.lib.get::<unsafe extern "C" fn(*const c_char, *const c_char, *const c_char) -> c_int>(b"cactus_init")?;
        if init(system.as_ptr(), tools.as_ptr(), std::ptr::null()) < 0 {
            bail!("cactus_init failed: {}", e.last_error());
        }
        e.lib.get::<unsafe extern "C" fn()>(b"cactus_reset")?();
        let out = e.buffer.as_mut_ptr() as *mut c_char;
        let complete = e.lib.get::<unsafe extern "C" fn(*const c_char, *const c_float, c_int, c_int, *mut c_char, c_int) -> c_int>(b"cactus_complete")?;
        complete(text.as_ptr(), std::ptr::null(), 0, 512, out, len)
    };
    if code < 0 {
        bail!("cactus_complete failed: {}", e.last_error());
    }
    e.reply()
}

/// Mirage on a packed RGB image: OCR text and objects, boxes normalised to 0-1000.
pub fn describe(rgb: &[u8], width: u32, height: u32) -> Result<Value> {
    if rgb.len() != (width as usize) * (height as usize) * 3 {
        bail!("describe needs width*height*3 RGB bytes");
    }
    let weights = weights_path(&MIRAGE)?;
    let mut e = engine()?.lock().unwrap();
    e.load(&weights, MIRAGE.bit)?;
    let options = CString::new("{}")?;
    let len = e.buffer.len() as c_int;
    // SAFETY: as above; `rgb` is exactly width*height*3 bytes, checked on entry.
    let code = unsafe {
        let out = e.buffer.as_mut_ptr() as *mut c_char;
        let describe = e.lib.get::<unsafe extern "C" fn(*const u8, c_int, c_int, *const c_char, *const c_char, *const c_char, *mut c_char, c_int) -> c_int>(b"cactus_describe")?;
        describe(rgb.as_ptr(), width as c_int, height as c_int, options.as_ptr(), std::ptr::null(), std::ptr::null(), out, len)
    };
    if code < 0 {
        bail!("cactus_describe failed: {}", e.last_error());
    }
    e.reply()
}

// -- fetching, pinned -------------------------------------------------------------------------

fn cache_root() -> PathBuf {
    std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::home_dir().unwrap_or_default().join(".cache"))
        .join("cactus")
}

fn sha256(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

fn download(repo: &str, revision: &str, file: &str, expect_sha256: &str) -> Result<Vec<u8>> {
    let url = format!("https://huggingface.co/{repo}/resolve/{revision}/{file}");
    let mut bytes = Vec::new();
    crate::http(&url).get(&url).call().with_context(|| format!("downloading {url}"))?.into_reader().read_to_end(&mut bytes)?;
    if sha256(&bytes) != expect_sha256 {
        bail!("checksum mismatch for {url}; refusing to use it");
    }
    Ok(bytes)
}

fn store(path: &Path, bytes: &[u8]) -> Result<()> {
    std::fs::create_dir_all(path.parent().unwrap())?;
    let partial = path.with_extension("partial");
    std::fs::write(&partial, bytes)?;
    std::fs::rename(&partial, path)?; // atomic: a crash never leaves a half-written model
    Ok(())
}

pub fn weights_path(model: &Model) -> Result<PathBuf> {
    let file = format!("{}.cact", model.name);
    let path = cache_root().join("models").join(model.name).join(model.revision).join(&file);
    if !path.exists() {
        store(&path, &download(model.repo, model.revision, &file, model.sha256)?)?;
    }
    Ok(path)
}

fn library_path() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("CACTUS_LIB_PATH") {
        return Ok(path.into());
    }
    let (name, os) = match std::env::consts::OS {
        "macos" => ("libcactus.dylib", "macosx_11_0_"),
        "windows" => ("libcactus.dll", "win_"),
        _ if cfg!(target_env = "musl") => ("libcactus.so", "musllinux_1_2_"),
        _ => ("libcactus.so", "manylinux2014_"),
    };
    let path = cache_root().join("engine").join(ENGINE_VERSION).join(ENGINE_REVISION).join(name);
    if path.exists() {
        return Ok(path);
    }
    let arch = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => "amd64",
        ("macos" | "windows", "aarch64") => "arm64",
        (_, arch @ ("x86_64" | "aarch64")) => arch,
        (_, other) => bail!("no Cactus engine is published for {other}; build libcactus and set CACTUS_LIB_PATH"),
    };
    // The manifest is pinned by hash; it in turn pins every artifact.
    let prefix = format!("releases/{ENGINE_VERSION}/");
    let manifest = download(ENGINE_REPO, ENGINE_REVISION, &format!("{prefix}manifest.json"), MANIFEST_SHA256)?;
    let manifest: Value = serde_json::from_slice(&manifest)?;
    let wheel = format!("python/cactus_runtime-{ENGINE_VERSION}-py3-none-{os}{arch}.whl");
    let expected = manifest["files"][&wheel]["sha256"].as_str().ok_or_else(|| anyhow!("{wheel} is not in the engine manifest"))?;
    let wheel = download(ENGINE_REPO, ENGINE_REVISION, &format!("{prefix}{wheel}"), expected)?;
    let mut lib = Vec::new();
    zip::ZipArchive::new(std::io::Cursor::new(wheel))?.by_name(&format!("cactus/{name}"))?.read_to_end(&mut lib)?;
    store(&path, &lib)?;
    Ok(path)
}
