# Deployment manifests

`manifests/hermes-toolsets.yaml` is the source-controlled declaration for the
Librarian Hermes platform toolsets. It records the additive built-in tool
surface for Telegram and CLI and the MCP servers that must remain enabled.

The manifest does not contain credentials, mutable gateway state, or live
profile data. Applying a change to the live profile requires updating the
profile-local Hermes configuration, restarting
`hermes-gateway-hve-librarian.service`, and verifying the service heartbeat,
channel connection, tool surface, and MCP preservation.
