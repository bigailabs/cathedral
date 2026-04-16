Place the external veritas binaries here before building the validator image:

- executor-binary
- validator-binary

These files will be copied into the container at /opt/cathedral/bin/.
The Dockerfile is resilient and will still build if this directory is empty,
but features depending on these binaries will not be available.

