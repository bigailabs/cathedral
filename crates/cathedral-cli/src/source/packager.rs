//! Source code packaging for deployment

use crate::error::SourceError;
use std::path::{Path, PathBuf};

/// Maximum file size for inline source (1MB)
const MAX_SOURCE_SIZE: usize = 1024 * 1024;

/// Source type with associated data
#[derive(Debug, Clone)]
pub enum SourceType {
    /// Python file with path and content
    PythonFile { path: PathBuf, content: String },
    /// Inline Python code
    InlineCode(String),
    /// Docker image reference (no source processing)
    DockerImage(String),
}

/// Detected Python framework
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Framework {
    FastApi,
    Flask,
    Django,
    Streamlit,
    #[default]
    Unknown,
}

impl std::fmt::Display for Framework {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Framework::FastApi => write!(f, "FastAPI"),
            Framework::Flask => write!(f, "Flask"),
            Framework::Django => write!(f, "Django"),
            Framework::Streamlit => write!(f, "Streamlit"),
            Framework::Unknown => write!(f, "Unknown"),
        }
    }
}

/// Source code packager for deployment
pub struct SourcePackager {
    source_type: SourceType,
}

impl SourcePackager {
    /// Create a new packager from input string
    pub fn new(input: &str) -> Result<Self, SourceError> {
        let source_type = Self::detect_and_load(input)?;
        Ok(Self { source_type })
    }

    /// Create packager from inline Python code (for testing)
    #[cfg(test)]
    pub fn from_inline_code(code: &str) -> Self {
        Self {
            source_type: SourceType::InlineCode(code.to_string()),
        }
    }

    /// Create packager from Docker image reference (for testing)
    #[cfg(test)]
    pub fn from_docker_image(image: &str) -> Self {
        Self {
            source_type: SourceType::DockerImage(image.to_string()),
        }
    }

    /// Create packager from Python file content (for testing without file system)
    #[cfg(test)]
    pub fn from_python_content(path: &str, content: &str) -> Self {
        Self {
            source_type: SourceType::PythonFile {
                path: PathBuf::from(path),
                content: content.to_string(),
            },
        }
    }

    /// Get the source type
    pub fn source_type(&self) -> &SourceType {
        &self.source_type
    }

    /// Get content if available (not for Docker images)
    pub fn content(&self) -> Option<&str> {
        match &self.source_type {
            SourceType::PythonFile { content, .. } => Some(content),
            SourceType::InlineCode(content) => Some(content),
            SourceType::DockerImage(_) => None,
        }
    }

    /// Detect source type and load content
    fn detect_and_load(input: &str) -> Result<SourceType, SourceError> {
        // Check if it's a Python file
        if input.ends_with(".py") {
            let path = PathBuf::from(input);
            let expanded = shellexpand::tilde(input);
            let full_path = PathBuf::from(expanded.as_ref());

            if !full_path.exists() {
                return Err(SourceError::FileNotFound { path });
            }

            let content = Self::read_and_validate(&full_path)?;
            return Ok(SourceType::PythonFile { path, content });
        }

        // Check if it's an existing file (any extension)
        let expanded = shellexpand::tilde(input);
        let path = PathBuf::from(expanded.as_ref());
        if path.exists() && path.is_file() {
            let content = Self::read_and_validate(&path)?;
            return Ok(SourceType::PythonFile {
                path: PathBuf::from(input),
                content,
            });
        }

        // Check if it looks like a Docker image reference
        if Self::is_docker_image(input) {
            return Ok(SourceType::DockerImage(input.to_string()));
        }

        // Treat as inline Python code if it contains Python-like syntax
        if Self::looks_like_python(input) {
            if input.len() > MAX_SOURCE_SIZE {
                return Err(SourceError::FileTooLarge {
                    path: PathBuf::from("<inline>"),
                    size: input.len(),
                    max: MAX_SOURCE_SIZE,
                });
            }
            return Ok(SourceType::InlineCode(input.to_string()));
        }

        Err(SourceError::UnknownSourceType {
            input: input.to_string(),
        })
    }

    /// Read file and validate size/content
    fn read_and_validate(path: &Path) -> Result<String, SourceError> {
        let metadata = std::fs::metadata(path)?;
        let size = metadata.len() as usize;

        if size > MAX_SOURCE_SIZE {
            return Err(SourceError::FileTooLarge {
                path: path.to_owned(),
                size,
                max: MAX_SOURCE_SIZE,
            });
        }

        let content = std::fs::read_to_string(path)?;

        if content.trim().is_empty() {
            return Err(SourceError::EmptyFile {
                path: path.to_owned(),
            });
        }

        Ok(content)
    }

