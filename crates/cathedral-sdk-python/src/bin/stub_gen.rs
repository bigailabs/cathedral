//! Stub file generator for the cathedral Python module
//!
//! This binary generates Python type stub files (.pyi) for the cathedral module.
//! Run with: cargo run --bin stub_gen --features stub-gen

#[cfg(feature = "stub-gen")]
use pyo3_stub_gen::Result;

#[cfg(feature = "stub-gen")]
fn main() -> Result<()> {
    // Get the stub information from the cathedral module
    let stub = cathedral::stub_info()?;

    // Generate the stub file to python/cathedral/_cathedral.pyi
    stub.generate()?;

    println!("Successfully generated Python stub file!");
    Ok(())
}

#[cfg(not(feature = "stub-gen"))]
fn main() {
    eprintln!("The 'stub-gen' feature is not enabled. Enable it with `--features stub-gen`.");
    std::process::exit(1);
}
