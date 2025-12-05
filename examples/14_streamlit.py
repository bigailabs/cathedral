#!/usr/bin/env python3
"""
Streamlit - Interactive data apps in minutes.

Streamlit is a Python framework for building interactive web applications
with minimal code. Perfect for dashboards, data visualization, and ML demos.

This example deploys streamlit_app.py which demonstrates:
  - Interactive widgets (sliders, buttons, text input)
  - Real-time charts
  - Session state management

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 14_streamlit.py

Documentation: https://docs.streamlit.io
"""
import base64
from pathlib import Path

from basilica import BasilicaClient, Deployment


def main():
    client = BasilicaClient()

    print("Deploying Streamlit app...")

    # Read and encode the external Streamlit app file
    app_path = Path(__file__).parent / "streamlit_app.py"
    app_source = app_path.read_text()
    app_b64 = base64.b64encode(app_source.encode()).decode()

    # Build command: install streamlit, decode app, run it
    # Using base64 to safely pass the source code through shell
    script = (
        f'pip install -q streamlit && '
        f'echo "{app_b64}" | base64 -d > /tmp/app.py && '
        f'python3 -m streamlit run /tmp/app.py '
        f'--server.port=8501 --server.address=0.0.0.0 --server.headless=true'
    )

    # Use create_deployment for custom command (streamlit requires special runner)
    response = client.create_deployment(
        instance_name="streamlit-demo",
        image="python:3.11-slim",
        port=8501,
        command=["bash", "-c", script],
        cpu="500m",
        memory="512Mi",
        ttl_seconds=3600,
    )

    deployment = Deployment._from_response(client, response)
    deployment.wait_until_ready(timeout=300)

    print()
    print("=" * 50)
    print("Streamlit App Ready")
    print("=" * 50)
    print(f"URL: {deployment.url}")
    print()
    print("Open the URL in your browser to interact with the app!")


if __name__ == "__main__":
    main()