    /// Check if input looks like a Docker image reference
    fn is_docker_image(input: &str) -> bool {
        // Docker image patterns:
        // - name:tag (nginx:latest)
        // - registry/name:tag (docker.io/nginx:latest)
        // - registry:port/name:tag (localhost:5000/myapp:v1)
        let has_registry_chars = input.contains('/') || input.contains(':');
        let no_spaces = !input.contains(' ');
        let no_newlines = !input.contains('\n');
        let no_python_markers = !input.contains("import ")
            && !input.contains("def ")
            && !input.contains("class ")
            && !input.contains("print(");

        has_registry_chars && no_spaces && no_newlines && no_python_markers
    }

    /// Check if input looks like Python code
    fn looks_like_python(input: &str) -> bool {
        input.contains("import ")
            || input.contains("from ")
            || input.contains("def ")
            || input.contains("class ")
            || input.contains("print(")
            || input.contains("if __name__")
    }

    /// Detect framework from content
    pub fn detect_framework(&self) -> Framework {
        let content = match self.content() {
            Some(c) => c,
            None => return Framework::Unknown,
        };

        if content.contains("from fastapi") || content.contains("import fastapi") {
            Framework::FastApi
        } else if content.contains("from flask") || content.contains("import flask") {
            Framework::Flask
        } else if content.contains("from django") || content.contains("import django") {
            Framework::Django
        } else if content.contains("import streamlit") || content.contains("from streamlit") {
            Framework::Streamlit
        } else {
            Framework::Unknown
        }
    }

    /// Get default packages for detected framework
    pub fn default_packages(&self) -> Vec<String> {
        match self.detect_framework() {
            Framework::FastApi => vec!["fastapi".into(), "uvicorn".into()],
            Framework::Flask => vec!["flask".into()],
            Framework::Django => vec!["django".into(), "gunicorn".into()],
            Framework::Streamlit => vec!["streamlit".into()],
            Framework::Unknown => vec![],
        }
    }

    /// Build container command with heredoc
    pub fn build_command(&self, pip_packages: &[String]) -> Option<(Vec<String>, Vec<String>)> {
        let content = self.content()?;

        let packages = if pip_packages.is_empty() {
            self.default_packages()
        } else {
            pip_packages.to_vec()
        };

        let pip_install = if packages.is_empty() {
            String::new()
        } else {
            format!("pip install -q {} && ", packages.join(" "))
        };

        let script = format!(
            r#"{}python3 - <<'BASILICA_PYCODE'
{}
BASILICA_PYCODE"#,
            pip_install, content
        );

        Some((vec!["bash".to_string(), "-c".to_string()], vec![script]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_source_type_docker_image() {
        let packager = SourcePackager::new("nginx:latest").unwrap();
        assert!(matches!(packager.source_type(), SourceType::DockerImage(_)));
    }

    #[test]
    fn test_detect_source_type_docker_registry() {
        let packager = SourcePackager::new("docker.io/library/nginx:latest").unwrap();
        assert!(matches!(packager.source_type(), SourceType::DockerImage(_)));
    }

    #[test]
    fn test_detect_framework_fastapi() {
        let packager =
            SourcePackager::from_inline_code("from fastapi import FastAPI\napp = FastAPI()");
        assert_eq!(packager.detect_framework(), Framework::FastApi);
    }

    #[test]
    fn test_detect_framework_flask() {
        let packager =
            SourcePackager::from_inline_code("from flask import Flask\napp = Flask(__name__)");
        assert_eq!(packager.detect_framework(), Framework::Flask);
    }

    #[test]
    fn test_packager_from_docker_image() {
        let packager = SourcePackager::from_docker_image("nginx:latest");
        assert!(matches!(packager.source_type(), SourceType::DockerImage(_)));
        assert!(packager.content().is_none());
    }

    #[test]
    fn test_packager_from_python_content() {
        let packager = SourcePackager::from_python_content("app.py", "print('hello')");
        assert!(matches!(
            packager.source_type(),
            SourceType::PythonFile { .. }
        ));
        assert_eq!(packager.content(), Some("print('hello')"));
    }

    #[test]
    fn test_default_packages_fastapi() {
        let packager = SourcePackager::from_inline_code("from fastapi import FastAPI");
        let packages = packager.default_packages();
        assert!(packages.contains(&"fastapi".to_string()));
        assert!(packages.contains(&"uvicorn".to_string()));
    }

    #[test]
    fn test_build_command_with_packages() {
        let packager = SourcePackager::from_inline_code("print('hello')");
        let (cmd, args) = packager.build_command(&["requests".to_string()]).unwrap();
        assert_eq!(cmd, vec!["bash", "-c"]);
        assert!(args[0].contains("pip install -q requests"));
        assert!(args[0].contains("print('hello')"));
    }

    #[test]
    fn test_unknown_source_type() {
        let result = SourcePackager::new("not_a_file_or_docker_or_python");
        assert!(result.is_err());
    }
}
