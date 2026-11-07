// Records the compile target triple so the binary can verify the platform
// declared beside its path, SHA-256 and version in settings.json.
fn main() {
    let target = std::env::var("TARGET").expect("cargo sets TARGET for build scripts");
    println!("cargo:rustc-env=COMSOL_MCP_SHIM_TARGET={target}");
    println!("cargo:rerun-if-changed=build.rs");
}
