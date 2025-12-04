"""
Deployment Facade

This module provides a high-level, user-friendly interface for managing deployments.
The Deployment class wraps the low-level API responses and provides convenient
methods for common operations.

Example:
    >>> deployment = client.deploy("my-app", source="app.py")
    >>> print(deployment.url)           # Public URL
    >>> print(deployment.logs())        # Get logs
    >>> deployment.delete()             # Clean up
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .exceptions import (
    DeploymentFailed,
    DeploymentNotFound,
    DeploymentTimeout,
)

if TYPE_CHECKING:
    from . import BasilicaClient


@dataclass
class DeploymentStatus:
    """
    Represents the current status of a deployment.

    Attributes:
        state: Current state (Pending, Active, Running, Failed, Terminating)
        replicas_ready: Number of replicas that are ready
        replicas_desired: Total number of desired replicas
        message: Optional status message or error description
    """
    state: str
    replicas_ready: int
    replicas_desired: int
    message: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        """Check if the deployment is fully ready."""
        return (
            self.state in ("Active", "Running")
            and self.replicas_ready == self.replicas_desired
            and self.replicas_ready > 0
        )

    @property
    def is_failed(self) -> bool:
        """Check if the deployment has failed."""
        return self.state == "Failed"

    @property
    def is_pending(self) -> bool:
        """Check if the deployment is still starting."""
        return self.state in ("Pending", "Provisioning")


class Deployment:
    """
    A facade for managing a Basilica deployment.

    This class provides a convenient, object-oriented interface for working
    with deployments. It wraps the low-level API client and provides methods
    for common operations like getting logs, checking status, and cleanup.

    Attributes:
        name: The deployment instance name
        url: The public URL for accessing the deployment
        namespace: The Kubernetes namespace
        user_id: The owner's user ID
        created_at: Timestamp when the deployment was created

    Example:
        >>> # Create a deployment (via client.deploy())
        >>> deployment = client.deploy("my-api", source="api.py", port=8000)

        >>> # Access deployment info
        >>> print(f"Live at: {deployment.url}")

        >>> # Get logs
        >>> print(deployment.logs(tail=50))

        >>> # Check current status
        >>> status = deployment.status()
        >>> print(f"State: {status.state}, Ready: {status.replicas_ready}/{status.replicas_desired}")

        >>> # Clean up
        >>> deployment.delete()
    """

    def __init__(
        self,
        client: "BasilicaClient",
        instance_name: str,
        url: str,
        namespace: str,
        user_id: str,
        state: str,
        created_at: str,
        replicas_ready: int = 0,
        replicas_desired: int = 1,
        updated_at: Optional[str] = None,
    ):
        """
        Initialize a Deployment instance.

        Note: Users should not create Deployment objects directly.
        Use client.deploy() or client.get_deployment() instead.
        """
        self._client = client
        self._name = instance_name
        self._url = url
        self._namespace = namespace
        self._user_id = user_id
        self._state = state
        self._created_at = created_at
        self._updated_at = updated_at
        self._replicas_ready = replicas_ready
        self._replicas_desired = replicas_desired

    @property
    def name(self) -> str:
        """The deployment instance name."""
        return self._name

    @property
    def url(self) -> str:
        """
        The public URL for accessing the deployment.

        Example:
            >>> print(deployment.url)
            'https://my-app.deployments.basilica.ai'
        """
        return self._url

    @property
    def namespace(self) -> str:
        """The Kubernetes namespace (e.g., 'u-userid123')."""
        return self._namespace

    @property
    def user_id(self) -> str:
        """The owner's user ID."""
        return self._user_id

    @property
    def created_at(self) -> str:
        """Timestamp when the deployment was created (ISO 8601 format)."""
        return self._created_at

    @property
    def state(self) -> str:
        """
        The last known deployment state.

        Note: This may be stale. Call status() for the latest state.
        """
        return self._state

    def status(self) -> DeploymentStatus:
        """
        Get the current deployment status from the API.

        Returns:
            DeploymentStatus with current state and replica counts

        Raises:
            DeploymentNotFound: If the deployment no longer exists
            NetworkError: If the API is unreachable

        Example:
            >>> status = deployment.status()
            >>> if status.is_ready:
            ...     print("Deployment is healthy!")
            >>> elif status.is_failed:
            ...     print(f"Deployment failed: {status.message}")
        """
        response = self._client.get_deployment(self._name)

        # Update cached state
        self._state = response.state
        self._replicas_ready = response.replicas.ready
        self._replicas_desired = response.replicas.desired

        return DeploymentStatus(
            state=response.state,
            replicas_ready=response.replicas.ready,
            replicas_desired=response.replicas.desired,
            message=None,  # API doesn't return message in status
        )

    def logs(self, tail: Optional[int] = None, follow: bool = False) -> str:
        """
        Get deployment logs.

        Args:
            tail: Number of lines from the end to return.
                  If None, returns all available logs.
            follow: If True, streams logs continuously (blocking).
                   Note: follow mode may not be fully supported yet.

        Returns:
            Log content as a string

        Raises:
            DeploymentNotFound: If the deployment doesn't exist
            NetworkError: If the API is unreachable

        Example:
            >>> # Get last 100 lines
            >>> recent_logs = deployment.logs(tail=100)

            >>> # Get all logs
            >>> all_logs = deployment.logs()
        """
        return self._client.get_deployment_logs(self._name, follow=follow, tail=tail)

    def wait_until_ready(
        self,
        timeout: int = 300,
        poll_interval: int = 5,
        raise_on_failure: bool = True,
    ) -> DeploymentStatus:
        """
        Wait for the deployment to become ready.

        Polls the deployment status until it reaches a ready state,
        fails, or times out.

        Args:
            timeout: Maximum seconds to wait (default: 300)
            poll_interval: Seconds between status checks (default: 5)
            raise_on_failure: If True, raises DeploymentFailed on failure state

        Returns:
            Final DeploymentStatus when ready or failed

        Raises:
            DeploymentTimeout: If deployment doesn't become ready within timeout
            DeploymentFailed: If deployment enters Failed state (when raise_on_failure=True)
            DeploymentNotFound: If deployment is deleted during wait

        Example:
            >>> try:
            ...     status = deployment.wait_until_ready(timeout=120)
            ...     print(f"Ready! URL: {deployment.url}")
            ... except DeploymentTimeout:
            ...     print("Timed out waiting for deployment")
        """
        elapsed = 0
        last_status = None

        while elapsed < timeout:
            last_status = self.status()

            if last_status.is_ready:
                return last_status

            if last_status.is_failed and raise_on_failure:
                raise DeploymentFailed(
                    instance_name=self._name,
                    reason=last_status.message
                )

            time.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout reached
        raise DeploymentTimeout(
            instance_name=self._name,
            timeout_seconds=timeout,
            last_state=last_status.state if last_status else "Unknown",
            replicas_ready=last_status.replicas_ready if last_status else 0,
            replicas_desired=last_status.replicas_desired if last_status else 1,
        )

    def delete(self) -> None:
        """
        Delete the deployment.

        This permanently removes the deployment and all associated resources.
        The operation is asynchronous - the deployment may take a few seconds
        to fully terminate.

        Raises:
            DeploymentNotFound: If the deployment doesn't exist
            NetworkError: If the API is unreachable

        Example:
            >>> deployment.delete()
            >>> print(f"Deleted deployment: {deployment.name}")
        """
        self._client.delete_deployment(self._name)
        self._state = "Deleted"

    def refresh(self) -> "Deployment":
        """
        Refresh the deployment data from the API.

        Updates all cached properties with the latest values from the server.

        Returns:
            Self, for method chaining

        Example:
            >>> deployment.refresh()
            >>> print(f"Current state: {deployment.state}")
        """
        response = self._client.get_deployment(self._name)

        self._url = response.url
        self._state = response.state
        self._replicas_ready = response.replicas.ready
        self._replicas_desired = response.replicas.desired
        if response.updated_at:
            self._updated_at = response.updated_at

        return self

    def __repr__(self) -> str:
        return f"Deployment(name={self._name!r}, state={self._state!r}, url={self._url!r})"

    def __str__(self) -> str:
        return f"Deployment '{self._name}' ({self._state}) at {self._url}"

    @classmethod
    def _from_response(cls, client: "BasilicaClient", response) -> "Deployment":
        """
        Create a Deployment from an API response.

        Internal method used by BasilicaClient.
        """
        return cls(
            client=client,
            instance_name=response.instance_name,
            url=response.url,
            namespace=response.namespace,
            user_id=response.user_id,
            state=response.state,
            created_at=response.created_at,
            replicas_ready=response.replicas.ready,
            replicas_desired=response.replicas.desired,
            updated_at=response.updated_at,
        )
