#!/usr/bin/env python3
"""
Health Check Example for Cathedral SDK
"""

from cathedral import CathedralClient


def main():
    # Initialize client using environment variables
    # BASILICA_API_URL and BASILICA_API_TOKEN
    client = CathedralClient()

    # Or initialize with explicit configuration
    # client = CathedralClient(
    #     base_url="https://api.basilica.ai",
    #     api_key="cathedral_..."  # Your token from 'cathedral tokens create'
    # )
    
    # Perform health check
    response = client.health_check()
    
    # Access response fields
    print(f"Status: {response.status}")
    print(f"Version: {response.version}")
    print(f"Timestamp: {response.timestamp}")
    print(f"Healthy validators: {response.healthy_validators}/{response.total_validators}")


if __name__ == "__main__":
    main()