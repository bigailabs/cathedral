"""
Volume class for persistent storage.

Provides a simple interface for referencing persistent storage volumes
that can be mounted into deployments.
"""
from dataclasses import dataclass


@dataclass
class Volume:
    """
    Persistent storage volume that can be mounted into deployments.

    Example:
        >>> cache = Volume.from_name("model-cache", create_if_missing=True)
        >>> @basilica.deployment(name="app", volumes={"/cache": cache})
        ... def serve():
        ...     pass
    """

    name: str
    create_if_missing: bool = False

    @classmethod
    def from_name(cls, name: str, create_if_missing: bool = False) -> "Volume":
        """
        Reference or create a named volume.

        Args:
            name: Volume name (used as bucket identifier)
            create_if_missing: Create the volume if it doesn't exist

        Returns:
            Volume instance
        """
        return cls(name=name, create_if_missing=create_if_missing)
